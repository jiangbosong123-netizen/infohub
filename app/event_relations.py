from __future__ import annotations

"""Append-only event relations and observable, cycle-safe event merges."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .database import get_db
from .publication import ChangeRequest, PublishedChange, PublicationResult, publish_job_result


RELATIONS = {"follows", "implements", "corrects", "denies", "related_to"}
RELATION_METHOD_VERSION = "event-relations-v1"


class EventRelationError(RuntimeError):
    """An event relation or merge would violate identity/history rules."""


@dataclass(frozen=True)
class EventResolution:
    requested_id: str
    canonical_id: str
    chain: tuple[str, ...]


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _stable_id(prefix: str, idempotency_key: str) -> str:
    clean = idempotency_key.strip()
    if not clean:
        raise ValueError("idempotency_key is required")
    return f"{prefix}_{hashlib.sha256(clean.encode('utf-8')).hexdigest()[:32]}"


def _clean_reason(reason: str) -> str:
    clean = reason.strip()
    if not clean:
        raise ValueError("reason is required")
    if len(clean) > 2_000:
        raise ValueError("reason exceeds 2000 characters")
    return clean


def _evidence(db: sqlite3.Connection, evidence_ids: Iterable[str]) -> tuple[str, ...]:
    values = tuple(sorted({value.strip() for value in evidence_ids if value.strip()}))
    if not values:
        raise EventRelationError("at least one raw evidence ID is required")
    placeholders = ",".join("?" for _ in values)
    found = {
        row[0] for row in db.execute(
            f"SELECT id FROM raw_records WHERE id IN ({placeholders})", values
        )
    }
    missing = sorted(set(values) - found)
    if missing:
        raise EventRelationError(f"raw event evidence is missing: {missing[:5]}")
    return values


def resolve_event(db: sqlite3.Connection, event_id: str) -> EventResolution:
    if not db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
        raise EventRelationError(f"event {event_id} does not exist")
    chain = [event_id]
    seen = {event_id}
    current = event_id
    while True:
        row = db.execute(
            "SELECT survivor_event_id FROM event_merges WHERE absorbed_event_id=?",
            (current,),
        ).fetchone()
        if not row:
            return EventResolution(event_id, current, tuple(chain))
        current = row["survivor_event_id"]
        if current in seen:
            raise EventRelationError("event merge graph contains a cycle")
        seen.add(current)
        chain.append(current)


def _event_version(
    db: sqlite3.Connection, event_id: str, expected_version_id: str
) -> sqlite3.Row:
    row = db.execute(
        "SELECT id,current_version_id,status FROM events WHERE id=?", (event_id,)
    ).fetchone()
    if not row:
        raise EventRelationError(f"event {event_id} does not exist")
    if row["current_version_id"] != expected_version_id:
        raise EventRelationError(f"event {event_id} changed before publication")
    return row


def _existing_change(db: sqlite3.Connection, version_id: str) -> ChangeRequest | None:
    row = db.execute(
        """SELECT idempotency_key,resource_type,resource_id,version_id,operation,payload_json
           FROM change_log WHERE version_id=?""",
        (version_id,),
    ).fetchone()
    if not row:
        return None
    return ChangeRequest(
        idempotency_key=row["idempotency_key"], resource_type=row["resource_type"],
        resource_id=row["resource_id"], version_id=row["version_id"],
        operation=row["operation"], payload=json.loads(row["payload_json"]),
    )


def _retry_persist_should_not_run(
    db: sqlite3.Connection, changes: tuple[PublishedChange, ...]
) -> None:
    raise EventRelationError("existing event publication belongs to another job")


def _prepare_merge(
    *,
    absorbed_event_id: str,
    requested_survivor_event_id: str,
    survivor_event_id: str,
    expected_absorbed_version_id: str,
    expected_survivor_version_id: str,
    evidence_ids: tuple[str, ...],
    reason: str,
    idempotency_key: str,
) -> tuple[ChangeRequest, str]:
    merge_id = _stable_id("event_merge", idempotency_key)
    payload = {
        "schema_version": "event-merge-v1",
        "absorbed_event_id": absorbed_event_id,
        "requested_survivor_event_id": requested_survivor_event_id,
        "survivor_event_id": survivor_event_id,
        "absorbed_event_version_id": expected_absorbed_version_id,
        "survivor_event_version_id": expected_survivor_version_id,
        "evidence_ids": list(evidence_ids),
        "reason": reason,
        "method_version": RELATION_METHOD_VERSION,
    }
    return ChangeRequest(
        idempotency_key=idempotency_key,
        resource_type="event",
        resource_id=absorbed_event_id,
        version_id=merge_id,
        operation="merge",
        payload=payload,
    ), merge_id


def publish_event_merge(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    absorbed_event_id: str,
    survivor_event_id: str,
    expected_absorbed_version_id: str,
    expected_survivor_version_id: str,
    evidence_ids: Iterable[str],
    reason: str,
    idempotency_key: str,
    now: datetime | str | None = None,
) -> PublicationResult:
    """Publish one merge with its change record and job success atomically."""
    clean_reason = _clean_reason(reason)
    requested_evidence = tuple(sorted({item.strip() for item in evidence_ids if item.strip()}))
    merge_id = _stable_id("event_merge", idempotency_key)
    with get_db() as db:
        existing = _existing_change(db, merge_id)
        if existing:
            expected_existing = {
                "absorbed_event_id": absorbed_event_id,
                "requested_survivor_event_id": survivor_event_id,
                "absorbed_event_version_id": expected_absorbed_version_id,
                "survivor_event_version_id": expected_survivor_version_id,
                "evidence_ids": list(requested_evidence),
                "reason": clean_reason,
            }
            if any(existing.payload.get(key) != value for key, value in expected_existing.items()):
                raise EventRelationError("merge retry inputs do not match the published merge")
            return publish_job_result(
                job_id=job_id, lease_token=lease_token,
                expected_input_version=expected_input_version,
                changes=(existing,), persist=_retry_persist_should_not_run, now=now,
            )
        target = resolve_event(db, survivor_event_id).canonical_id
        if target == absorbed_event_id:
            raise EventRelationError("event merge would create a cycle")
        target_row = _event_version(db, target, expected_survivor_version_id)
        absorbed_row = _event_version(
            db, absorbed_event_id, expected_absorbed_version_id
        )
        if absorbed_row["status"] in {"merged", "split", "retracted"}:
            raise EventRelationError("absorbed event is not mergeable")
        if target_row["status"] in {"merged", "split", "retracted"}:
            raise EventRelationError("survivor event is not mergeable")
        clean_evidence = _evidence(db, requested_evidence)

    change, prepared_merge_id = _prepare_merge(
        absorbed_event_id=absorbed_event_id,
        requested_survivor_event_id=survivor_event_id,
        survivor_event_id=target,
        expected_absorbed_version_id=expected_absorbed_version_id,
        expected_survivor_version_id=expected_survivor_version_id,
        evidence_ids=clean_evidence,
        reason=clean_reason,
        idempotency_key=idempotency_key,
    )
    if prepared_merge_id != merge_id:
        raise EventRelationError("merge identity preparation is inconsistent")

    def persist(db: sqlite3.Connection, changes: tuple[PublishedChange, ...]) -> None:
        if len(changes) != 1 or changes[0].version_id != merge_id:
            raise EventRelationError("merge publication does not match its change record")
        absorbed = _event_version(
            db, absorbed_event_id, expected_absorbed_version_id
        )
        survivor = resolve_event(db, target)
        if survivor.canonical_id != target:
            raise EventRelationError("survivor changed before publication")
        target_row = _event_version(db, target, expected_survivor_version_id)
        if absorbed["status"] in {"merged", "split", "retracted"}:
            raise EventRelationError("absorbed event is not mergeable")
        if target_row["status"] in {"merged", "split", "retracted"}:
            raise EventRelationError("survivor event is not mergeable")
        persisted_evidence = _evidence(db, clean_evidence)
        db.execute(
            """INSERT INTO event_merges(
                   id,absorbed_event_id,survivor_event_id,evidence_ids_json,
                   reason,available_at,publication_seq
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                merge_id, absorbed_event_id, target, _json(persisted_evidence),
                clean_reason, changes[0].available_at, changes[0].seq,
            ),
        )
        updated = db.execute(
            """UPDATE events SET status='merged'
               WHERE id=? AND current_version_id=? AND status NOT IN ('merged','split','retracted')""",
            (absorbed_event_id, expected_absorbed_version_id),
        )
        if updated.rowcount != 1:
            raise EventRelationError("absorbed event changed before publication")

    return publish_job_result(
        job_id=job_id,
        lease_token=lease_token,
        expected_input_version=expected_input_version,
        changes=(change,),
        persist=persist,
        now=now,
    )


def publish_event_relation(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    from_event_id: str,
    to_event_id: str,
    expected_from_version_id: str,
    expected_to_version_id: str,
    relation: str,
    evidence_ids: Iterable[str],
    reason: str,
    idempotency_key: str,
    supersedes_relation_id: str | None = None,
    now: datetime | str | None = None,
) -> PublicationResult:
    """Publish an immutable semantic relation between canonical event IDs."""
    if relation not in RELATIONS:
        raise ValueError(f"unsupported event relation {relation!r}")
    clean_reason = _clean_reason(reason)
    requested_evidence = tuple(sorted({item.strip() for item in evidence_ids if item.strip()}))
    relation_id = _stable_id("event_relation", idempotency_key)
    with get_db() as db:
        existing = _existing_change(db, relation_id)
        if existing:
            expected_existing = {
                "requested_from_event_id": from_event_id,
                "requested_to_event_id": to_event_id,
                "from_event_version_id": expected_from_version_id,
                "to_event_version_id": expected_to_version_id,
                "relation": relation,
                "evidence_ids": list(requested_evidence),
                "reason": clean_reason,
                "supersedes_relation_id": supersedes_relation_id,
            }
            if any(existing.payload.get(key) != value for key, value in expected_existing.items()):
                raise EventRelationError(
                    "relation retry inputs do not match the published relation"
                )
            return publish_job_result(
                job_id=job_id, lease_token=lease_token,
                expected_input_version=expected_input_version,
                changes=(existing,), persist=_retry_persist_should_not_run, now=now,
            )
        source = resolve_event(db, from_event_id).canonical_id
        target = resolve_event(db, to_event_id).canonical_id
        if source == target:
            raise EventRelationError("an event cannot relate to itself or its merge alias")
        source_row = _event_version(db, source, expected_from_version_id)
        target_row = _event_version(db, target, expected_to_version_id)
        if source_row["status"] in {"merged", "split", "retracted"} or target_row[
            "status"
        ] in {"merged", "split", "retracted"}:
            raise EventRelationError("relation endpoints must be current canonical events")
        clean_evidence = _evidence(db, requested_evidence)
    payload = {
        "schema_version": "event-relation-v1",
        "requested_from_event_id": from_event_id,
        "requested_to_event_id": to_event_id,
        "from_event_id": source,
        "to_event_id": target,
        "from_event_version_id": expected_from_version_id,
        "to_event_version_id": expected_to_version_id,
        "relation": relation,
        "evidence_ids": list(clean_evidence),
        "reason": clean_reason,
        "supersedes_relation_id": supersedes_relation_id,
        "method_version": RELATION_METHOD_VERSION,
    }
    change = ChangeRequest(
        idempotency_key=idempotency_key,
        resource_type="event",
        resource_id=source,
        version_id=relation_id,
        operation="update",
        payload=payload,
    )

    def persist(db: sqlite3.Connection, changes: tuple[PublishedChange, ...]) -> None:
        if len(changes) != 1 or changes[0].version_id != relation_id:
            raise EventRelationError("relation publication does not match its change record")
        source_resolution = resolve_event(db, source)
        target_resolution = resolve_event(db, target)
        if source_resolution.canonical_id != source or target_resolution.canonical_id != target:
            raise EventRelationError("relation endpoint was merged before publication")
        source_row = _event_version(db, source, expected_from_version_id)
        target_row = _event_version(db, target, expected_to_version_id)
        if source_row["status"] in {"merged", "split", "retracted"} or target_row[
            "status"
        ] in {"merged", "split", "retracted"}:
            raise EventRelationError("relation endpoints changed before publication")
        persisted_evidence = _evidence(db, clean_evidence)
        if supersedes_relation_id:
            previous = db.execute(
                """SELECT from_event_id,to_event_id FROM event_relations
                   WHERE id=?""",
                (supersedes_relation_id,),
            ).fetchone()
            if not previous or tuple(previous) != (source, target):
                raise EventRelationError(
                    "superseded relation is missing or has different endpoints"
                )
            if db.execute(
                """SELECT 1 FROM event_relations WHERE supersedes_relation_id=?""",
                (supersedes_relation_id,),
            ).fetchone():
                raise EventRelationError("event relation was already superseded")
        db.execute(
            """INSERT INTO event_relations(
                   id,from_event_id,to_event_id,relation,evidence_ids_json,reason,
                   available_at,supersedes_relation_id,publication_seq
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                relation_id, source, target, relation, _json(persisted_evidence),
                clean_reason, changes[0].available_at, supersedes_relation_id,
                changes[0].seq,
            ),
        )

    return publish_job_result(
        job_id=job_id,
        lease_token=lease_token,
        expected_input_version=expected_input_version,
        changes=(change,),
        persist=persist,
        now=now,
    )
