from __future__ import annotations

"""Durable at-least-once jobs with idempotent enqueueing and fenced leases."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping
from uuid import uuid4

from . import config
from .database import get_db
from .timeutil import format_utc, parse_utc, utc_now


class JobError(RuntimeError):
    """Base class for invalid durable-job operations."""


class IdempotencyConflictError(JobError):
    """An idempotency key was reused for a different logical request."""


class LeaseLostError(JobError):
    """A worker attempted to update a job after losing its lease."""


class InputVersionChangedError(JobError):
    """The worker result was produced for a different input version."""


class JobEnvironmentError(JobError):
    """The job database belongs to a different runtime environment."""


@dataclass(frozen=True)
class JobRecord:
    id: str
    kind: str
    subject_id: str | None
    input_version: str | None
    payload: dict
    idempotency_key: str
    dataset_epoch: str | None
    state: str
    priority: int
    scheduled_for: str
    next_attempt_at: str
    lease_owner: str | None
    lease_token: str | None
    lease_generation: int
    lease_expires_at: str | None
    heartbeat_at: str | None
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_detail: str | None
    result_ref: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical_json(payload: Mapping | None) -> str:
    return json.dumps(
        payload or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_request(
    kind: str,
    subject_id: str | None,
    input_version: str | None,
    payload_json: str,
    priority: int,
    scheduled_for: str,
    max_attempts: int,
) -> str:
    value = json.dumps(
        {
            "kind": kind,
            "subject_id": subject_id,
            "input_version": input_version,
            "payload": json.loads(payload_json),
            "priority": priority,
            "scheduled_for": scheduled_for,
            "max_attempts": max_attempts,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_required(value: str, field: str, maximum: int = 200) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return cleaned


def _job_from_row(row) -> JobRecord:
    logical_key = (
        row["logical_idempotency_key"]
        if "logical_idempotency_key" in row.keys() and row["logical_idempotency_key"]
        else row["idempotency_key"]
    )
    return JobRecord(
        id=row["id"],
        kind=row["kind"],
        subject_id=row["subject_id"],
        input_version=row["input_version"],
        payload=json.loads(row["payload_json"]),
        idempotency_key=logical_key,
        dataset_epoch=row["dataset_epoch"] if "dataset_epoch" in row.keys() else None,
        state=row["state"],
        priority=row["priority"],
        scheduled_for=row["scheduled_for"],
        next_attempt_at=row["next_attempt_at"],
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_generation=row["lease_generation"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        result_ref=row["result_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _normalize_time(value: datetime | str | None) -> str:
    if value is None:
        return utc_now()
    if isinstance(value, str):
        return format_utc(parse_utc(value))
    return format_utc(value)


def _idempotency_scope(db, logical_key: str) -> tuple[str | None, str]:
    dataset_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dataset_state'"
    ).fetchone()
    if not dataset_table:
        return None, logical_key
    state = db.execute(
        """SELECT current_epoch,owner_environment_id
           FROM dataset_state WHERE singleton=1"""
    ).fetchone()
    if not state:
        raise JobEnvironmentError("dataset identity has not been initialized")
    if state["owner_environment_id"] != config.ENVIRONMENT_ID:
        raise JobEnvironmentError(
            f"database belongs to environment {state['owner_environment_id']!r}, "
            f"not {config.ENVIRONMENT_ID!r}"
        )
    epoch = state["current_epoch"]
    return epoch, f"{epoch}:{logical_key}"


def _find_existing_job(db, logical_key: str, epoch: str | None, storage_key: str):
    if epoch is None:
        return db.execute(
            "SELECT * FROM jobs WHERE idempotency_key=?", (storage_key,)
        ).fetchone()
    return db.execute(
        """SELECT * FROM jobs
           WHERE dataset_epoch=? AND logical_idempotency_key=?""",
        (epoch, logical_key),
    ).fetchone()


def _enqueue(
    db,
    *,
    kind: str,
    idempotency_key: str,
    subject_id: str | None,
    input_version: str | None,
    payload: Mapping | None,
    priority: int,
    scheduled_for: str,
    max_attempts: int,
    created_at: str,
) -> JobRecord:
    clean_kind = _clean_required(kind, "kind")
    clean_key = _clean_required(idempotency_key, "idempotency_key", 500)
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    payload_json = _canonical_json(payload)
    dataset_epoch, storage_key = _idempotency_scope(db, clean_key)
    request_hash = _hash_request(
        clean_kind,
        subject_id,
        input_version,
        payload_json,
        priority,
        scheduled_for,
        max_attempts,
    )
    existing = _find_existing_job(db, clean_key, dataset_epoch, storage_key)
    if existing:
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                f"idempotency key {clean_key!r} belongs to a different request"
            )
        return _job_from_row(existing)

    job_id = str(uuid4())
    if dataset_epoch:
        db.execute(
            """INSERT INTO jobs(
                   id,kind,subject_id,input_version,payload_json,request_hash,idempotency_key,
                   dataset_epoch,idempotency_scope,logical_idempotency_key,state,priority,
                   scheduled_for,next_attempt_at,max_attempts,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)""",
            (
                job_id, clean_kind, subject_id, input_version, payload_json, request_hash,
                storage_key, dataset_epoch, dataset_epoch, clean_key, priority, scheduled_for,
                scheduled_for, max_attempts, created_at, created_at,
            ),
        )
    else:
        db.execute(
            """INSERT INTO jobs(
                   id,kind,subject_id,input_version,payload_json,request_hash,idempotency_key,
                   state,priority,scheduled_for,next_attempt_at,max_attempts,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)""",
            (
                job_id, clean_kind, subject_id, input_version, payload_json, request_hash,
                clean_key, priority, scheduled_for, scheduled_for, max_attempts, created_at,
                created_at,
            ),
        )
    return _job_from_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def enqueue_job(
    *,
    kind: str,
    idempotency_key: str,
    subject_id: str | None = None,
    input_version: str | None = None,
    payload: Mapping | None = None,
    priority: int = 0,
    scheduled_for: datetime | str | None = None,
    max_attempts: int = 3,
) -> JobRecord:
    created = utc_now()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if scheduled_for is None:
            clean_key = _clean_required(idempotency_key, "idempotency_key", 500)
            epoch, storage_key = _idempotency_scope(db, clean_key)
            existing = _find_existing_job(db, clean_key, epoch, storage_key)
            due = existing["scheduled_for"] if existing else created
        else:
            due = _normalize_time(scheduled_for)
        return _enqueue(
            db,
            kind=kind,
            idempotency_key=idempotency_key,
            subject_id=subject_id,
            input_version=input_version,
            payload=payload,
            priority=priority,
            scheduled_for=due,
            max_attempts=max_attempts,
            created_at=created,
        )


def get_job(job_id: str) -> JobRecord | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def _expire_leases(db, now: str) -> None:
    rows = db.execute(
        """SELECT id,lease_token,attempt_count,max_attempts FROM jobs
           WHERE state='running' AND lease_expires_at<=?""",
        (now,),
    ).fetchall()
    for row in rows:
        db.execute(
            """UPDATE job_attempts SET status='lease_expired',finished_at=?,
                      error_code='lease_expired'
               WHERE job_id=? AND lease_token=? AND status='running'""",
            (now, row["id"], row["lease_token"]),
        )
        exhausted = row["attempt_count"] >= row["max_attempts"]
        db.execute(
            """UPDATE jobs SET state=?,lease_owner=NULL,lease_token=NULL,
                      lease_expires_at=NULL,next_attempt_at=?,error_code='lease_expired',
                      updated_at=?,finished_at=? WHERE id=?""",
            (
                "dead_letter" if exhausted else "retry_wait",
                now,
                now,
                now if exhausted else None,
                row["id"],
            ),
        )


def claim_job(
    *,
    worker_id: str,
    kinds: Iterable[str] = (),
    lease_seconds: int = 120,
    now: datetime | str | None = None,
) -> JobRecord | None:
    clean_worker = _clean_required(worker_id, "worker_id")
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    current = _normalize_time(now)
    expires = format_utc(parse_utc(current) + timedelta(seconds=lease_seconds))
    clean_kinds = tuple(dict.fromkeys(_clean_required(kind, "kind") for kind in kinds))

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire_leases(db, current)
        where = "state IN ('pending','retry_wait') AND scheduled_for<=? AND next_attempt_at<=?"
        params: list = [current, current]
        dataset_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dataset_state'"
        ).fetchone()
        if dataset_table:
            state = db.execute(
                """SELECT current_epoch,owner_environment_id
                   FROM dataset_state WHERE singleton=1"""
            ).fetchone()
            if state["owner_environment_id"] != config.ENVIRONMENT_ID:
                raise JobEnvironmentError(
                    f"database belongs to environment {state['owner_environment_id']!r}, "
                    f"not {config.ENVIRONMENT_ID!r}"
                )
            current_epoch = state["current_epoch"]
            where += " AND dataset_epoch=?"
            params.append(current_epoch)
        if clean_kinds:
            where += f" AND kind IN ({','.join('?' * len(clean_kinds))})"
            params.extend(clean_kinds)
        row = db.execute(
            f"""SELECT * FROM jobs WHERE {where}
                ORDER BY priority DESC,next_attempt_at,scheduled_for,created_at,id LIMIT 1""",
            params,
        ).fetchone()
        if not row:
            return None

        lease_token = str(uuid4())
        generation = row["lease_generation"] + 1
        attempt_number = row["attempt_count"] + 1
        updated = db.execute(
            """UPDATE jobs SET state='running',lease_owner=?,lease_token=?,
                      lease_generation=?,lease_expires_at=?,attempt_count=?,
                      heartbeat_at=?,error_code=NULL,error_detail=NULL,updated_at=?,finished_at=NULL
               WHERE id=? AND state IN ('pending','retry_wait')
                     AND lease_generation=?""",
            (
                clean_worker,
                lease_token,
                generation,
                expires,
                attempt_number,
                current,
                current,
                row["id"],
                row["lease_generation"],
            ),
        )
        if updated.rowcount != 1:
            raise LeaseLostError("job changed while claiming its lease")
        db.execute(
            """INSERT INTO job_attempts(
                   id,job_id,attempt_number,worker_id,lease_token,started_at,status
               ) VALUES(?,?,?,?,?,?,'running')""",
            (str(uuid4()), row["id"], attempt_number, clean_worker, lease_token, current),
        )
        return _job_from_row(
            db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        )


def _current_lease(db, job_id: str, lease_token: str, current: str):
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["state"] != "running" or row["lease_token"] != lease_token:
        raise LeaseLostError("job lease is no longer current")
    if row["lease_expires_at"] <= current:
        raise LeaseLostError("job lease has expired")
    return row


def renew_lease(
    job_id: str,
    lease_token: str,
    *,
    lease_seconds: int = 120,
    now: datetime | str | None = None,
) -> JobRecord:
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    current = _normalize_time(now)
    expires = format_utc(parse_utc(current) + timedelta(seconds=lease_seconds))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _current_lease(db, job_id, lease_token, current)
        db.execute(
            """UPDATE jobs SET lease_expires_at=?,heartbeat_at=?,updated_at=?
               WHERE id=? AND lease_token=?""",
            (expires, current, current, job_id, lease_token),
        )
        return _job_from_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def _validate_input_version(db, row, expected_input_version: str | None) -> str | None:
    if row["input_version"] != expected_input_version:
        raise InputVersionChangedError("job input version changed before completion")
    try:
        schedule_id = json.loads(row["payload_json"]).get("schedule_id")
    except (AttributeError, json.JSONDecodeError):
        schedule_id = None
    if schedule_id:
        schedule = db.execute(
            "SELECT config_hash FROM schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        if not schedule or schedule["config_hash"] != row["input_version"]:
            raise InputVersionChangedError(
                "schedule configuration changed before completion"
            )
    return schedule_id


def _complete_job_in_transaction(
    db,
    job_id: str,
    lease_token: str,
    *,
    expected_input_version: str | None = None,
    result_ref: str | None = None,
    current: str,
) -> JobRecord:
    existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if (
        existing
        and existing["state"] == "succeeded"
        and existing["completed_lease_token"] == lease_token
    ):
        if (
            existing["input_version"] != expected_input_version
            or existing["result_ref"] != result_ref
        ):
            raise IdempotencyConflictError(
                "completion retry does not match the published result"
            )
        return _job_from_row(existing)
    row = _current_lease(db, job_id, lease_token, current)
    schedule_id = _validate_input_version(db, row, expected_input_version)
    db.execute(
        """UPDATE job_attempts SET status='succeeded',finished_at=?,result_ref=?
           WHERE job_id=? AND lease_token=? AND status='running'""",
        (current, result_ref, job_id, lease_token),
    )
    db.execute(
        """UPDATE jobs SET state='succeeded',lease_owner=NULL,lease_token=NULL,
                  lease_expires_at=NULL,result_ref=?,completed_lease_token=?,
                  updated_at=?,finished_at=? WHERE id=?""",
        (result_ref, lease_token, current, current, job_id),
    )
    if schedule_id:
        db.execute(
            """UPDATE schedules SET last_success_at=?,updated_at=?
               WHERE id=? AND config_hash=?""",
            (current, current, schedule_id, row["input_version"]),
        )
    return _job_from_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def complete_job(
    job_id: str,
    lease_token: str,
    *,
    expected_input_version: str | None = None,
    result_ref: str | None = None,
    now: datetime | str | None = None,
) -> JobRecord:
    current = _normalize_time(now)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        return _complete_job_in_transaction(
            db,
            job_id,
            lease_token,
            expected_input_version=expected_input_version,
            result_ref=result_ref,
            current=current,
        )


def fail_job(
    job_id: str,
    lease_token: str,
    *,
    error_code: str,
    error_detail: str = "",
    retry_at: datetime | str | None = None,
    now: datetime | str | None = None,
) -> JobRecord:
    clean_error = _clean_required(error_code, "error_code", 100)
    current = _normalize_time(now)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _current_lease(db, job_id, lease_token, current)
        exhausted = row["attempt_count"] >= row["max_attempts"]
        if exhausted:
            next_attempt = current
            next_state = "dead_letter"
        else:
            next_attempt = _normalize_time(
                retry_at
                or (
                    parse_utc(current)
                    + timedelta(seconds=min(60 * 2 ** (row["attempt_count"] - 1), 3600))
                )
            )
            next_state = "retry_wait"
        detail = error_detail[:1000]
        db.execute(
            """UPDATE job_attempts SET status='failed',finished_at=?,error_code=?,error_detail=?
               WHERE job_id=? AND lease_token=? AND status='running'""",
            (current, clean_error, detail, job_id, lease_token),
        )
        db.execute(
            """UPDATE jobs SET state=?,lease_owner=NULL,lease_token=NULL,
                      lease_expires_at=NULL,next_attempt_at=?,error_code=?,error_detail=?,
                      updated_at=?,finished_at=? WHERE id=?""",
            (
                next_state,
                next_attempt,
                clean_error,
                detail,
                current,
                current if exhausted else None,
                job_id,
            ),
        )
        return _job_from_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def block_job(
    job_id: str,
    lease_token: str,
    *,
    error_code: str,
    error_detail: str = "",
    now: datetime | str | None = None,
) -> JobRecord:
    clean_error = _clean_required(error_code, "error_code", 100)
    current = _normalize_time(now)
    detail = error_detail[:1000]
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _current_lease(db, job_id, lease_token, current)
        db.execute(
            """UPDATE job_attempts SET status='blocked',finished_at=?,error_code=?,error_detail=?
               WHERE job_id=? AND lease_token=? AND status='running'""",
            (current, clean_error, detail, job_id, lease_token),
        )
        db.execute(
            """UPDATE jobs SET state='blocked',lease_owner=NULL,lease_token=NULL,
                      lease_expires_at=NULL,error_code=?,error_detail=?,updated_at=?,finished_at=?
               WHERE id=?""",
            (clean_error, detail, current, current, job_id),
        )
        return _job_from_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def cancel_job(
    job_id: str,
    *,
    lease_token: str | None = None,
    now: datetime | str | None = None,
) -> JobRecord:
    """Cancel queued work; cancelling running work requires its current lease token."""
    current = _normalize_time(now)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise JobError("job does not exist")
        if row["state"] == "cancelled":
            return _job_from_row(row)
        if row["state"] in {"succeeded", "dead_letter"}:
            raise JobError(f"cannot cancel a {row['state']} job")
        if row["state"] == "running":
            if not lease_token:
                raise LeaseLostError("cancelling a running job requires its lease token")
            _current_lease(db, job_id, lease_token, current)
            db.execute(
                """UPDATE job_attempts SET status='cancelled',finished_at=?
                   WHERE job_id=? AND lease_token=? AND status='running'""",
                (current, job_id, lease_token),
            )
        db.execute(
            """UPDATE jobs SET state='cancelled',lease_owner=NULL,lease_token=NULL,
                      lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?""",
            (current, current, job_id),
        )
        return _job_from_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def upsert_interval_schedule(
    *,
    schedule_id: str,
    kind: str,
    next_due_at: datetime | str,
    interval_seconds: int,
    subject_id: str | None = None,
    payload: Mapping | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    enabled: bool = True,
) -> None:
    clean_id = _clean_required(schedule_id, "schedule_id")
    clean_kind = _clean_required(kind, "kind")
    if not 1 <= interval_seconds <= 31_536_000:
        raise ValueError("interval_seconds must be between 1 and 31536000")
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    payload_json = _canonical_json(payload)
    config_payload = json.dumps(
        {
            "kind": clean_kind,
            "subject_id": subject_id,
            "payload": json.loads(payload_json),
            "interval_seconds": interval_seconds,
            "priority": priority,
            "max_attempts": max_attempts,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    config_hash = hashlib.sha256(config_payload.encode("utf-8")).hexdigest()
    due = _normalize_time(next_due_at)
    current = utc_now()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO schedules(
                   id,kind,subject_id,payload_json,config_hash,interval_seconds,
                   priority,max_attempts,enabled,next_due_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   kind=excluded.kind,subject_id=excluded.subject_id,
                   payload_json=excluded.payload_json,config_hash=excluded.config_hash,
                   interval_seconds=excluded.interval_seconds,priority=excluded.priority,
                   max_attempts=excluded.max_attempts,enabled=excluded.enabled,
                   next_due_at=CASE
                       WHEN schedules.config_hash=excluded.config_hash
                       THEN schedules.next_due_at ELSE excluded.next_due_at END,
                   updated_at=excluded.updated_at""",
            (
                clean_id,
                clean_kind,
                subject_id,
                payload_json,
                config_hash,
                interval_seconds,
                priority,
                max_attempts,
                1 if enabled else 0,
                due,
                current,
                current,
            ),
        )


def enqueue_due_schedules(
    *, now: datetime | str | None = None, limit: int = 100
) -> tuple[JobRecord, ...]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    current = _normalize_time(now)
    current_dt = parse_utc(current)
    jobs = []
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT * FROM schedules WHERE enabled=1 AND next_due_at<=?
               ORDER BY next_due_at,id LIMIT ?""",
            (current, limit),
        ).fetchall()
        for row in rows:
            due = row["next_due_at"]
            payload = {
                "schedule_id": row["id"],
                "scheduled_for": due,
                "input": json.loads(row["payload_json"]),
            }
            job = _enqueue(
                db,
                kind=row["kind"],
                idempotency_key=f"schedule:{row['id']}:{due}",
                subject_id=row["subject_id"],
                input_version=row["config_hash"],
                payload=payload,
                priority=row["priority"],
                scheduled_for=due,
                max_attempts=row["max_attempts"],
                created_at=current,
            )
            due_dt = parse_utc(due)
            elapsed = max(0.0, (current_dt - due_dt).total_seconds())
            periods = int(elapsed // row["interval_seconds"]) + 1
            next_due = format_utc(
                due_dt + timedelta(seconds=periods * row["interval_seconds"])
            )
            db.execute(
                """UPDATE schedules SET next_due_at=?,last_enqueued_at=?,updated_at=?
                   WHERE id=? AND next_due_at=?""",
                (next_due, current, current, row["id"], due),
            )
            jobs.append(job)
    return tuple(jobs)


def job_counts() -> dict[str, int]:
    result = {
        state: 0
        for state in (
            "pending", "running", "succeeded", "retry_wait", "blocked", "dead_letter", "cancelled"
        )
    }
    with get_db() as db:
        for row in db.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state"):
            result[row["state"]] = row["count"]
    return result
