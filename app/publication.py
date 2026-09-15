from __future__ import annotations

"""Dataset identity, monotonic changes and atomic durable-job publication."""

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping
from uuid import uuid4

import rfc8785

from . import config
from .database import get_db
from .jobs import (
    JobRecord,
    _complete_job_in_transaction,
    _current_lease,
    _expire_leases,
    _validate_input_version,
)
from .timeutil import format_utc, parse_utc, utc_now


RESOURCE_TYPES = {
    "item", "event", "entity", "topic", "source", "analysis", "signal", "report",
    "evidence",
}
OPERATIONS = {"create", "update", "withdraw", "merge", "split", "delete"}
HASH_ALGORITHM = "jcs-sha256-v1"
MAX_PAYLOAD_BYTES = 2_000_000
MAX_CLOCK_EVIDENCE_AGE_SECONDS = 300
MAX_VERIFIED_CLOCK_OFFSET_MS = 1_000


class PublicationError(RuntimeError):
    """Base class for publication-ledger safety failures."""


class DatasetEnvironmentError(PublicationError):
    """The database belongs to a different configured runtime environment."""


class EpochConflictError(PublicationError):
    """The caller or job refers to a dataset epoch that is no longer current."""


class EpochRotationBlockedError(PublicationError):
    """A new epoch cannot start while a live old-epoch worker holds a lease."""


class PublicationConflictError(PublicationError):
    """A publication idempotency key was reused for different content."""


@dataclass(frozen=True)
class DatasetIdentity:
    dataset_id: str
    epoch: str
    owner_environment_id: str
    high_water: int
    epoch_started_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChangeRequest:
    idempotency_key: str
    resource_type: str
    resource_id: str
    version_id: str
    operation: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class PublishedChange:
    seq: int
    dataset_id: str
    epoch: str
    idempotency_key: str
    resource_type: str
    resource_id: str
    version_id: str
    operation: str
    available_at: str
    payload: dict
    payload_sha256: str
    hash_algorithm: str = HASH_ALGORITHM

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PublicationResult:
    dataset: DatasetIdentity
    job: JobRecord
    changes: tuple[PublishedChange, ...]

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset.to_dict(),
            "job": self.job.to_dict(),
            "changes": [change.to_dict() for change in self.changes],
        }


@dataclass(frozen=True)
class ClockCheck:
    id: str
    environment_id: str
    measured_at: str
    recorded_at: str
    source: str | None
    offset_ms: float | None
    status: str
    detail: dict


@dataclass(frozen=True)
class KnowledgeCheckpoint:
    id: str
    dataset_id: str
    epoch: str
    high_water: int
    observed_at: str
    clock_status: str
    clock_check_id: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _PreparedChange:
    request: ChangeRequest
    payload_json: str
    payload_sha256: str


PersistChanges = Callable[[sqlite3.Connection, tuple[PublishedChange, ...]], None]


def _clean(value: str, field: str, maximum: int = 200) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return cleaned


def _normalize_time(value: datetime | str | None) -> str:
    if value is None:
        return utc_now()
    if isinstance(value, str):
        return format_utc(parse_utc(value))
    return format_utc(value)


def _identity_for_epoch(
    db: sqlite3.Connection, dataset_id: str, epoch: str
) -> DatasetIdentity:
    row = db.execute(
        """SELECT owner_environment_id,started_at FROM dataset_epochs
           WHERE dataset_id=? AND epoch=?""",
        (dataset_id, epoch),
    ).fetchone()
    if not row:
        raise EpochConflictError("dataset epoch is not present in this database")
    high_water = db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM change_log WHERE dataset_id=? AND epoch=?",
        (dataset_id, epoch),
    ).fetchone()[0]
    return DatasetIdentity(
        dataset_id=dataset_id,
        epoch=epoch,
        owner_environment_id=row["owner_environment_id"],
        high_water=high_water,
        epoch_started_at=row["started_at"],
    )


def _current_identity(
    db: sqlite3.Connection, *, require_environment: bool = True
) -> DatasetIdentity:
    row = db.execute(
        """SELECT dataset_id,current_epoch,owner_environment_id
           FROM dataset_state WHERE singleton=1"""
    ).fetchone()
    if not row:
        raise PublicationError("dataset identity has not been initialized")
    if require_environment and row["owner_environment_id"] != config.ENVIRONMENT_ID:
        raise DatasetEnvironmentError(
            f"database belongs to environment {row['owner_environment_id']!r}, "
            f"not {config.ENVIRONMENT_ID!r}"
        )
    return _identity_for_epoch(db, row["dataset_id"], row["current_epoch"])


def get_dataset_identity(*, require_environment: bool = True) -> DatasetIdentity:
    with get_db() as db:
        return _current_identity(db, require_environment=require_environment)


def rotate_dataset_epoch(
    *,
    expected_epoch: str,
    reason: str,
    now: datetime | str | None = None,
) -> DatasetIdentity:
    """Start a new cursor namespace after a discontinuous restore or reset."""
    current = _normalize_time(now)
    clean_reason = _clean(reason, "reason", 1_000)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        identity = _current_identity(db)
        if identity.epoch != expected_epoch:
            raise EpochConflictError("dataset epoch changed before rotation")

        _expire_leases(db, current)
        live = db.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE state='running' AND (dataset_epoch=? OR dataset_epoch IS NULL)""",
            (identity.epoch,),
        ).fetchone()[0]
        if live:
            raise EpochRotationBlockedError(
                "stop workers or wait for current leases to expire before rotating the epoch"
            )

        db.execute(
            """UPDATE jobs SET state='cancelled',lease_owner=NULL,lease_token=NULL,
                      lease_expires_at=NULL,updated_at=?,finished_at=?
               WHERE state IN ('pending','retry_wait','blocked')
                 AND (dataset_epoch=? OR dataset_epoch IS NULL)""",
            (current, current, identity.epoch),
        )
        new_epoch = f"epoch_{uuid4().hex}"
        db.execute(
            """INSERT INTO dataset_epochs(
                   dataset_id,epoch,previous_epoch,reason,owner_environment_id,
                   started_at,release_id
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                identity.dataset_id,
                new_epoch,
                identity.epoch,
                clean_reason,
                identity.owner_environment_id,
                current,
                config.APP_VERSION.strip() or "unknown",
            ),
        )
        db.execute(
            """UPDATE dataset_state SET current_epoch=?,updated_at=? WHERE singleton=1
               AND current_epoch=?""",
            (new_epoch, current, identity.epoch),
        )
        return _identity_for_epoch(db, identity.dataset_id, new_epoch)


def _prepare_change(change: ChangeRequest) -> _PreparedChange:
    key = _clean(change.idempotency_key, "idempotency_key", 500)
    resource_type = _clean(change.resource_type, "resource_type", 30)
    if resource_type not in RESOURCE_TYPES:
        raise ValueError(f"unsupported resource_type {resource_type!r}")
    resource_id = _clean(change.resource_id, "resource_id", 128)
    version_id = _clean(change.version_id, "version_id", 128)
    operation = _clean(change.operation, "operation", 20)
    if operation not in OPERATIONS:
        raise ValueError(f"unsupported operation {operation!r}")
    if not isinstance(change.payload, Mapping):
        raise ValueError("payload must be a JSON object")
    try:
        payload_bytes = rfc8785.dumps(dict(change.payload))
    except ValueError as exc:
        raise ValueError(f"payload is not valid I-JSON: {exc}") from exc
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} canonical bytes")
    request = ChangeRequest(
        idempotency_key=key,
        resource_type=resource_type,
        resource_id=resource_id,
        version_id=version_id,
        operation=operation,
        payload=dict(change.payload),
    )
    return _PreparedChange(
        request=request,
        payload_json=payload_bytes.decode("utf-8"),
        payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
    )


def _change_from_row(row) -> PublishedChange:
    return PublishedChange(
        seq=row["seq"],
        dataset_id=row["dataset_id"],
        epoch=row["epoch"],
        idempotency_key=row["idempotency_key"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        version_id=row["version_id"],
        operation=row["operation"],
        available_at=row["available_at"],
        payload=json.loads(row["payload_json"]),
        payload_sha256=row["payload_sha256"],
        hash_algorithm=row["hash_algorithm"],
    )


def _same_change(prepared: _PreparedChange, published: PublishedChange) -> bool:
    request = prepared.request
    return (
        request.idempotency_key == published.idempotency_key
        and request.resource_type == published.resource_type
        and request.resource_id == published.resource_id
        and request.version_id == published.version_id
        and request.operation == published.operation
        and prepared.payload_json
        == rfc8785.dumps(published.payload).decode("utf-8")
        and prepared.payload_sha256 == published.payload_sha256
    )


def _result_ref(job_id: str, changes: tuple[PublishedChange, ...]) -> str:
    if not changes:
        return f"publication:none:{job_id}"
    return (
        f"publication:{changes[0].dataset_id}:{changes[0].epoch}:"
        f"{changes[0].seq}-{changes[-1].seq}"
    )


def _successful_retry(
    db: sqlite3.Connection,
    job_row,
    lease_token: str,
    expected_input_version: str | None,
    prepared: tuple[_PreparedChange, ...],
    current: str,
) -> PublicationResult:
    _current_identity(db)
    rows = db.execute(
        """SELECT * FROM change_log WHERE job_id=? AND lease_token=? ORDER BY seq""",
        (job_row["id"], lease_token),
    ).fetchall()
    changes = tuple(_change_from_row(row) for row in rows)
    if len(changes) != len(prepared) or any(
        not _same_change(request, change)
        for request, change in zip(prepared, changes)
    ):
        raise PublicationConflictError(
            "publication retry does not match the changes already committed"
        )
    job = _complete_job_in_transaction(
        db,
        job_row["id"],
        lease_token,
        expected_input_version=expected_input_version,
        result_ref=_result_ref(job_row["id"], changes),
        current=current,
    )
    epoch = job_row["dataset_epoch"]
    if not epoch:
        raise EpochConflictError("completed job has no dataset epoch")
    dataset_row = db.execute(
        "SELECT dataset_id FROM dataset_epochs WHERE epoch=?", (epoch,)
    ).fetchone()
    if not dataset_row:
        raise EpochConflictError("completed job epoch is unavailable")
    dataset = _identity_for_epoch(db, dataset_row["dataset_id"], epoch)
    return PublicationResult(dataset=dataset, job=job, changes=changes)


def publish_job_result(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    changes: Iterable[ChangeRequest] = (),
    persist: PersistChanges | None = None,
    now: datetime | str | None = None,
) -> PublicationResult:
    """Atomically persist bounded DB writes, changes, and durable-job success.

    ``persist`` must perform database-only, bounded writes using the supplied
    connection. Network calls and long computation belong outside this transaction.
    """
    current = _normalize_time(now)
    prepared = tuple(_prepare_change(change) for change in changes)
    keys = [change.request.idempotency_key for change in prepared]
    if len(keys) != len(set(keys)):
        raise ValueError("publication idempotency keys must be unique within a batch")
    if prepared and persist is None:
        raise ValueError("each visible change requires a bounded content persistence callback")
    if persist is not None and not prepared:
        raise ValueError("visible content writes require at least one change record")

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (
            job_row
            and job_row["state"] == "succeeded"
            and job_row["completed_lease_token"] == lease_token
        ):
            return _successful_retry(
                db, job_row, lease_token, expected_input_version, prepared, current
            )

        identity = _current_identity(db)
        job_row = _current_lease(db, job_id, lease_token, current)
        _validate_input_version(db, job_row, expected_input_version)
        if job_row["dataset_epoch"] != identity.epoch:
            raise EpochConflictError("job belongs to a non-current dataset epoch")

        published: list[PublishedChange] = []
        for change in prepared:
            request = change.request
            try:
                cursor = db.execute(
                    """INSERT INTO change_log(
                           dataset_id,epoch,idempotency_key,resource_type,resource_id,
                           version_id,operation,available_at,payload_json,payload_sha256,
                           hash_algorithm,job_id,lease_token
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identity.dataset_id,
                        identity.epoch,
                        request.idempotency_key,
                        request.resource_type,
                        request.resource_id,
                        request.version_id,
                        request.operation,
                        current,
                        change.payload_json,
                        change.payload_sha256,
                        HASH_ALGORITHM,
                        job_id,
                        lease_token,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = db.execute(
                    """SELECT * FROM change_log
                       WHERE dataset_id=? AND epoch=? AND idempotency_key=?""",
                    (identity.dataset_id, identity.epoch, request.idempotency_key),
                ).fetchone()
                if not existing:
                    raise
                if not _same_change(change, _change_from_row(existing)):
                    raise PublicationConflictError(
                        f"publication key {request.idempotency_key!r} is already used"
                    ) from exc
                raise PublicationConflictError(
                    f"publication key {request.idempotency_key!r} belongs to another job"
                ) from exc
            row = db.execute(
                "SELECT * FROM change_log WHERE seq=?", (cursor.lastrowid,)
            ).fetchone()
            published.append(_change_from_row(row))

        published_tuple = tuple(published)
        if persist is not None:
            persist(db, published_tuple)
        job = _complete_job_in_transaction(
            db,
            job_id,
            lease_token,
            expected_input_version=expected_input_version,
            result_ref=_result_ref(job_id, published_tuple),
            current=current,
        )
        dataset = DatasetIdentity(
            dataset_id=identity.dataset_id,
            epoch=identity.epoch,
            owner_environment_id=identity.owner_environment_id,
            high_water=published_tuple[-1].seq if published_tuple else identity.high_water,
            epoch_started_at=identity.epoch_started_at,
        )
        return PublicationResult(dataset=dataset, job=job, changes=published_tuple)


def record_clock_check(
    *,
    measured_at: datetime | str,
    source: str | None,
    offset_ms: float | None,
    detail: Mapping[str, object] | None = None,
    recorded_at: datetime | str | None = None,
) -> ClockCheck:
    measured = _normalize_time(measured_at)
    recorded = _normalize_time(recorded_at)
    clean_source = source.strip()[:200] if source and source.strip() else None
    if offset_ms is not None and not math.isfinite(offset_ms):
        raise ValueError("offset_ms must be finite")
    age = (parse_utc(recorded) - parse_utc(measured)).total_seconds()
    if clean_source is None or offset_ms is None:
        status = "unknown"
    elif age < 0 or age > MAX_CLOCK_EVIDENCE_AGE_SECONDS:
        status = "suspect"
    elif abs(offset_ms) > MAX_VERIFIED_CLOCK_OFFSET_MS:
        status = "suspect"
    else:
        status = "verified"
    try:
        detail_bytes = rfc8785.dumps(dict(detail or {}))
    except ValueError as exc:
        raise ValueError(f"clock detail is not valid I-JSON: {exc}") from exc
    if len(detail_bytes) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"clock detail exceeds {MAX_PAYLOAD_BYTES} canonical bytes")
    check = ClockCheck(
        id=f"clock_{uuid4().hex}",
        environment_id=config.ENVIRONMENT_ID,
        measured_at=measured,
        recorded_at=recorded,
        source=clean_source,
        offset_ms=offset_ms,
        status=status,
        detail=json.loads(detail_bytes),
    )
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _current_identity(db)
        db.execute(
            """INSERT INTO clock_checks(
                   id,environment_id,measured_at,recorded_at,source,offset_ms,status,detail_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                check.id,
                check.environment_id,
                check.measured_at,
                check.recorded_at,
                check.source,
                check.offset_ms,
                check.status,
                detail_bytes.decode("utf-8"),
            ),
        )
    return check


def create_knowledge_checkpoint(
    *,
    clock_check_id: str | None = None,
    observed_at: datetime | str | None = None,
) -> KnowledgeCheckpoint:
    supplied_observed = _normalize_time(observed_at) if observed_at is not None else None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        identity = _current_identity(db)
        high_water = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM change_log WHERE dataset_id=? AND epoch=?",
            (identity.dataset_id, identity.epoch),
        ).fetchone()[0]
        observed = supplied_observed or utc_now()

        clock_status = "unknown"
        if clock_check_id:
            check = db.execute(
                "SELECT * FROM clock_checks WHERE id=?", (clock_check_id,)
            ).fetchone()
            if not check:
                raise PublicationError("clock check does not exist")
            if check["environment_id"] != identity.owner_environment_id:
                raise DatasetEnvironmentError(
                    "clock evidence belongs to a different runtime environment"
                )
            age = (parse_utc(observed) - parse_utc(check["measured_at"])).total_seconds()
            if check["status"] == "unknown":
                clock_status = "unknown"
            elif (
                check["status"] == "verified"
                and 0 <= age <= MAX_CLOCK_EVIDENCE_AGE_SECONDS
                and check["offset_ms"] is not None
                and abs(check["offset_ms"]) <= MAX_VERIFIED_CLOCK_OFFSET_MS
            ):
                clock_status = "verified"
            else:
                clock_status = "suspect"

        checkpoint = KnowledgeCheckpoint(
            id=f"checkpoint_{uuid4().hex}",
            dataset_id=identity.dataset_id,
            epoch=identity.epoch,
            high_water=high_water,
            observed_at=observed,
            clock_status=clock_status,
            clock_check_id=clock_check_id,
        )
        db.execute(
            """INSERT INTO knowledge_checkpoints(
                   id,dataset_id,epoch,high_water,observed_at,clock_status,clock_check_id
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                checkpoint.id,
                checkpoint.dataset_id,
                checkpoint.epoch,
                checkpoint.high_water,
                checkpoint.observed_at,
                checkpoint.clock_status,
                checkpoint.clock_check_id,
            ),
        )
        return checkpoint
