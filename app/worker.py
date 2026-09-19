from __future__ import annotations

"""Single durable scheduler/worker process for production background tasks."""

import json
import logging
import os
import re
import signal
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from uuid import uuid4

from . import config
from .db_admin import verify_database
from .jobs import (
    JobRecord,
    LeaseLostError,
    claim_job,
    complete_job,
    enqueue_due_schedules,
    fail_job,
    renew_lease,
    upsert_interval_schedule,
)
from .runtime_health import write_worker_heartbeat
from .timeutil import format_utc, utc_now


log = logging.getLogger(__name__)
JOB_KINDS = ("crawl", "ai", "reconcile", "report", "prune", "curation-search")


def _next_daily(hour: int, minute: int, now: datetime) -> datetime:
    local = now.astimezone(config.APP_TZ)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def register_default_schedules(now: datetime | None = None) -> None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    definitions = (
        ("crawl:due-sources", "crawl", current, config.CRAWL_TICK_MINUTES * 60, 50, 4, {}),
        ("ai:pending", "ai", current, config.AI_TICK_MINUTES * 60, 30, 3, {}),
        (
            "reconcile:daily", "reconcile",
            _next_daily(config.RECONCILE_HOUR, config.RECONCILE_MINUTE, current),
            86_400, 20, 3,
            {"timezone": str(config.APP_TZ), "local_time": f"{config.RECONCILE_HOUR:02d}:{config.RECONCILE_MINUTE:02d}"},
        ),
        (
            "report:daily", "report",
            _next_daily(config.REPORT_HOUR, config.REPORT_MINUTE, current),
            86_400, 10, 3,
            {"timezone": str(config.APP_TZ), "local_time": f"{config.REPORT_HOUR:02d}:{config.REPORT_MINUTE:02d}"},
        ),
        (
            "maintenance:prune", "prune", _next_daily(4, 5, current), 86_400, 0, 3,
            {"timezone": str(config.APP_TZ), "local_time": "04:05"},
        ),
    )
    for schedule_id, kind, due, interval, priority, attempts, payload in definitions:
        upsert_interval_schedule(
            schedule_id=schedule_id,
            kind=kind,
            next_due_at=due,
            interval_seconds=interval,
            payload=payload,
            priority=priority,
            max_attempts=attempts,
        )
    if config.CURATION_SEARCH_ENABLED:
        upsert_interval_schedule(
            schedule_id="curation-search:refresh", kind="curation-search",
            next_due_at=current, interval_seconds=60, priority=15,
            max_attempts=3,
        )
    else:
        # A prior deployment may have enabled it. Queued jobs are harmless
        # because the handler also checks the flag before touching the index.
        from .database import get_db
        with get_db() as db:
            db.execute("""UPDATE schedules SET enabled=0,updated_at=?
                          WHERE id='curation-search:refresh' AND enabled=1""", (utc_now(),))


def _crawl() -> dict:
    from .crawler.runner import run_due_sources
    return run_due_sources()


def _ai() -> dict:
    from .ai.pipeline import backfill_titles, backfill_tmt, process_pending
    total = 0
    for _ in range(12):
        count = process_pending(limit=30)
        total += count
        if count < 30:
            break
    judged = backfill_tmt(max_batches=12)
    translated = backfill_titles(max_batches=12)
    from .stories import refresh_derived
    derived = refresh_derived()
    return {
        "processed": total,
        "tmt": judged,
        "translated": translated,
        "derived": derived,
        "llm_enabled": config.llm_enabled(),
    }


def _reconcile() -> dict:
    from .crawler.googlenews import run_reconcile
    return run_reconcile()


def _report() -> dict:
    from .ai.daily import generate_daily
    return {"date": generate_daily()}


def _prune() -> dict:
    from .database import get_db
    cutoff = format_utc(datetime.now(timezone.utc) - timedelta(days=14))
    with get_db() as db:
        deleted = db.execute("DELETE FROM fetch_log WHERE ran_at < ?", (cutoff,)).rowcount
    return {"deleted": deleted}


def _curation_search_refresh() -> dict:
    if not config.CURATION_SEARCH_ENABLED:
        return {"status": "disabled", "batches": 0}
    from .curation_search import advance_search_index
    report = None
    batches = 0
    for _ in range(10):
        report = advance_search_index(500)
        batches += 1
        if report.status == "ready" and report.dirty_remaining == 0:
            break
    return {"batches": batches, **report.to_dict()}


def default_handlers() -> dict[str, Callable[[], object]]:
    return {
        "crawl": _crawl,
        "ai": _ai,
        "reconcile": _reconcile,
        "report": _report,
        "prune": _prune,
        "curation-search": _curation_search_refresh,
    }


def _result_reference(value: object) -> str:
    return _safe_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), 4000
    )


def _safe_text(value: str, maximum: int) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    if config.LLM_API_KEY:
        text = text.replace(config.LLM_API_KEY, "[redacted]")
    text = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
        "authorization=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|password|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return text[:maximum]


def execute_claimed_job(
    job: JobRecord,
    *,
    handlers: Mapping[str, Callable[[], object]] | None = None,
    now: datetime | str | None = None,
) -> JobRecord:
    handler = (handlers or default_handlers()).get(job.kind)
    if handler is None:
        return fail_job(
            job.id, job.lease_token or "", error_code="unknown_job_kind",
            error_detail=f"no handler for {job.kind}", now=now,
        )
    try:
        result = handler()
        return complete_job(
            job.id,
            job.lease_token or "",
            expected_input_version=job.input_version,
            result_ref=_result_reference(result),
            now=now,
        )
    except LeaseLostError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("job %s (%s) failed with %s", job.id, job.kind, type(exc).__name__)
        return fail_job(
            job.id,
            job.lease_token or "",
            error_code=f"handler_{type(exc).__name__.lower()}"[:100],
            error_detail=_safe_text(str(exc), 1000),
            now=now,
        )


def process_one_job(
    *,
    worker_id: str,
    handlers: Mapping[str, Callable[[], object]] | None = None,
    now: datetime | str | None = None,
    lease_seconds: int | None = None,
) -> JobRecord | None:
    job = claim_job(
        worker_id=worker_id,
        kinds=JOB_KINDS,
        lease_seconds=lease_seconds or config.WORKER_LEASE_SECONDS,
        now=now,
    )
    if job is None:
        return None
    return execute_claimed_job(job, handlers=handlers, now=now)


class HeartbeatPump:
    def __init__(self, worker_id: str, started_at: str):
        self.worker_id = worker_id
        self.started_at = started_at
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.active_job: JobRecord | None = None
        self.state = "running"
        self.thread = threading.Thread(target=self._run, name="worker-heartbeat", daemon=True)

    def start(self) -> None:
        self._write("running")
        self.thread.start()

    def set_active(self, job: JobRecord | None) -> None:
        with self.lock:
            self.active_job = job
        self._write()

    def set_state(self, state: str) -> None:
        with self.lock:
            self.state = state
        self._write()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=config.WORKER_HEARTBEAT_SECONDS + 2)
        self._write("stopped")

    def _snapshot(self) -> JobRecord | None:
        with self.lock:
            return self.active_job

    def _write(self, state: str | None = None) -> None:
        active = self._snapshot()
        with self.lock:
            current_state = state or self.state
        write_worker_heartbeat(
            worker_id=self.worker_id,
            started_at=self.started_at,
            state=current_state,
            active_job_id=active.id if active else None,
        )

    def _run(self) -> None:
        while not self.stop_event.wait(config.WORKER_HEARTBEAT_SECONDS):
            active = self._snapshot()
            if active and active.lease_token:
                try:
                    renewed = renew_lease(
                        active.id,
                        active.lease_token,
                        lease_seconds=config.WORKER_LEASE_SECONDS,
                    )
                    with self.lock:
                        if self.active_job and self.active_job.id == active.id:
                            self.active_job = renewed
                except LeaseLostError:
                    log.error("worker lost lease for job %s", active.id)
                except Exception:  # noqa: BLE001
                    log.exception("could not renew job lease")
            try:
                self._write()
            except Exception:  # noqa: BLE001
                log.exception("could not publish worker heartbeat")


def run_worker() -> None:
    if config.PROCESS_ROLE != "worker":
        raise config.RuntimeConfigurationError("worker command requires INFOHUB_PROCESS_ROLE=worker")
    config.require_network_tasks("worker")
    if not config.DURABLE_JOBS_ENABLED or not config.SCHEDULER_ENABLED:
        raise config.RuntimeConfigurationError("worker requires durable jobs and scheduler")
    verify_database(config.DB_PATH, require_current=True)
    register_default_schedules()

    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    started_at = format_utc(datetime.now(timezone.utc))
    stopping = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stopping.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    pump = HeartbeatPump(worker_id, started_at)
    pump.start()
    log.info("worker %s started for %s at version %s", worker_id, config.ENVIRONMENT_ID, config.APP_VERSION)
    try:
        while not stopping.is_set():
            try:
                enqueue_due_schedules()
                job = claim_job(
                    worker_id=worker_id,
                    kinds=JOB_KINDS,
                    lease_seconds=config.WORKER_LEASE_SECONDS,
                )
                pump.set_state("running")
                if job is None:
                    stopping.wait(config.WORKER_POLL_SECONDS)
                    continue
                pump.set_active(job)
                try:
                    execute_claimed_job(job)
                except LeaseLostError:
                    log.exception("job %s lost its lease", job.id)
                finally:
                    pump.set_active(None)
            except Exception:  # noqa: BLE001
                log.exception("worker loop failed; retrying")
                try:
                    pump.set_state("degraded")
                except Exception:  # noqa: BLE001
                    log.exception("could not publish degraded worker state")
                stopping.wait(config.WORKER_POLL_SECONDS)
    finally:
        pump.close()
