from __future__ import annotations

"""Single durable scheduler/worker process for production background tasks (two lanes)."""

import json
import logging
import os
import signal
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from uuid import uuid4

from . import config
from .db_admin import verify_database
from .jobs import (
    InputVersionChangedError,
    JobRecord,
    LeaseLostError,
    cancel_job,
    claim_job,
    complete_job,
    enqueue_due_schedules,
    fail_job,
    renew_lease,
    schedule_changed,
    upsert_interval_schedule,
)
from .redaction import redact
from .runtime_health import write_worker_heartbeat
from .timeutil import format_utc, utc_now


log = logging.getLogger(__name__)
JOB_KINDS = (
    "crawl", "ai", "reconcile", "report", "prune", "curation-search",
    "curation-hot", "topic-statistics", "sync-snapshot", "backup", "article-text",
)
# Succeeded routine jobs are kept a month for operations; fetch_log keeps 14 days.
JOB_RETENTION_DAYS = 30


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
        (
            "maintenance:backup", "backup",
            _next_daily(config.BACKUP_HOUR, config.BACKUP_MINUTE, current), 86_400, 5, 2,
            {"timezone": str(config.APP_TZ), "local_time": f"{config.BACKUP_HOUR:02d}:{config.BACKUP_MINUTE:02d}"},
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
    if config.CURATION_HOT_ENABLED:
        upsert_interval_schedule(
            schedule_id="curation-hot:refresh", kind="curation-hot",
            next_due_at=current, interval_seconds=60, priority=14,
            max_attempts=3,
        )
    else:
        # Previously queued work must also be guarded by the handler.
        from .database import get_db
        with get_db() as db:
            db.execute("""UPDATE schedules SET enabled=0,updated_at=?
                          WHERE id='curation-hot:refresh' AND enabled=1""", (utc_now(),))
    if config.ARTICLE_TEXT_ENABLED:
        upsert_interval_schedule(
            schedule_id="article-text:fetch", kind="article-text",
            next_due_at=current, interval_seconds=300, priority=12, max_attempts=2,
        )
    else:
        from .database import get_db
        with get_db() as db:
            db.execute("""UPDATE schedules SET enabled=0,updated_at=?
                          WHERE id='article-text:fetch' AND enabled=1""", (utc_now(),))
    if config.TOPIC_STATISTICS_ENABLED:
        upsert_interval_schedule(
            schedule_id="topic-statistics:refresh", kind="topic-statistics",
            next_due_at=current, interval_seconds=60, priority=13,
            max_attempts=3,
        )
    else:
        from .database import get_db
        with get_db() as db:
            db.execute("""UPDATE schedules SET enabled=0,updated_at=?
                          WHERE id='topic-statistics:refresh' AND enabled=1""", (utc_now(),))


def _crawl() -> dict:
    from .crawler.runner import run_due_sources
    from .external_heartbeat import report_cycle
    result = run_due_sources()
    report_cycle()  # only a finished cycle counts as alive
    return result


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
    if config.REPORT_WRITE_ENABLED:
        from .report_schedule import generate_scheduled_report
        return generate_scheduled_report()
    from .report_schedule import generate_legacy_scheduled_report
    return generate_legacy_scheduled_report()


def _prune() -> dict:
    from .database import get_db
    from .jobs import prune_succeeded_jobs
    now = datetime.now(timezone.utc)
    cutoff = format_utc(now - timedelta(days=14))
    with get_db() as db:
        deleted = db.execute("DELETE FROM fetch_log WHERE ran_at < ?", (cutoff,)).rowcount
    with get_db() as db:
        jobs_deleted = prune_succeeded_jobs(
            db, finished_before=now - timedelta(days=JOB_RETENTION_DAYS))
    from .sync_retention import cleanup_expired_snapshot_files
    snapshots = cleanup_expired_snapshot_files(dry_run=False)
    if snapshots.refused:
        raise RuntimeError(
            f"sync snapshot retention refused {snapshots.refused} path(s)"
        )
    return {"fetch_logs_deleted": deleted, "routine_jobs_deleted": jobs_deleted,
            "sync_snapshots": snapshots.to_dict()}


def _article_text() -> dict:
    from .crawler.article_text import fetch_article_texts
    return fetch_article_texts()


def _backup() -> dict:
    from .scheduled_backup import run_nightly_backup
    return run_nightly_backup()


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


def _curation_hot_refresh() -> dict:
    if not config.CURATION_HOT_ENABLED:
        return {"status": "disabled", "batches": 0}
    from .curation_hot_metrics import advance_hot_metrics
    report = None
    batches = 0
    for _ in range(10):
        report = advance_hot_metrics(100)
        batches += 1
        if report.status == "ready" and report.dirty_remaining == 0:
            break
    return {"batches": batches, **report.to_dict()}


def _topic_statistics_refresh() -> dict:
    if not config.TOPIC_STATISTICS_ENABLED:
        return {"status": "disabled", "batches": 0}
    from .topic_statistics import advance_topic_statistics
    report = None
    batches = 0
    for _ in range(10):
        report = advance_topic_statistics(25)
        batches += 1
        if report.status != "building":
            break
    return {"batches": batches, **report.to_dict()}


def _sync_snapshot(job: JobRecord, *, now: datetime | str | None = None) -> dict:
    from .sync_snapshots import build_sync_snapshot
    snapshot_id = job.payload.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("sync snapshot job requires snapshot_id")
    current = format_utc(now) if isinstance(now, datetime) else now
    return build_sync_snapshot(
        snapshot_id, job_id=job.id, lease_token=job.lease_token or "", now=current
    ).to_dict()


def default_handlers() -> dict[str, Callable[[], object]]:
    return {
        "crawl": _crawl,
        "ai": _ai,
        "reconcile": _reconcile,
        "report": _report,
        "prune": _prune,
        "backup": _backup,
        "article-text": _article_text,
        "curation-search": _curation_search_refresh,
        "curation-hot": _curation_hot_refresh,
        "topic-statistics": _topic_statistics_refresh,
    }


def _result_reference(value: object) -> str:
    return _safe_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), 4000
    )


def _safe_text(value: str, maximum: int) -> str:
    return redact(value.replace("\r", " ").replace("\n", " "))[:maximum]


def execute_claimed_job(
    job: JobRecord,
    *,
    handlers: Mapping[str, Callable[[], object]] | None = None,
    now: datetime | str | None = None,
) -> JobRecord:
    handler = (handlers or default_handlers()).get(job.kind)
    if handlers is None and job.kind == "sync-snapshot":
        handler = lambda: _sync_snapshot(job, now=now)
    if handler is None:
        return fail_job(
            job.id, job.lease_token or "", error_code="unknown_job_kind",
            error_detail=f"no handler for {job.kind}", now=now,
        )
    if schedule_changed(job):
        # Queued before a deploy or .env change; the reconfigured schedule queues its own job.
        log.info("job %s (%s) cancelled: its schedule changed", job.id, job.kind)
        return cancel_job(job.id, lease_token=job.lease_token, now=now)
    try:
        result = handler()
        return complete_job(
            job.id,
            job.lease_token or "",
            expected_input_version=job.input_version,
            result_ref=_result_reference(result),
            now=now,
        )
    except InputVersionChangedError:
        log.info("job %s (%s) cancelled: its schedule changed while it ran", job.id, job.kind)
        return cancel_job(job.id, lease_token=job.lease_token, now=now)
    except LeaseLostError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("job %s (%s) failed with %s", job.id, job.kind, type(exc).__name__)
        failed = fail_job(
            job.id,
            job.lease_token or "",
            error_code=f"handler_{type(exc).__name__.lower()}"[:100],
            error_detail=_safe_text(str(exc), 1000),
            now=now,
        )
        if job.kind == "sync-snapshot" and failed.state in {"failed", "dead_letter"}:
            from .sync_snapshots import fail_sync_snapshot
            snapshot_id = job.payload.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id:
                fail_sync_snapshot(
                    snapshot_id,
                    error_code=f"handler_{type(exc).__name__.lower()}"[:100],
                    now=now if isinstance(now, str) else None,
                )
        return failed


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
    """Publishes the worker heartbeat and renews the lease of the job each lane is running."""

    def __init__(self, worker_id: str, started_at: str):
        self.worker_id = worker_id
        self.started_at = started_at
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.active_jobs: dict[str, JobRecord] = {}
        self.lane_states: dict[str, str] = {}
        self.state = "running"
        self.thread = threading.Thread(target=self._run, name="worker-heartbeat", daemon=True)

    @property
    def active_job(self) -> JobRecord | None:
        with self.lock:
            return self.active_jobs.get("main") or next(iter(self.active_jobs.values()), None)

    def start(self) -> None:
        self._write("running")
        self.thread.start()

    def set_active(self, job: JobRecord | None, lane: str = "main") -> None:
        with self.lock:
            if job is None:
                self.active_jobs.pop(lane, None)
            else:
                self.active_jobs[lane] = job
        self._write()

    def set_state(self, state: str, lane: str = "main") -> None:
        """A lane reports running or degraded; the worker is degraded while any lane is."""
        with self.lock:
            self.lane_states[lane] = state
            self.state = "degraded" if "degraded" in self.lane_states.values() else state
        self._write()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=config.WORKER_HEARTBEAT_SECONDS + 2)
        self._write("stopped")

    def _snapshot(self) -> dict[str, JobRecord]:
        with self.lock:
            return dict(self.active_jobs)

    def _write(self, state: str | None = None) -> None:
        active = self._snapshot()
        with self.lock:
            current_state = state or self.state
        main = active.get("main") or next(iter(active.values()), None)
        write_worker_heartbeat(
            worker_id=self.worker_id,
            started_at=self.started_at,
            state=current_state,
            active_job_id=main.id if main else None,
            active_job_ids={lane: job.id for lane, job in sorted(active.items())},
        )

    def _run(self) -> None:
        while not self.stop_event.wait(config.WORKER_HEARTBEAT_SECONDS):
            for lane, active in self._snapshot().items():
                if not active.lease_token:
                    continue
                try:
                    renewed = renew_lease(
                        active.id,
                        active.lease_token,
                        lease_seconds=config.WORKER_LEASE_SECONDS,
                    )
                    with self.lock:
                        current = self.active_jobs.get(lane)
                        if current and current.id == active.id:
                            self.active_jobs[lane] = renewed
                except LeaseLostError:
                    log.error("worker lost lease for job %s", active.id)
                except Exception:  # noqa: BLE001
                    log.exception("could not renew job lease")
            try:
                self._write()
            except Exception:  # noqa: BLE001
                log.exception("could not publish worker heartbeat")


# Crawls run in a lane of their own, so a long job in the main lane (an AI tick calling the
# model, the nightly backup, a reindex after a topic change) no longer holds them back: flash
# feeds list only their latest items, and a crawl held back long enough misses some. The crawl
# code already runs sources on eight threads; the lane adds one more thread with its own
# database connections. Each lane claims only its own kinds of job.
CRAWL_LANE_KINDS = ("crawl",)
MAIN_LANE_KINDS = tuple(kind for kind in JOB_KINDS if kind not in CRAWL_LANE_KINDS)


def run_lane(
    *,
    lane: str,
    kinds: tuple[str, ...],
    worker_id: str,
    pump: HeartbeatPump,
    stopping: threading.Event,
    enqueue: bool = False,
    handlers: Mapping[str, Callable[[], object]] | None = None,
) -> None:
    """Claim and run jobs of ``kinds`` until ``stopping`` is set, finishing the job in hand."""
    while not stopping.is_set():
        try:
            if enqueue:
                enqueue_due_schedules()
            job = claim_job(
                worker_id=worker_id,
                kinds=kinds,
                lease_seconds=config.WORKER_LEASE_SECONDS,
            )
            pump.set_state("running", lane)
            if job is None:
                stopping.wait(config.WORKER_POLL_SECONDS)
                continue
            pump.set_active(job, lane)
            try:
                execute_claimed_job(job, handlers=handlers)
            except LeaseLostError:
                log.exception("job %s lost its lease", job.id)
            finally:
                pump.set_active(None, lane)
        except Exception:  # noqa: BLE001
            log.exception("worker %s lane failed; retrying", lane)
            try:
                pump.set_state("degraded", lane)
            except Exception:  # noqa: BLE001
                log.exception("could not publish degraded worker state")
            stopping.wait(config.WORKER_POLL_SECONDS)


def run_lanes(
    *,
    worker_id: str,
    pump: HeartbeatPump,
    stopping: threading.Event,
    handlers: Mapping[str, Callable[[], object]] | None = None,
) -> None:
    """Run the crawl lane on a thread and the main lane here until ``stopping`` is set."""
    crawl = threading.Thread(
        target=run_lane, name="worker-crawl", daemon=True,
        kwargs=dict(lane="crawl", kinds=CRAWL_LANE_KINDS, worker_id=f"{worker_id}:crawl",
                    pump=pump, stopping=stopping, handlers=handlers),
    )
    crawl.start()
    try:
        run_lane(lane="main", kinds=MAIN_LANE_KINDS, worker_id=worker_id, pump=pump,
                 stopping=stopping, enqueue=True, handlers=handlers)
    finally:
        stopping.set()
        crawl.join()  # a crawl in hand finishes, as the main lane's job does


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
    if config.SEC_USER_AGENT.endswith("@example.com"):
        log.warning("SEC_USER_AGENT still uses the example contact; SEC asks for a real name and email")
    try:
        run_lanes(worker_id=worker_id, pump=pump, stopping=stopping)
    finally:
        pump.close()
