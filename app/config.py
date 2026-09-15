from __future__ import annotations

"""Validated runtime configuration with isolated data paths per environment."""

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class RuntimeConfigurationError(RuntimeError):
    """The process cannot start safely with the supplied environment."""


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

_ENVIRONMENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENVIRONMENTS = {"development", "test", "production"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuntimeConfigurationError(
        f"{name} must be one of {sorted(_TRUE_VALUES | _FALSE_VALUES)}, got {raw!r}"
    )


def _integer(
    environ: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise RuntimeConfigurationError(
            f"{name} must be between {minimum} and {maximum}, got {value}"
        )
    return value


def _path(raw: str, base_dir: Path, name: str, require_absolute: bool) -> Path:
    value = Path(raw).expanduser()
    if require_absolute and not value.is_absolute():
        raise RuntimeConfigurationError(f"{name} must be an absolute path in production")
    if not value.is_absolute():
        value = base_dir / value
    return value.resolve()


@dataclass(frozen=True)
class RuntimeSettings:
    environment: str
    environment_id: str
    database_path: Path
    blob_path: Path
    backup_path: Path
    allow_network_tasks: bool
    scheduler_enabled: bool
    legacy_data_layout: bool

    @property
    def process_role(self) -> str:
        return "combined" if self.scheduler_enabled else "web"

    def public_manifest(self) -> dict:
        """Return non-secret labels safe for logs and local operator output."""
        value = asdict(self)
        for key in ("database_path", "blob_path", "backup_path"):
            value[key] = str(value[key])
        value["process_role"] = self.process_role
        return value


def load_runtime_settings(
    environ: Mapping[str, str] | None = None, base_dir: Path = BASE_DIR
) -> RuntimeSettings:
    values = os.environ if environ is None else environ
    environment = values.get("INFOHUB_ENVIRONMENT", "development").strip().lower()
    if environment not in _ENVIRONMENTS:
        raise RuntimeConfigurationError(
            f"INFOHUB_ENVIRONMENT must be one of {sorted(_ENVIRONMENTS)}, got {environment!r}"
        )

    raw_environment_id = values.get("INFOHUB_ENVIRONMENT_ID", "").strip().lower()
    if environment == "production" and not raw_environment_id:
        raise RuntimeConfigurationError("INFOHUB_ENVIRONMENT_ID is required in production")
    environment_id = raw_environment_id or f"{environment}-local"
    if not _ENVIRONMENT_ID.fullmatch(environment_id):
        raise RuntimeConfigurationError(
            "INFOHUB_ENVIRONMENT_ID must start with a lower-case letter or digit and "
            "contain only lower-case letters, digits, '_' or '-' (maximum 64 characters)"
        )

    legacy_data_layout = _boolean(values, "INFOHUB_LEGACY_DATA_LAYOUT", False)
    if environment == "production" and legacy_data_layout:
        raise RuntimeConfigurationError(
            "INFOHUB_LEGACY_DATA_LAYOUT is a temporary local compatibility option, not a production layout"
        )

    path_names = ("INFOHUB_DB_PATH", "INFOHUB_BLOB_PATH", "INFOHUB_BACKUP_PATH")
    if environment == "production":
        missing = [name for name in path_names if not values.get(name, "").strip()]
        if missing:
            raise RuntimeConfigurationError(
                "production requires explicit data paths: " + ", ".join(missing)
            )
        required_flags = ("INFOHUB_ALLOW_NETWORK_TASKS", "INFOHUB_ENABLE_SCHEDULER")
        missing_flags = [name for name in required_flags if not values.get(name, "").strip()]
        if missing_flags:
            raise RuntimeConfigurationError(
                "production requires explicit task flags: " + ", ".join(missing_flags)
            )

    default_root = (
        base_dir / "data"
        if legacy_data_layout
        else base_dir / ".runtime" / environment_id
    )
    database_path = _path(
        values.get("INFOHUB_DB_PATH", str(default_root / "app.db")),
        base_dir,
        "INFOHUB_DB_PATH",
        environment == "production",
    )
    blob_path = _path(
        values.get("INFOHUB_BLOB_PATH", str(default_root / "blobs")),
        base_dir,
        "INFOHUB_BLOB_PATH",
        environment == "production",
    )
    backup_path = _path(
        values.get("INFOHUB_BACKUP_PATH", str(default_root / "backups")),
        base_dir,
        "INFOHUB_BACKUP_PATH",
        environment == "production",
    )

    legacy_database = (base_dir / "data" / "app.db").resolve()
    if environment != "production" and database_path == legacy_database and not legacy_data_layout:
        raise RuntimeConfigurationError(
            "the legacy data/app.db path requires INFOHUB_LEGACY_DATA_LAYOUT=true"
        )

    if database_path in {blob_path, backup_path}:
        raise RuntimeConfigurationError("the database path cannot also be a blob or backup directory")
    if blob_path == backup_path or blob_path in backup_path.parents or backup_path in blob_path.parents:
        raise RuntimeConfigurationError("blob and backup directories must not contain one another")
    if blob_path in database_path.parents or backup_path in database_path.parents:
        raise RuntimeConfigurationError("the database file cannot be stored inside blob or backup directories")

    allow_network_tasks = _boolean(values, "INFOHUB_ALLOW_NETWORK_TASKS", False)
    scheduler_enabled = _boolean(values, "INFOHUB_ENABLE_SCHEDULER", False)
    if scheduler_enabled and not allow_network_tasks:
        raise RuntimeConfigurationError(
            "INFOHUB_ENABLE_SCHEDULER=true requires INFOHUB_ALLOW_NETWORK_TASKS=true"
        )

    return RuntimeSettings(
        environment=environment,
        environment_id=environment_id,
        database_path=database_path,
        blob_path=blob_path,
        backup_path=backup_path,
        allow_network_tasks=allow_network_tasks,
        scheduler_enabled=scheduler_enabled,
        legacy_data_layout=legacy_data_layout,
    )


RUNTIME = load_runtime_settings()
ENVIRONMENT = RUNTIME.environment
ENVIRONMENT_ID = RUNTIME.environment_id
DB_PATH = RUNTIME.database_path
BLOB_PATH = RUNTIME.blob_path
BACKUP_PATH = RUNTIME.backup_path
ALLOW_NETWORK_TASKS = RUNTIME.allow_network_tasks
SCHEDULER_ENABLED = RUNTIME.scheduler_enabled
PROCESS_ROLE = RUNTIME.process_role

WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.yaml"

try:
    APP_TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Shanghai"))
except ZoneInfoNotFoundError as exc:
    raise RuntimeConfigurationError(f"APP_TZ is not a known time zone: {exc}") from exc

# LLM (OpenAI-compatible provider). Secrets are never included in the manifest.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()

CRAWL_TICK_MINUTES = _integer(os.environ, "CRAWL_TICK_MINUTES", 5, 1, 1440)
RECONCILE_HOUR = _integer(os.environ, "RECONCILE_HOUR", 6, 0, 23)
RECONCILE_MINUTE = _integer(os.environ, "RECONCILE_MINUTE", 30, 0, 59)
REPORT_HOUR = _integer(os.environ, "REPORT_HOUR", 8, 0, 23)
REPORT_MINUTE = _integer(os.environ, "REPORT_MINUTE", 0, 0, 59)

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "personal-news-aggregator admin@example.com"
).strip()
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEB_PORT = _integer(os.environ, "PORT", 8000, 1, 65535)
APP_VERSION = os.getenv("APP_VERSION", "unknown").strip() or "unknown"


def require_network_tasks(action: str) -> None:
    if not ALLOW_NETWORK_TASKS:
        raise RuntimeConfigurationError(
            f"{action} is disabled in environment {ENVIRONMENT_ID!r}; set "
            "INFOHUB_ALLOW_NETWORK_TASKS=true only for an intentional network run"
        )


def llm_enabled() -> bool:
    return bool(ALLOW_NETWORK_TASKS and LLM_API_KEY and LLM_BASE_URL and LLM_MODEL)
