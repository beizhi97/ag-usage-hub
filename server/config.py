import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("ag_usage_hub")

_CACHED_ADMIN_KEY: str | None = None


def get_db_path() -> str:
    return os.environ.get("AG_DB_PATH", "./data/usage.db")


def get_admin_key_file() -> Path:
    custom_key_file = os.environ.get("AG_ADMIN_KEY_FILE")
    if custom_key_file:
        return Path(custom_key_file)
    db_path = Path(get_db_path())
    return db_path.parent / "admin.key"


def ensure_admin_key() -> str:
    """Return AG_ADMIN_KEY from env or load/generate from ./data/admin.key."""
    global _CACHED_ADMIN_KEY
    env_key = os.environ.get("AG_ADMIN_KEY", "").strip()
    if env_key:
        _CACHED_ADMIN_KEY = env_key
        return env_key

    key_file = get_admin_key_file()
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            _CACHED_ADMIN_KEY = stored
            return stored

    if _CACHED_ADMIN_KEY:
        return _CACHED_ADMIN_KEY

    generated = secrets.token_hex(16)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    _CACHED_ADMIN_KEY = generated
    msg = f"[ag-usage-hub] Generated new AG_ADMIN_KEY (saved to {key_file}): {generated}"
    print(msg, flush=True)
    logger.warning(msg)
    return generated


def get_admin_key() -> str:
    return ensure_admin_key()


def get_public_url() -> str:
    return os.environ.get("AG_PUBLIC_URL", "").strip().rstrip("/")


def get_sub_monthly_usd() -> float:
    raw = os.environ.get("AG_SUB_MONTHLY_USD", "200").strip()
    try:
        val = float(raw)
        return val if val > 0 else 200.0
    except ValueError:
        return 200.0


def is_view_locked() -> bool:
    val = os.environ.get("AG_LOCK_VIEW", "false").strip().lower()
    return val in ("1", "true", "yes", "on")
