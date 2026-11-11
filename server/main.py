from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from server.config import (
    ensure_admin_key,
    get_admin_key,
    get_public_url,
    is_view_locked,
)
from server.db import (
    create_device,
    delete_device,
    extract_daily_entries,
    get_aggregated_stats,
    get_device_by_id,
    get_device_by_token,
    init_db,
    list_devices,
    upsert_daily_records,
)
from server.installer_templates import (
    build_agent_prompt,
    render_ps1_installer,
    render_sh_installer,
)
from server.middleware import GzipRequestMiddleware

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_admin_key()
    init_db()
    yield


app = FastAPI(
    title="Antigravity Usage Hub",
    description="Zero-privacy multi-device Token telemetry & achievement dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(GzipRequestMiddleware)


def resolve_base_url(request: Request, origin: str | None = None) -> str:
    """
    Priority:
    1. Explicit `origin` query/body parameter
    2. `AG_PUBLIC_URL` environment variable
    3. `X-Forwarded-Proto` + `X-Forwarded-Host` / `Host` headers
    4. `request.base_url`
    """
    if origin and origin.strip():
        return origin.strip().rstrip("/")

    pub_url = get_public_url()
    if pub_url:
        return pub_url

    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = fwd_host or request.headers.get("host", "").strip()
    scheme = fwd_proto or request.url.scheme or "http"
    if host:
        return f"{scheme}://{host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def verify_admin_key(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> str:
    expected = get_admin_key()
    if not x_admin_key:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Key header")
    if x_admin_key.strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid X-Admin-Key")
    return x_admin_key.strip()


def verify_optional_view_lock(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    if not is_view_locked():
        return
    expected = get_admin_key()
    candidate = (
        x_admin_key
        or request.query_params.get("admin_key")
        or request.cookies.get("ag_admin_key")
        or ""
    ).strip()
    if not candidate:
        raise HTTPException(status_code=401, detail="View locked: admin key required")
    if candidate != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key")


class CreateDeviceRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=120)
    origin: str | None = Field(default=None)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "ag-usage-hub"}


@app.get("/", response_class=HTMLResponse)
async def index_page() -> Any:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>ag-usage-hub dashboard missing</h1>", status_code=500)
    return FileResponse(index_file, media_type="text/html; charset=utf-8")


@app.post("/api/devices", dependencies=[Depends(verify_admin_key)])
async def api_create_device(
    body: CreateDeviceRequest,
    request: Request,
) -> dict[str, Any]:
    device = create_device(body.display_name)
    base_url = resolve_base_url(request, body.origin)
    token = device["api_token"]
    prompt = build_agent_prompt(base_url, token)
    return {
        "device_id": device["device_id"],
        "display_name": device["display_name"],
        "api_token": token,
        "created_at": device["created_at"],
        "origin": base_url,
        "sh_url": f"{base_url}/i/{token}.sh",
        "ps1_url": f"{base_url}/i/{token}.ps1",
        "agent_prompt": prompt,
    }


@app.get("/api/devices", dependencies=[Depends(verify_admin_key)])
async def api_list_devices() -> dict[str, Any]:
    return {"devices": list_devices()}


@app.get(
    "/api/devices/{device_id}/prompt",
    dependencies=[Depends(verify_admin_key)],
)
async def api_get_device_prompt(
    device_id: str,
    request: Request,
    origin: str | None = Query(default=None),
) -> dict[str, Any]:
    device = get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    base_url = resolve_base_url(request, origin)
    token = device["api_token"]
    prompt = build_agent_prompt(base_url, token)
    return {
        "device_id": device["device_id"],
        "display_name": device["display_name"],
        "api_token": token,
        "origin": base_url,
        "sh_url": f"{base_url}/i/{token}.sh",
        "ps1_url": f"{base_url}/i/{token}.ps1",
        "agent_prompt": prompt,
    }


@app.delete(
    "/api/devices/{device_id}",
    dependencies=[Depends(verify_admin_key)],
)
async def api_delete_device(
    device_id: str,
    purge_data: bool = Query(default=False),
) -> dict[str, Any]:
    deleted = delete_device(device_id, purge_data=purge_data)
    if not deleted:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "status": "deleted",
        "device_id": device_id,
        "purge_data": purge_data,
    }


@app.get("/i/{token_spec}")
async def serve_installer_script(
    token_spec: str,
    request: Request,
    origin: str | None = Query(default=None),
) -> PlainTextResponse:
    if token_spec.endswith(".sh"):
        api_token = token_spec[:-3]
        script_type = "sh"
    elif token_spec.endswith(".ps1"):
        api_token = token_spec[:-4]
        script_type = "ps1"
    else:
        raise HTTPException(status_code=404, detail="Unsupported installer format")

    device = get_device_by_token(api_token)
    if not device:
        raise HTTPException(status_code=404, detail="Invalid or revoked device token")

    base_url = resolve_base_url(request, origin)
    if script_type == "sh":
        content = render_sh_installer(
            base_url=base_url,
            api_token=api_token,
            device_id=device["device_id"],
            display_name=device["display_name"],
        )
    else:
        content = render_ps1_installer(
            base_url=base_url,
            api_token=api_token,
            device_id=device["device_id"],
            display_name=device["display_name"],
        )

    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
    )


@app.post("/api/report")
async def api_report_usage(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=403, detail="Missing or invalid Bearer token")

    api_token = authorization[7:].strip()
    device = get_device_by_token(api_token)
    if not device:
        raise HTTPException(status_code=403, detail="Invalid or revoked device token")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}")

    hostname = ""
    os_info = ""
    if isinstance(payload, dict):
        hostname = str(
            payload.get("hostname")
            or payload.get("host")
            or payload.get("machine")
            or ""
        ).strip()
        os_info = str(
            payload.get("os_info")
            or payload.get("osInfo")
            or payload.get("os")
            or payload.get("platform")
            or ""
        ).strip()

    daily_entries = extract_daily_entries(payload)
    upserted_count = upsert_daily_records(
        device_id=device["device_id"],
        raw_records=daily_entries,
        hostname=hostname,
        os_info=os_info,
    )

    return {
        "status": "ok",
        "device_id": device["device_id"],
        "display_name": device["display_name"],
        "upserted_days": upserted_count,
    }


@app.get("/api/stats", dependencies=[Depends(verify_optional_view_lock)])
async def api_get_stats(
    device: str = Query(default="all"),
) -> dict[str, Any]:
    return get_aggregated_stats(device_filter=device)
