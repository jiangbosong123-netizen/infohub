from __future__ import annotations

"""Atomic process heartbeats shared by the web and worker containers."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config
from .timeutil import format_utc, parse_utc


WORKER_HEARTBEAT_FILE = "worker-heartbeat.json"


@dataclass(frozen=True)
class WorkerHeartbeatStatus:
    status: str
    healthy: bool
    detail: str
    heartbeat_at: str | None = None
    age_seconds: int | None = None
    version: str | None = None
    worker_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "healthy": self.healthy,
            "detail": self.detail,
            "heartbeat_at": self.heartbeat_at,
            "age_seconds": self.age_seconds,
            "version": self.version,
            "worker_id": self.worker_id,
        }


def worker_heartbeat_path(runtime_path: Path | None = None) -> Path:
    return Path(runtime_path or config.RUNTIME_PATH) / WORKER_HEARTBEAT_FILE


def clear_worker_heartbeat(runtime_path: Path | None = None) -> None:
    """Remove a previous release heartbeat before starting the new process pair."""
    worker_heartbeat_path(runtime_path).unlink(missing_ok=True)


def write_worker_heartbeat(
    *,
    worker_id: str,
    started_at: str,
    state: str = "running",
    active_job_id: str | None = None,
    heartbeat_at: datetime | str | None = None,
    runtime_path: Path | None = None,
) -> dict:
    target = worker_heartbeat_path(runtime_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    observed = (
        format_utc(heartbeat_at)
        if isinstance(heartbeat_at, datetime)
        else heartbeat_at or format_utc(datetime.now(timezone.utc))
    )
    payload = {
        "schema_version": 1,
        "role": "worker",
        "environment_id": config.ENVIRONMENT_ID,
        "version": config.APP_VERSION,
        "worker_id": worker_id,
        "pid": os.getpid(),
        "started_at": started_at,
        "heartbeat_at": observed,
        "state": state,
        "active_job_id": active_job_id,
    }
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def read_worker_heartbeat(
    *,
    expected_version: str | None = None,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
    runtime_path: Path | None = None,
) -> WorkerHeartbeatStatus:
    target = worker_heartbeat_path(runtime_path)
    expected = expected_version or config.APP_VERSION
    maximum = max_age_seconds or config.WORKER_HEARTBEAT_TTL_SECONDS
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WorkerHeartbeatStatus("missing", False, "worker heartbeat is missing")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return WorkerHeartbeatStatus("invalid", False, "worker heartbeat is unreadable")
    if value.get("schema_version") != 1 or value.get("role") != "worker":
        return WorkerHeartbeatStatus("invalid", False, "worker heartbeat schema is invalid")
    heartbeat_at = value.get("heartbeat_at")
    try:
        observed = parse_utc(heartbeat_at)
    except (AttributeError, TypeError, ValueError):
        return WorkerHeartbeatStatus("invalid", False, "worker heartbeat time is invalid")
    age = max(0, int((current - observed).total_seconds()))
    future_seconds = max(0, int((observed - current).total_seconds()))
    common = {
        "heartbeat_at": format_utc(observed),
        "age_seconds": age,
        "version": str(value.get("version") or ""),
        "worker_id": str(value.get("worker_id") or "") or None,
    }
    if value.get("environment_id") != config.ENVIRONMENT_ID:
        return WorkerHeartbeatStatus(
            "wrong_environment", False, "worker belongs to a different environment", **common
        )
    if value.get("version") != expected:
        return WorkerHeartbeatStatus(
            "wrong_version", False, "worker is not running the web release version", **common
        )
    if value.get("state") != "running":
        return WorkerHeartbeatStatus(
            "not_running", False, f"worker state is {value.get('state')!r}", **common
        )
    if future_seconds > config.WORKER_HEARTBEAT_FUTURE_TOLERANCE_SECONDS:
        return WorkerHeartbeatStatus(
            "future", False, "worker heartbeat is ahead of the web clock", **common
        )
    if age > maximum:
        return WorkerHeartbeatStatus("stale", False, "worker heartbeat is stale", **common)
    return WorkerHeartbeatStatus("healthy", True, "worker heartbeat is fresh", **common)
