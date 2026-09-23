from __future__ import annotations

"""Validated runtime configuration with isolated data paths per environment."""

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class RuntimeConfigurationError(RuntimeError):
    """The process cannot start safely with the supplied environment."""


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

_ENVIRONMENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENVIRONMENTS = {"development", "test", "production"}
_PROCESS_ROLES = {"web", "worker", "maintenance"}
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
    runtime_path: Path
    allow_network_tasks: bool
    scheduler_enabled: bool
    durable_jobs_enabled: bool
    curated_feed_enabled: bool
    curation_read_enabled: bool
    curation_search_enabled: bool
    curation_hot_enabled: bool
    topic_statistics_enabled: bool
    report_read_enabled: bool
    report_write_enabled: bool
    api_catalog_enabled: bool
    api_key_rate_per_minute: int
    api_consumer_concurrency: int
    api_request_lease_seconds: int
    public_origin: str | None
    process_role: str
    legacy_data_layout: bool

    def public_manifest(self) -> dict:
        """Return non-secret labels safe for logs and local operator output."""
        value = asdict(self)
        for key in ("database_path", "blob_path", "backup_path", "runtime_path"):
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

    path_names = (
        "INFOHUB_DB_PATH", "INFOHUB_BLOB_PATH", "INFOHUB_BACKUP_PATH",
        "INFOHUB_RUNTIME_PATH",
    )
    if environment == "production":
        missing = [name for name in path_names if not values.get(name, "").strip()]
        if missing:
            raise RuntimeConfigurationError(
                "production requires explicit data paths: " + ", ".join(missing)
            )
        required_flags = (
            "INFOHUB_ALLOW_NETWORK_TASKS", "INFOHUB_ENABLE_SCHEDULER",
            "INFOHUB_DURABLE_JOBS_ENABLED", "INFOHUB_PROCESS_ROLE",
        )
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
    runtime_path = _path(
        values.get("INFOHUB_RUNTIME_PATH", str(default_root / "runtime")),
        base_dir,
        "INFOHUB_RUNTIME_PATH",
        environment == "production",
    )

    legacy_database = (base_dir / "data" / "app.db").resolve()
    if environment != "production" and database_path == legacy_database and not legacy_data_layout:
        raise RuntimeConfigurationError(
            "the legacy data/app.db path requires INFOHUB_LEGACY_DATA_LAYOUT=true"
        )

    data_directories = {
        "INFOHUB_BLOB_PATH": blob_path,
        "INFOHUB_BACKUP_PATH": backup_path,
        "INFOHUB_RUNTIME_PATH": runtime_path,
    }
    if database_path in set(data_directories.values()):
        raise RuntimeConfigurationError("the database path cannot also be a data directory")
    directory_items = list(data_directories.items())
    for index, (first_name, first_path) in enumerate(directory_items):
        if first_path in database_path.parents:
            raise RuntimeConfigurationError(
                f"the database file cannot be stored inside {first_name}"
            )
        for second_name, second_path in directory_items[index + 1:]:
            if first_path == second_path or first_path in second_path.parents or second_path in first_path.parents:
                raise RuntimeConfigurationError(
                    f"{first_name} and {second_name} must not contain one another"
                )

    allow_network_tasks = _boolean(values, "INFOHUB_ALLOW_NETWORK_TASKS", False)
    scheduler_enabled = _boolean(values, "INFOHUB_ENABLE_SCHEDULER", False)
    durable_jobs_enabled = _boolean(values, "INFOHUB_DURABLE_JOBS_ENABLED", False)
    curated_feed_enabled = _boolean(values, "INFOHUB_CURATED_FEED_ENABLED", False)
    curation_read_enabled = _boolean(values, "INFOHUB_CURATION_READ_ENABLED", False)
    curation_search_enabled = _boolean(values, "INFOHUB_CURATION_SEARCH_ENABLED", False)
    if curation_search_enabled and not curation_read_enabled:
        raise RuntimeConfigurationError(
            "INFOHUB_CURATION_SEARCH_ENABLED=true requires INFOHUB_CURATION_READ_ENABLED=true"
        )
    curation_hot_enabled = _boolean(values, "INFOHUB_CURATION_HOT_ENABLED", False)
    if curation_hot_enabled and not curation_read_enabled:
        raise RuntimeConfigurationError(
            "INFOHUB_CURATION_HOT_ENABLED=true requires INFOHUB_CURATION_READ_ENABLED=true"
        )
    topic_statistics_enabled = _boolean(
        values, "INFOHUB_TOPIC_STATISTICS_ENABLED", False
    )
    report_read_enabled = _boolean(values, "INFOHUB_REPORT_READ_ENABLED", False)
    report_write_enabled = _boolean(values, "INFOHUB_REPORT_WRITE_ENABLED", False)
    if report_write_enabled and not report_read_enabled:
        raise RuntimeConfigurationError(
            "INFOHUB_REPORT_WRITE_ENABLED=true requires INFOHUB_REPORT_READ_ENABLED=true"
        )
    api_catalog_enabled = _boolean(values, "INFOHUB_API_CATALOG_ENABLED", False)
    api_key_rate_per_minute = _integer(
        values, "INFOHUB_API_KEY_RATE_PER_MINUTE", 60, 1, 10_000
    )
    api_consumer_concurrency = _integer(
        values, "INFOHUB_API_CONSUMER_CONCURRENCY", 5, 1, 100
    )
    api_request_lease_seconds = _integer(
        values, "INFOHUB_API_REQUEST_LEASE_SECONDS", 300, 30, 3_600
    )
    raw_public_origin = values.get("INFOHUB_PUBLIC_ORIGIN", "").strip()
    if environment == "production" and not raw_public_origin:
        raise RuntimeConfigurationError(
            "INFOHUB_PUBLIC_ORIGIN is required in production and must be the private HTTPS URL"
        )
    public_origin = None
    if raw_public_origin:
        parsed_origin = urlsplit(raw_public_origin)
        try:
            has_port = parsed_origin.port is not None
        except ValueError as exc:
            raise RuntimeConfigurationError(
                "INFOHUB_PUBLIC_ORIGIN contains an invalid port"
            ) from exc
        if (
            parsed_origin.scheme != "https" or parsed_origin.username is not None
            or parsed_origin.password is not None or has_port
            or parsed_origin.path not in ("", "/") or parsed_origin.query
            or parsed_origin.fragment or not parsed_origin.hostname
            or not parsed_origin.hostname.lower().endswith(".ts.net")
        ):
            raise RuntimeConfigurationError(
                "INFOHUB_PUBLIC_ORIGIN must be an HTTPS *.ts.net origin without port, path, query, or fragment"
            )
        public_origin = f"https://{parsed_origin.hostname.lower()}"
    process_role = values.get("INFOHUB_PROCESS_ROLE", "web").strip().lower() or "web"
    if process_role not in _PROCESS_ROLES:
        raise RuntimeConfigurationError(
            f"INFOHUB_PROCESS_ROLE must be one of {sorted(_PROCESS_ROLES)}, got {process_role!r}"
        )
    if scheduler_enabled and not allow_network_tasks:
        raise RuntimeConfigurationError(
            "INFOHUB_ENABLE_SCHEDULER=true requires INFOHUB_ALLOW_NETWORK_TASKS=true"
        )
    if process_role == "web" and (allow_network_tasks or scheduler_enabled):
        raise RuntimeConfigurationError(
            "the web role must keep network tasks and the scheduler disabled"
        )
    if process_role == "worker" and not (
        allow_network_tasks and scheduler_enabled and durable_jobs_enabled
    ):
        raise RuntimeConfigurationError(
            "the worker role requires network tasks, scheduler and durable jobs"
        )
    if process_role == "maintenance" and scheduler_enabled:
        raise RuntimeConfigurationError("the maintenance role cannot run the scheduler")

    return RuntimeSettings(
        environment=environment,
        environment_id=environment_id,
        database_path=database_path,
        blob_path=blob_path,
        backup_path=backup_path,
        runtime_path=runtime_path,
        allow_network_tasks=allow_network_tasks,
        scheduler_enabled=scheduler_enabled,
        durable_jobs_enabled=durable_jobs_enabled,
        curated_feed_enabled=curated_feed_enabled,
        curation_read_enabled=curation_read_enabled,
        curation_search_enabled=curation_search_enabled,
        curation_hot_enabled=curation_hot_enabled,
        topic_statistics_enabled=topic_statistics_enabled,
        report_read_enabled=report_read_enabled,
        report_write_enabled=report_write_enabled,
        api_catalog_enabled=api_catalog_enabled,
        api_key_rate_per_minute=api_key_rate_per_minute,
        api_consumer_concurrency=api_consumer_concurrency,
        api_request_lease_seconds=api_request_lease_seconds,
        public_origin=public_origin,
        process_role=process_role,
        legacy_data_layout=legacy_data_layout,
    )


RUNTIME = load_runtime_settings()
ENVIRONMENT = RUNTIME.environment
ENVIRONMENT_ID = RUNTIME.environment_id
DB_PATH = RUNTIME.database_path
BLOB_PATH = RUNTIME.blob_path
BACKUP_PATH = RUNTIME.backup_path
RUNTIME_PATH = RUNTIME.runtime_path
ALLOW_NETWORK_TASKS = RUNTIME.allow_network_tasks
SCHEDULER_ENABLED = RUNTIME.scheduler_enabled
DURABLE_JOBS_ENABLED = RUNTIME.durable_jobs_enabled
CURATED_FEED_ENABLED = RUNTIME.curated_feed_enabled
CURATION_READ_ENABLED = RUNTIME.curation_read_enabled
CURATION_SEARCH_ENABLED = RUNTIME.curation_search_enabled
CURATION_HOT_ENABLED = RUNTIME.curation_hot_enabled
TOPIC_STATISTICS_ENABLED = RUNTIME.topic_statistics_enabled
REPORT_READ_ENABLED = RUNTIME.report_read_enabled
REPORT_WRITE_ENABLED = RUNTIME.report_write_enabled
API_CATALOG_ENABLED = RUNTIME.api_catalog_enabled
API_KEY_RATE_PER_MINUTE = RUNTIME.api_key_rate_per_minute
API_CONSUMER_CONCURRENCY = RUNTIME.api_consumer_concurrency
API_REQUEST_LEASE_SECONDS = RUNTIME.api_request_lease_seconds
PUBLIC_ORIGIN = RUNTIME.public_origin
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
AI_TICK_MINUTES = _integer(os.environ, "AI_TICK_MINUTES", 15, 1, 1440)
WORKER_POLL_SECONDS = _integer(os.environ, "WORKER_POLL_SECONDS", 2, 1, 60)
WORKER_HEARTBEAT_SECONDS = _integer(os.environ, "WORKER_HEARTBEAT_SECONDS", 10, 2, 60)
WORKER_HEARTBEAT_TTL_SECONDS = _integer(
    os.environ, "WORKER_HEARTBEAT_TTL_SECONDS", 45, 10, 600
)
WORKER_HEARTBEAT_FUTURE_TOLERANCE_SECONDS = _integer(
    os.environ, "WORKER_HEARTBEAT_FUTURE_TOLERANCE_SECONDS", 5, 0, 60
)
WORKER_LEASE_SECONDS = _integer(os.environ, "WORKER_LEASE_SECONDS", 300, 30, 3600)
PIPELINE_JOB_STALE_SECONDS = _integer(
    os.environ, "PIPELINE_JOB_STALE_SECONDS", 900, 60, 86_400
)
RAW_PAYLOAD_MAX_BYTES = _integer(
    os.environ, "RAW_PAYLOAD_MAX_BYTES", 2_097_152, 1_024, 104_857_600
)

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
