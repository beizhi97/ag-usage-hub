import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import ensure_admin_key
from server.db import (
    compute_device_status,
    get_aggregated_stats,
    init_db,
    update_device_last_seen,
)
from server.main import app


@pytest.fixture()
def test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "test_usage.db"
    key_file = tmp_path / "admin.key"
    admin_key = "test_super_secret_admin_key_123456"

    monkeypatch.setenv("AG_DB_PATH", str(db_file))
    monkeypatch.setenv("AG_ADMIN_KEY_FILE", str(key_file))
    monkeypatch.setenv("AG_ADMIN_KEY", admin_key)
    monkeypatch.setenv("AG_SUB_MONTHLY_USD", "200")
    monkeypatch.setenv("AG_LOCK_VIEW", "false")

    init_db(str(db_file))
    client = TestClient(app)
    return {
        "client": client,
        "admin_key": admin_key,
        "db_path": str(db_file),
        "key_file": key_file,
    }


def test_1_admin_auth_and_device_lifecycle(test_env):
    """1. 管理员密钥校验、动态新建客户端（POST /api/devices）与吊销客户端闭环测试。"""
    client: TestClient = test_env["client"]
    admin_key: str = test_env["admin_key"]

    # Missing X-Admin-Key -> 401
    res_no_key = client.post("/api/devices", json={"display_name": "Workstation"})
    assert res_no_key.status_code == 401

    # Wrong X-Admin-Key -> 403
    res_bad_key = client.post(
        "/api/devices",
        headers={"X-Admin-Key": "wrong_key"},
        json={"display_name": "Workstation"},
    )
    assert res_bad_key.status_code == 403

    # Valid X-Admin-Key -> 200 & device created
    res_create = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "主力台式机", "origin": "https://ag.example.com"},
    )
    assert res_create.status_code == 200
    data = res_create.json()
    assert data["device_id"].startswith("dev_")
    assert data["api_token"].startswith("ag_tok_")
    assert len(data["api_token"]) == len("ag_tok_") + 24
    assert data["display_name"] == "主力台式机"
    assert (
        f'curl -fsSL "https://ag.example.com/i/{data["api_token"]}.sh" | bash'
        in data["agent_prompt"]
    )
    assert (
        f'irm "https://ag.example.com/i/{data["api_token"]}.ps1" | iex'
        in data["agent_prompt"]
    )

    device_id = data["device_id"]
    api_token = data["api_token"]

    # Re-fetch prompt via GET /api/devices/{device_id}/prompt
    res_prompt = client.get(
        f"/api/devices/{device_id}/prompt?origin=https://hub.internal",
        headers={"X-Admin-Key": admin_key},
    )
    assert res_prompt.status_code == 200
    assert (
        f"https://hub.internal/i/{api_token}.sh"
        in res_prompt.json()["agent_prompt"]
    )

    # Revoke device via DELETE /api/devices/{device_id}
    res_del = client.delete(
        f"/api/devices/{device_id}?purge_data=true",
        headers={"X-Admin-Key": admin_key},
    )
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "deleted"

    # Reporting with revoked token must return 403
    res_report = client.post(
        "/api/report",
        headers={"Authorization": f"Bearer {api_token}"},
        json={"daily": [{"date": "2026-11-11", "totalTokens": 1000, "totalCost": 0.05}]},
    )
    assert res_report.status_code == 403


def test_2_dynamic_installer_script_rendering(test_env):
    """2. 动态安装脚本渲染测试：请求 /i/{token}.sh?origin=https://ag.example.com 与 .ps1，验证返回 200 且脚本注入准确，非法 token 返回 404。"""
    client: TestClient = test_env["client"]
    admin_key: str = test_env["admin_key"]

    res_create = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "MacBook-Pro"},
    )
    token = res_create.json()["api_token"]

    # Linux/macOS .sh installer
    res_sh = client.get(f"/i/{token}.sh?origin=https://ag.example.com")
    assert res_sh.status_code == 200
    assert "text/plain" in res_sh.headers["content-type"]
    sh_body = res_sh.text
    assert "https://ag.example.com/api/report" in sh_body
    assert token in sh_body
    assert "last_sync_date" in sh_body
    assert "Persistent=true" in sh_body

    # Windows .ps1 installer
    res_ps1 = client.get(f"/i/{token}.ps1?origin=https://ag.example.com")
    assert res_ps1.status_code == 200
    assert "text/plain" in res_ps1.headers["content-type"]
    ps1_body = res_ps1.text
    assert "https://ag.example.com/api/report" in ps1_body
    assert token in ps1_body
    assert "last_sync_date" in ps1_body
    assert "StartWhenAvailable" in ps1_body

    # Test X-Forwarded-Proto & X-Forwarded-Host fallback when ?origin is omitted
    res_fwd = client.get(
        f"/i/{token}.sh",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "proxy.example.org",
        },
    )
    assert res_fwd.status_code == 200
    assert "https://proxy.example.org/api/report" in res_fwd.text

    # Invalid token -> 404
    assert client.get("/i/ag_tok_invalid0000000000000000.sh").status_code == 404
    assert client.get("/i/ag_tok_invalid0000000000000000.ps1").status_code == 404


def test_3_heterogeneous_ccusage_json_and_gzip_report(test_env):
    """3. 异构 ccusage JSON（驼峰/下划线字段 + sections.daily.daily）+ Gzip 压缩请求上报测试。"""
    client: TestClient = test_env["client"]
    admin_key: str = test_env["admin_key"]

    token = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Linux-DevBox"},
    ).json()["api_token"]

    # Heterogeneous payload with nested sections.daily.daily, camelCase & snake_case mixed
    payload = {
        "hostname": "ubuntu-dev-01",
        "os_info": "Linux 6.8.0",
        "sections": {
            "daily": {
                "daily": [
                    {
                        "date": "20261110",  # YYYYMMDD format
                        "inputTokens": 120000,
                        "outputTokens": 30000,
                        "reasoningTokens": 10000,
                        "cacheCreationInputTokens": 40000,
                        "cacheReadInputTokens": 800000,
                        "totalTokens": 1000000,
                        "totalCost": 12.5,
                        "modelBreakdowns": [
                            {
                                "modelName": "claude-sonnet-4-20250514",
                                "inputTokens": 120000,
                                "outputTokens": 30000,
                                "reasoningTokens": 10000,
                                "cacheCreationTokens": 40000,
                                "cacheReadTokens": 800000,
                                "totalTokens": 1000000,
                                "cost": 12.5,
                            }
                        ],
                    },
                    {
                        "date": "2026-11-11",  # YYYY-MM-DD snake_case format
                        "input_tokens": 50000,
                        "output_tokens": 20000,
                        "reasoning_tokens": 5000,
                        "cache_creation_tokens": 25000,
                        "cache_read_tokens": 400000,
                        "total_tokens": 500000,
                        "total_cost": 7.5,
                        "model_breakdowns": [
                            {
                                "model_name": "gemini-2.5-pro",
                                "input_tokens": 50000,
                                "output_tokens": 20000,
                                "reasoning_tokens": 5000,
                                "cache_creation_tokens": 25000,
                                "cache_read_tokens": 400000,
                                "total_tokens": 500000,
                                "total_cost": 7.5,
                            }
                        ],
                    },
                ]
            }
        },
    }

    compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
    res = client.post(
        "/api/report",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        content=compressed,
    )
    assert res.status_code == 200
    assert res.json()["upserted_days"] == 2

    stats = client.get("/api/stats?device=all").json()
    kpi = stats["kpi"]
    assert kpi["total_tokens"] == 1500000
    assert kpi["input_tokens"] == 170000
    assert kpi["output_tokens"] == 50000
    assert kpi["reasoning_tokens"] == 15000
    assert kpi["cache_creation_tokens"] == 65000
    assert kpi["cache_read_tokens"] == 1200000
    assert kpi["total_cost"] == pytest.approx(1.6938)
    assert len(stats["models"]) == 2
    assert stats["devices"][0]["hostname"] == "ubuntu-dev-01"


def test_4_idempotent_upsert_and_multi_device_aggregation(test_env):
    """4. 同一设备同日多次上报的幂等覆盖（防累加翻倍）测试，以及多设备汇总（device=all vs 单设备过滤）准确性测试。"""
    client: TestClient = test_env["client"]
    admin_key: str = test_env["admin_key"]

    dev_a = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Device-A"},
    ).json()
    dev_b = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Device-B"},
    ).json()

    # Device A first report for 2026-11-11
    client.post(
        "/api/report",
        headers={"Authorization": f"Bearer {dev_a['api_token']}"},
        json={
            "daily": [
                {
                    "date": "2026-11-11",
                    "inputTokens": 1000,
                    "outputTokens": 500,
                    "totalTokens": 1500,
                    "totalCost": 1.0,
                }
            ]
        },
    )

    # Device A second report on same day (updated total to 2500, NOT 1500+2500)
    client.post(
        "/api/report",
        headers={"Authorization": f"Bearer {dev_a['api_token']}"},
        json={
            "daily": [
                {
                    "date": "2026-11-11",
                    "inputTokens": 1800,
                    "outputTokens": 700,
                    "totalTokens": 2500,
                    "totalCost": 2.5,
                }
            ]
        },
    )

    # Device B report on same day (3000 tokens, $3.0)
    client.post(
        "/api/report",
        headers={"Authorization": f"Bearer {dev_b['api_token']}"},
        json={
            "daily": [
                {
                    "date": "2026-11-11",
                    "inputTokens": 2000,
                    "outputTokens": 1000,
                    "totalTokens": 3000,
                    "totalCost": 3.0,
                }
            ]
        },
    )

    # Filter Device A only -> must be 2500 tokens, $2.5 (idempotent overwrite verified)
    stats_a = client.get(f"/api/stats?device={dev_a['device_id']}").json()
    assert stats_a["kpi"]["total_tokens"] == 2500
    assert stats_a["kpi"]["total_cost"] == pytest.approx(2.5)

    # Filter Device B only -> must be 3000 tokens, $3.0
    stats_b = client.get(f"/api/stats?device={dev_b['device_id']}").json()
    assert stats_b["kpi"]["total_tokens"] == 3000
    assert stats_b["kpi"]["total_cost"] == pytest.approx(3.0)

    # Aggregate all devices -> must be 2500 + 3000 = 5500 tokens, $5.5
    stats_all = client.get("/api/stats?device=all").json()
    assert stats_all["kpi"]["total_tokens"] == 5500
    assert stats_all["kpi"]["total_cost"] == pytest.approx(5.5)
    assert len(stats_all["daily"]) == 1
    assert stats_all["daily"][0]["total_tokens"] == 5500


def test_5_device_online_idle_offline_state_machine(test_env):
    """5. 设备 online / idle / offline 状态机判定测试。"""
    client: TestClient = test_env["client"]
    admin_key: str = test_env["admin_key"]
    db_path: str = test_env["db_path"]

    ref_now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)

    # Unit check on compute_device_status
    assert compute_device_status("", now_dt=ref_now) == "offline"
    assert (
        compute_device_status(
            (ref_now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            now_dt=ref_now,
        )
        == "online"
    )
    assert (
        compute_device_status(
            (ref_now - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            now_dt=ref_now,
        )
        == "online"
    )
    assert (
        compute_device_status(
            (ref_now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            now_dt=ref_now,
        )
        == "idle"
    )
    assert (
        compute_device_status(
            (ref_now - timedelta(hours=26)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            now_dt=ref_now,
        )
        == "offline"
    )

    # Integration check via DB & get_aggregated_stats
    d_online = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Online-Host"},
    ).json()["device_id"]
    d_idle = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Idle-Host"},
    ).json()["device_id"]
    d_offline = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Offline-Host"},
    ).json()["device_id"]
    d_never = client.post(
        "/api/devices",
        headers={"X-Admin-Key": admin_key},
        json={"display_name": "Never-Seen-Host"},
    ).json()["device_id"]

    update_device_last_seen(
        d_online,
        (ref_now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        db_path=db_path,
    )
    update_device_last_seen(
        d_idle,
        (ref_now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        db_path=db_path,
    )
    update_device_last_seen(
        d_offline,
        (ref_now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        db_path=db_path,
    )

    agg = get_aggregated_stats(now_dt=ref_now, db_path=db_path)
    status_by_id = {d["device_id"]: d["status"] for d in agg["devices"]}
    assert status_by_id[d_online] == "online"
    assert status_by_id[d_idle] == "idle"
    assert status_by_id[d_offline] == "offline"
    assert status_by_id[d_never] == "offline"
    assert agg["device_status_counts"] == {"online": 1, "idle": 1, "offline": 2}


def test_6_auto_generated_admin_key_and_dashboard_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """验证首次未传入 AG_ADMIN_KEY 时自动生成并持久化至 admin.key，以及 /health 与 / 看板路由。"""
    import server.config as cfg

    cfg._CACHED_ADMIN_KEY = None
    key_file = tmp_path / "data" / "admin.key"
    db_file = tmp_path / "data" / "usage.db"
    monkeypatch.delenv("AG_ADMIN_KEY", raising=False)
    monkeypatch.setenv("AG_ADMIN_KEY_FILE", str(key_file))
    monkeypatch.setenv("AG_DB_PATH", str(db_file))

    generated = ensure_admin_key()
    assert len(generated) == 32
    assert key_file.exists()
    assert key_file.read_text(encoding="utf-8").strip() == generated

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "+ 新建客户端" in res_index.text
