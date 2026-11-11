import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from server.config import get_db_path, get_sub_monthly_usd


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_datetime(val: str) -> datetime | None:
    if not val or not val.strip():
        return None
    s = val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def compute_device_status(last_seen_at: str, now_dt: datetime | None = None) -> str:
    """
    <= 1h (3600s) -> online
    1h ~ 24h (3600s < delta <= 86400s) -> idle
    > 24h or empty/invalid -> offline
    """
    seen_dt = parse_iso_datetime(last_seen_at)
    if seen_dt is None:
        return "offline"
    ref_dt = now_dt.astimezone(timezone.utc) if now_dt else datetime.now(timezone.utc)
    delta_seconds = max(0.0, (ref_dt - seen_dt).total_seconds())
    if delta_seconds <= 3600:
        return "online"
    if delta_seconds <= 86400:
        return "idle"
    return "offline"


@contextmanager
def get_connection(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    resolved_path = db_path or get_db_path()
    path_obj = Path(resolved_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path_obj), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    with get_connection(db_path) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    api_token TEXT UNIQUE NOT NULL,
                    hostname TEXT DEFAULT '',
                    os_info TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT DEFAULT ''
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_records (
                    device_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    input_tokens INT DEFAULT 0,
                    output_tokens INT DEFAULT 0,
                    reasoning_tokens INT DEFAULT 0,
                    cache_creation_tokens INT DEFAULT 0,
                    cache_read_tokens INT DEFAULT 0,
                    total_tokens INT DEFAULT 0,
                    total_cost REAL DEFAULT 0.0,
                    model_breakdowns TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (device_id, date)
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_records_date ON daily_records(date);"
            )


def create_device(display_name: str, db_path: str | None = None) -> dict[str, Any]:
    init_db(db_path)
    clean_name = display_name.strip() or "Unnamed-Device"
    created_at = utc_now_iso()
    with get_connection(db_path) as conn:
        while True:
            device_id = f"dev_{secrets.token_hex(3)}"
            api_token = f"ag_tok_{secrets.token_hex(12)}"
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO devices (device_id, display_name, api_token, hostname, os_info, created_at, last_seen_at)
                        VALUES (?, ?, ?, '', '', ?, '')
                        """,
                        (device_id, clean_name, api_token, created_at),
                    )
                break
            except sqlite3.IntegrityError:
                continue

    return {
        "device_id": device_id,
        "display_name": clean_name,
        "api_token": api_token,
        "hostname": "",
        "os_info": "",
        "created_at": created_at,
        "last_seen_at": "",
    }


def get_device_by_id(device_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        return dict(row) if row else None


def get_device_by_token(api_token: str, db_path: str | None = None) -> dict[str, Any] | None:
    if not api_token:
        return None
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE api_token = ?", (api_token,)
        ).fetchone()
        return dict(row) if row else None


def list_devices(
    now_dt: datetime | None = None, db_path: str | None = None
) -> list[dict[str, Any]]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM devices ORDER BY created_at ASC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["status"] = compute_device_status(d.get("last_seen_at", ""), now_dt=now_dt)
            result.append(d)
        return result


def delete_device(
    device_id: str, purge_data: bool = False, db_path: str | None = None
) -> bool:
    init_db(db_path)
    with get_connection(db_path) as conn:
        with conn:
            cur = conn.execute(
                "DELETE FROM devices WHERE device_id = ?", (device_id,)
            )
            if cur.rowcount == 0:
                return False
            if purge_data:
                conn.execute(
                    "DELETE FROM daily_records WHERE device_id = ?", (device_id,)
                )
            return True


def update_device_last_seen(
    device_id: str,
    last_seen_at: str,
    hostname: str = "",
    os_info: str = "",
    db_path: str | None = None,
) -> bool:
    init_db(db_path)
    with get_connection(db_path) as conn:
        with conn:
            cur = conn.execute(
                """
                UPDATE devices
                SET last_seen_at = ?,
                    hostname = CASE WHEN ? != '' THEN ? ELSE hostname END,
                    os_info = CASE WHEN ? != '' THEN ? ELSE os_info END
                WHERE device_id = ?
                """,
                (last_seen_at, hostname, hostname, os_info, os_info, device_id),
            )
            return cur.rowcount > 0


def _pick_int(obj: dict[str, Any], *keys: str) -> int:
    for k in keys:
        if k in obj and obj[k] is not None:
            try:
                return int(obj[k])
            except (ValueError, TypeError):
                pass
    return 0


def _pick_float(obj: dict[str, Any], *keys: str) -> float:
    for k in keys:
        if k in obj and obj[k] is not None:
            try:
                return float(obj[k])
            except (ValueError, TypeError):
                pass
    return 0.0


def _normalize_date(raw_date: Any) -> str:
    s = str(raw_date or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _normalize_model_breakdown(item: dict[str, Any]) -> dict[str, Any]:
    model_name = (
        item.get("modelName")
        or item.get("model_name")
        or item.get("model")
        or item.get("name")
        or "unknown"
    )
    inp = _pick_int(item, "inputTokens", "input_tokens")
    out = _pick_int(item, "outputTokens", "output_tokens")
    reas = _pick_int(
        item,
        "reasoningTokens",
        "reasoning_tokens",
        "thinkingTokens",
        "thinking_tokens",
    )
    cc = _pick_int(
        item,
        "cacheCreationTokens",
        "cacheCreationInputTokens",
        "cache_creation_tokens",
        "cache_creation_input_tokens",
    )
    cr = _pick_int(
        item,
        "cacheReadTokens",
        "cacheReadInputTokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
    )
    tot = _pick_int(item, "totalTokens", "total_tokens")
    computed_tot = inp + out + reas + cc + cr
    if tot == 0 and computed_tot > 0:
        tot = computed_tot
    cost = _pick_float(item, "cost", "totalCost", "total_cost", "costUSD")
    return {
        "model_name": str(model_name),
        "input_tokens": inp,
        "output_tokens": out,
        "reasoning_tokens": reas,
        "cache_creation_tokens": cc,
        "cache_read_tokens": cr,
        "total_tokens": tot,
        "cost": round(cost, 6),
    }


def extract_daily_entries(payload: Any) -> list[dict[str, Any]]:
    """
    Extract daily records from arbitrary ccusage / client JSON structures:
    - Direct list of daily entries: `[...]`
    - `{"daily": [...]}` or `{"data": {"daily": [...]}}` or `{"records": [...]}`
    - Nested `{"sections": {"daily": {"daily": [...]}}}` or `{"usage": {"daily": [...]}}`
    - Single day object `{"date": "2026-11-11", ...}`
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    # Check nested sections.daily.daily
    sections = payload.get("sections")
    if isinstance(sections, dict):
        sec_daily = sections.get("daily")
        if isinstance(sec_daily, list):
            return [x for x in sec_daily if isinstance(x, dict)]
        if isinstance(sec_daily, dict) and isinstance(sec_daily.get("daily"), list):
            return [x for x in sec_daily["daily"] if isinstance(x, dict)]

    # Check data wrapper
    data_field = payload.get("data")
    if isinstance(data_field, list):
        return [x for x in data_field if isinstance(x, dict)]
    if isinstance(data_field, dict):
        nested = extract_daily_entries(data_field)
        if nested:
            return nested

    # Check usage wrapper
    usage_field = payload.get("usage")
    if isinstance(usage_field, (dict, list)):
        nested = extract_daily_entries(usage_field)
        if nested:
            return nested

    for key in ("daily", "records", "days", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict) and isinstance(val.get("daily"), list):
            return [x for x in val["daily"] if isinstance(x, dict)]

    if "date" in payload:
        return [payload]

    return []


def normalize_daily_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    date_str = _normalize_date(raw.get("date"))
    if not date_str:
        return None

    raw_models = (
        raw.get("modelBreakdowns")
        or raw.get("model_breakdowns")
        or raw.get("models")
        or raw.get("breakdown")
        or []
    )
    if isinstance(raw_models, str):
        try:
            raw_models = json.loads(raw_models)
        except Exception:
            raw_models = []

    models: list[dict[str, Any]] = []
    if isinstance(raw_models, list):
        for m in raw_models:
            if isinstance(m, dict):
                models.append(_normalize_model_breakdown(m))
    elif isinstance(raw_models, dict):
        for m_name, m_val in raw_models.items():
            if isinstance(m_val, dict):
                m_copy = dict(m_val)
                m_copy.setdefault("modelName", m_name)
                models.append(_normalize_model_breakdown(m_copy))

    inp = _pick_int(raw, "inputTokens", "input_tokens")
    out = _pick_int(raw, "outputTokens", "output_tokens")
    reas = _pick_int(
        raw,
        "reasoningTokens",
        "reasoning_tokens",
        "thinkingTokens",
        "thinking_tokens",
    )
    cc = _pick_int(
        raw,
        "cacheCreationTokens",
        "cacheCreationInputTokens",
        "cache_creation_tokens",
        "cache_creation_input_tokens",
    )
    cr = _pick_int(
        raw,
        "cacheReadTokens",
        "cacheReadInputTokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
    )

    if inp == 0 and models:
        inp = sum(m["input_tokens"] for m in models)
    if out == 0 and models:
        out = sum(m["output_tokens"] for m in models)
    if reas == 0 and models:
        reas = sum(m["reasoning_tokens"] for m in models)
    if cc == 0 and models:
        cc = sum(m["cache_creation_tokens"] for m in models)
    if cr == 0 and models:
        cr = sum(m["cache_read_tokens"] for m in models)

    tot = _pick_int(raw, "totalTokens", "total_tokens")
    computed_tot = inp + out + reas + cc + cr
    if tot == 0 and computed_tot > 0:
        tot = computed_tot
    elif tot == 0 and models:
        tot = sum(m["total_tokens"] for m in models)

    cost = _pick_float(raw, "totalCost", "total_cost", "costUSD", "cost")
    if cost == 0.0 and models:
        cost = sum(m["cost"] for m in models)

    return {
        "date": date_str,
        "input_tokens": inp,
        "output_tokens": out,
        "reasoning_tokens": reas,
        "cache_creation_tokens": cc,
        "cache_read_tokens": cr,
        "total_tokens": tot,
        "total_cost": round(cost, 6),
        "model_breakdowns": models,
    }


def upsert_daily_records(
    device_id: str,
    raw_records: list[dict[str, Any]],
    hostname: str = "",
    os_info: str = "",
    seen_at: str | None = None,
    db_path: str | None = None,
) -> int:
    init_db(db_path)
    now_iso = seen_at or utc_now_iso()
    normalized_list: list[dict[str, Any]] = []
    for r in raw_records:
        norm = normalize_daily_record(r)
        if norm:
            normalized_list.append(norm)

    with get_connection(db_path) as conn:
        with conn:
            for item in normalized_list:
                conn.execute(
                    """
                    INSERT INTO daily_records (
                        device_id, date,
                        input_tokens, output_tokens, reasoning_tokens,
                        cache_creation_tokens, cache_read_tokens, total_tokens,
                        total_cost, model_breakdowns, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, date) DO UPDATE SET
                        input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens,
                        reasoning_tokens = excluded.reasoning_tokens,
                        cache_creation_tokens = excluded.cache_creation_tokens,
                        cache_read_tokens = excluded.cache_read_tokens,
                        total_tokens = excluded.total_tokens,
                        total_cost = excluded.total_cost,
                        model_breakdowns = excluded.model_breakdowns,
                        updated_at = excluded.updated_at
                    """,
                    (
                        device_id,
                        item["date"],
                        item["input_tokens"],
                        item["output_tokens"],
                        item["reasoning_tokens"],
                        item["cache_creation_tokens"],
                        item["cache_read_tokens"],
                        item["total_tokens"],
                        item["total_cost"],
                        json.dumps(item["model_breakdowns"], ensure_ascii=False),
                        now_iso,
                    ),
                )
            conn.execute(
                """
                UPDATE devices
                SET last_seen_at = ?,
                    hostname = CASE WHEN ? != '' THEN ? ELSE hostname END,
                    os_info = CASE WHEN ? != '' THEN ? ELSE os_info END
                WHERE device_id = ?
                """,
                (now_iso, hostname, hostname, os_info, os_info, device_id),
            )

    return len(normalized_list)


def get_aggregated_stats(
    device_filter: str = "all",
    now_dt: datetime | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    ref_dt = now_dt.astimezone(timezone.utc) if now_dt else datetime.now(timezone.utc)
    current_month_prefix = ref_dt.strftime("%Y-%m")
    sub_monthly_usd = get_sub_monthly_usd()

    with get_connection(db_path) as conn:
        device_rows = conn.execute(
            "SELECT * FROM devices ORDER BY created_at ASC"
        ).fetchall()

        per_device_totals = {
            r["device_id"]: {"total_tokens": 0, "total_cost": 0.0}
            for r in device_rows
        }
        dev_agg_rows = conn.execute(
            """
            SELECT device_id,
                   COALESCE(SUM(total_tokens), 0) AS sum_tokens,
                   COALESCE(SUM(total_cost), 0.0) AS sum_cost
            FROM daily_records
            GROUP BY device_id
            """
        ).fetchall()
        for r in dev_agg_rows:
            did = r["device_id"]
            if did in per_device_totals:
                per_device_totals[did] = {
                    "total_tokens": int(r["sum_tokens"] or 0),
                    "total_cost": round(float(r["sum_cost"] or 0.0), 4),
                }

        devices_out: list[dict[str, Any]] = []
        for r in device_rows:
            did = r["device_id"]
            last_seen = r["last_seen_at"] or ""
            totals = per_device_totals.get(did, {"total_tokens": 0, "total_cost": 0.0})
            devices_out.append(
                {
                    "device_id": did,
                    "display_name": r["display_name"],
                    "hostname": r["hostname"] or "",
                    "os_info": r["os_info"] or "",
                    "created_at": r["created_at"],
                    "last_seen_at": last_seen,
                    "status": compute_device_status(last_seen, now_dt=ref_dt),
                    "total_tokens": totals["total_tokens"],
                    "total_cost": totals["total_cost"],
                }
            )

        if device_filter and device_filter != "all":
            record_rows = conn.execute(
                "SELECT * FROM daily_records WHERE device_id = ? ORDER BY date ASC",
                (device_filter,),
            ).fetchall()
        else:
            record_rows = conn.execute(
                "SELECT * FROM daily_records ORDER BY date ASC"
            ).fetchall()

    # Aggregate daily and model statistics
    daily_map: dict[str, dict[str, Any]] = {}
    model_map: dict[str, dict[str, Any]] = {}

    total_input = 0
    total_output = 0
    total_reasoning = 0
    total_cache_create = 0
    total_cache_read = 0
    total_tokens = 0
    total_cost = 0.0
    current_month_cost = 0.0
    current_month_tokens = 0

    for row in record_rows:
        d_str = row["date"]
        inp = int(row["input_tokens"] or 0)
        out = int(row["output_tokens"] or 0)
        reas = int(row["reasoning_tokens"] or 0)
        cc = int(row["cache_creation_tokens"] or 0)
        cr = int(row["cache_read_tokens"] or 0)
        tot = int(row["total_tokens"] or 0)
        cost = float(row["total_cost"] or 0.0)

        total_input += inp
        total_output += out
        total_reasoning += reas
        total_cache_create += cc
        total_cache_read += cr
        total_tokens += tot
        total_cost += cost

        if d_str.startswith(current_month_prefix):
            current_month_cost += cost
            current_month_tokens += tot

        if d_str not in daily_map:
            daily_map[d_str] = {
                "date": d_str,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "_models": {},
            }

        day_entry = daily_map[d_str]
        day_entry["input_tokens"] += inp
        day_entry["output_tokens"] += out
        day_entry["reasoning_tokens"] += reas
        day_entry["cache_creation_tokens"] += cc
        day_entry["cache_read_tokens"] += cr
        day_entry["total_tokens"] += tot
        day_entry["total_cost"] = round(day_entry["total_cost"] + cost, 6)

        try:
            m_list = json.loads(row["model_breakdowns"] or "[]")
        except Exception:
            m_list = []

        if isinstance(m_list, list):
            for m in m_list:
                if not isinstance(m, dict):
                    continue
                norm_m = _normalize_model_breakdown(m)
                m_name = norm_m["model_name"]

                # Per-day model aggregation
                if m_name not in day_entry["_models"]:
                    day_entry["_models"][m_name] = {
                        "model_name": m_name,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": 0,
                        "total_tokens": 0,
                        "cost": 0.0,
                    }
                dm = day_entry["_models"][m_name]
                dm["input_tokens"] += norm_m["input_tokens"]
                dm["output_tokens"] += norm_m["output_tokens"]
                dm["reasoning_tokens"] += norm_m["reasoning_tokens"]
                dm["cache_creation_tokens"] += norm_m["cache_creation_tokens"]
                dm["cache_read_tokens"] += norm_m["cache_read_tokens"]
                dm["total_tokens"] += norm_m["total_tokens"]
                dm["cost"] = round(dm["cost"] + norm_m["cost"], 6)

                # Global model aggregation
                if m_name not in model_map:
                    model_map[m_name] = {
                        "model_name": m_name,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": 0,
                        "total_tokens": 0,
                        "total_cost": 0.0,
                    }
                gm = model_map[m_name]
                gm["input_tokens"] += norm_m["input_tokens"]
                gm["output_tokens"] += norm_m["output_tokens"]
                gm["reasoning_tokens"] += norm_m["reasoning_tokens"]
                gm["cache_creation_tokens"] += norm_m["cache_creation_tokens"]
                gm["cache_read_tokens"] += norm_m["cache_read_tokens"]
                gm["total_tokens"] += norm_m["total_tokens"]
                gm["total_cost"] = round(gm["total_cost"] + norm_m["cost"], 6)

    daily_series: list[dict[str, Any]] = []
    for d_key in sorted(daily_map.keys()):
        item = daily_map[d_key]
        models_for_day = sorted(
            item.pop("_models").values(),
            key=lambda x: (x["cost"], x["total_tokens"]),
            reverse=True,
        )
        item["model_breakdowns"] = models_for_day
        daily_series.append(item)

    models_ranked = sorted(
        model_map.values(),
        key=lambda x: (x["total_cost"], x["total_tokens"]),
        reverse=True,
    )
    for m in models_ranked:
        m["cost_share_pct"] = (
            round((m["total_cost"] / total_cost) * 100.0, 2) if total_cost > 0 else 0.0
        )
        m["token_share_pct"] = (
            round((m["total_tokens"] / total_tokens) * 100.0, 2)
            if total_tokens > 0
            else 0.0
        )

    cache_denom = total_input + total_cache_create + total_cache_read
    cache_hit_rate = (
        round((total_cache_read / cache_denom) * 100.0, 2) if cache_denom > 0 else 0.0
    )
    roi_multiplier = (
        round(current_month_cost / sub_monthly_usd, 2) if sub_monthly_usd > 0 else 0.0
    )
    roi_percent = (
        round((current_month_cost / sub_monthly_usd) * 100.0, 1)
        if sub_monthly_usd > 0
        else 0.0
    )
    active_days = sum(1 for d in daily_series if d["total_tokens"] > 0)

    status_counts = {"online": 0, "idle": 0, "offline": 0}
    for dev in devices_out:
        st = dev["status"]
        status_counts[st] = status_counts.get(st, 0) + 1

    return {
        "device_filter": device_filter or "all",
        "kpi": {
            "total_tokens": total_tokens,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "reasoning_tokens": total_reasoning,
            "cache_creation_tokens": total_cache_create,
            "cache_read_tokens": total_cache_read,
            "total_cost": round(total_cost, 4),
            "cache_hit_rate": cache_hit_rate,
            "current_month": current_month_prefix,
            "current_month_cost": round(current_month_cost, 4),
            "current_month_tokens": current_month_tokens,
            "sub_monthly_usd": sub_monthly_usd,
            "roi_multiplier": roi_multiplier,
            "roi_percent": roi_percent,
            "active_days": active_days,
        },
        "devices": devices_out,
        "device_status_counts": status_counts,
        "daily": daily_series,
        "models": models_ranked,
    }
