from __future__ import annotations

"""Atomic split and retraction transitions that preserve prior event history."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable

from .database import get_db
from .event_relations import (
    EventRelationError,
    _clean_reason,
    _event_version,
    _existing_change,
    _retry_persist_should_not_run,
    _stable_id,
    resolve_event,
)
from .publication import ChangeRequest, PublishedChange, PublicationResult, publish_job_result


TERMINAL_STATUSES = {"merged", "split", "retracted"}
TRANSITION_VERSION = "event-terminal-transitions-v1"


@dataclass(frozen=True, order=True)
class SplitAssignment:
    document_version_id: str
    evidence_id: str
    replacement_event_id: str
    replacement_event_version_id: str


@dataclass(frozen=True, order=True)
class RetractionEvidence:
    document_version_id: str
    evidence_id: str
    role: str = "contradicts"


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _request_sha(value: object) -> str:
    return _sha({"method_version": TRANSITION_VERSION, "request": value})


def _validate_input_evidence(
    db: sqlite3.Connection, evidence: Iterable[RetractionEvidence]
) -> tuple[RetractionEvidence, ...]:
    rows = tuple(sorted(set(evidence)))
    if not rows:
        raise EventRelationError("at least one version-pinned evidence row is required")
    for item in rows:
        if item.role not in {"contradicts", "context"}:
            raise EventRelationError("retraction evidence must contradict or provide context")
        if not db.execute(
            """SELECT 1 FROM document_version_inputs
               WHERE version_id=? AND raw_record_id=?""",
            (item.document_version_id, item.evidence_id),
        ).fetchone():
            raise EventRelationError("retraction evidence does not belong to its document version")
    return rows


def publish_event_split(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    original_event_id: str,
    expected_original_version_id: str,
    replacements: Iterable[tuple[str, str]],
    assignments: Iterable[SplitAssignment],
    reason: str,
    idempotency_key: str,
    now: datetime | str | None = None,
) -> PublicationResult:
    """Split one event into at least two existing stable events."""
    clean_reason = _clean_reason(reason)
    requested_replacements = tuple(sorted(set(replacements)))
    requested_assignments = tuple(sorted(set(assignments)))
    request = {
        "original_event_id": original_event_id,
        "original_event_version_id": expected_original_version_id,
        "replacements": [list(item) for item in requested_replacements],
        "assignments": [asdict(item) for item in requested_assignments],
        "reason": clean_reason,
    }
    request_sha = _request_sha(request)
    split_id = _stable_id("event_split", idempotency_key)
    with get_db() as db:
        existing = _existing_change(db, split_id)
        if existing:
            if existing.payload.get("request_sha256") != request_sha:
                raise EventRelationError("split retry inputs do not match the published split")
            return publish_job_result(
                job_id=job_id, lease_token=lease_token,
                expected_input_version=expected_input_version, changes=(existing,),
                persist=_retry_persist_should_not_run, now=now,
            )
        canonical = resolve_event(db, original_event_id).canonical_id
        if canonical != original_event_id:
            raise EventRelationError("only a canonical event can be split")
        original = _event_version(db, original_event_id, expected_original_version_id)
        if original["status"] in TERMINAL_STATUSES:
            raise EventRelationError("original event is already terminal")
        if len(requested_replacements) < 2:
            raise EventRelationError("a split requires at least two replacement events")
        canonical_replacements: list[tuple[str, str]] = []
        for event_id, version_id in requested_replacements:
            target = resolve_event(db, event_id).canonical_id
            if target != event_id or target == original_event_id:
                raise EventRelationError("split replacements must be distinct canonical events")
            row = _event_version(db, target, version_id)
            if row["status"] in TERMINAL_STATUSES:
                raise EventRelationError("split replacement is terminal")
            canonical_replacements.append((target, version_id))
        if len({item[0] for item in canonical_replacements}) != len(canonical_replacements):
            raise EventRelationError("split replacement events must be unique")
        _validate_split_assignments(
            db, original_event_id, expected_original_version_id,
            tuple(canonical_replacements), requested_assignments,
        )

    payload = {
        "schema_version": "event-split-v1", "request_sha256": request_sha,
        **request, "method_version": TRANSITION_VERSION,
    }
    change = ChangeRequest(
        idempotency_key=idempotency_key, resource_type="event",
        resource_id=original_event_id, version_id=split_id,
        operation="split", payload=payload,
    )

    def persist(db: sqlite3.Connection, changes: tuple[PublishedChange, ...]) -> None:
        if len(changes) != 1 or changes[0].version_id != split_id:
            raise EventRelationError("split publication does not match its change record")
        if resolve_event(db, original_event_id).canonical_id != original_event_id:
            raise EventRelationError("original event was merged before split publication")
        original = _event_version(db, original_event_id, expected_original_version_id)
        if original["status"] in TERMINAL_STATUSES:
            raise EventRelationError("original event became terminal before split publication")
        for event_id, version_id in requested_replacements:
            if resolve_event(db, event_id).canonical_id != event_id:
                raise EventRelationError("replacement event was merged before split publication")
            replacement = _event_version(db, event_id, version_id)
            if replacement["status"] in TERMINAL_STATUSES:
                raise EventRelationError("replacement event became terminal before split publication")
        _validate_split_assignments(
            db, original_event_id, expected_original_version_id,
            requested_replacements, requested_assignments,
        )
        evidence_ids = sorted({item.evidence_id for item in requested_assignments})
        db.execute(
            """INSERT INTO event_splits(
                   id,original_event_id,previous_status,evidence_ids_json,reason,
                   available_at,publication_seq
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                split_id, original_event_id, original["status"], _json(evidence_ids),
                clean_reason, changes[0].available_at, changes[0].seq,
            ),
        )
        for ordinal, (event_id, version_id) in enumerate(requested_replacements):
            db.execute(
                """INSERT INTO event_split_replacements(
                       split_id,replacement_event_id,replacement_event_version_id,ordinal
                   ) VALUES(?,?,?,?)""",
                (split_id, event_id, version_id, ordinal),
            )
        for item in requested_assignments:
            db.execute(
                """INSERT INTO event_split_assignments(
                       split_id,document_version_id,evidence_id,replacement_event_id,
                       replacement_event_version_id,available_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    split_id, item.document_version_id, item.evidence_id,
                    item.replacement_event_id, item.replacement_event_version_id,
                    changes[0].available_at,
                ),
            )
        updated = db.execute(
            """UPDATE events SET status='split'
               WHERE id=? AND current_version_id=? AND status=?""",
            (original_event_id, expected_original_version_id, original["status"]),
        )
        if updated.rowcount != 1:
            raise EventRelationError("original event changed before split publication")

    return publish_job_result(
        job_id=job_id, lease_token=lease_token,
        expected_input_version=expected_input_version, changes=(change,),
        persist=persist, now=now,
    )


def _validate_split_assignments(
    db: sqlite3.Connection,
    original_event_id: str,
    original_version_id: str,
    replacements: tuple[tuple[str, str], ...],
    assignments: tuple[SplitAssignment, ...],
) -> None:
    replacement_map = dict(replacements)
    if not assignments:
        raise EventRelationError("a split requires evidence assignments")
    source_evidence = {
        (row["document_version_id"], row["evidence_id"])
        for row in db.execute(
            """SELECT document_version_id,evidence_id FROM event_evidence
               WHERE event_version_id=?""",
            (original_version_id,),
        )
    }
    if not source_evidence:
        raise EventRelationError("the original event version has no evidence to split")
    assigned_source: set[tuple[str, str]] = set()
    used_replacements: set[str] = set()
    for item in assignments:
        expected_version = replacement_map.get(item.replacement_event_id)
        if expected_version != item.replacement_event_version_id:
            raise EventRelationError("split assignment targets an undeclared replacement version")
        pair = (item.document_version_id, item.evidence_id)
        if pair not in source_evidence:
            raise EventRelationError("split assignment is not evidence of the original event version")
        assigned_source.add(pair)
        used_replacements.add(item.replacement_event_id)
    if assigned_source != source_evidence:
        raise EventRelationError("every original evidence row must be assigned")
    if used_replacements != set(replacement_map):
        raise EventRelationError("every replacement event must receive evidence")


def publish_event_retraction(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    event_id: str,
    expected_event_version_id: str,
    evidence: Iterable[RetractionEvidence],
    reason: str,
    idempotency_key: str,
    now: datetime | str | None = None,
) -> PublicationResult:
    """Append a retracted event version and publish its terminal transition."""
    clean_reason = _clean_reason(reason)
    requested_evidence = tuple(sorted(set(evidence)))
    request = {
        "event_id": event_id, "event_version_id": expected_event_version_id,
        "evidence": [asdict(item) for item in requested_evidence], "reason": clean_reason,
    }
    request_sha = _request_sha(request)
    new_version_id = _stable_id("event_version", idempotency_key)
    retraction_id = _stable_id("event_retraction", idempotency_key)
    with get_db() as db:
        existing = _existing_change(db, new_version_id)
        if existing:
            if existing.payload.get("request_sha256") != request_sha:
                raise EventRelationError(
                    "retraction retry inputs do not match the published retraction"
                )
            return publish_job_result(
                job_id=job_id, lease_token=lease_token,
                expected_input_version=expected_input_version, changes=(existing,),
                persist=_retry_persist_should_not_run, now=now,
            )
        if resolve_event(db, event_id).canonical_id != event_id:
            raise EventRelationError("only a canonical event can be retracted")
        event = _event_version(db, event_id, expected_event_version_id)
        if event["status"] in TERMINAL_STATUSES:
            raise EventRelationError("event is already terminal")
        clean_evidence = _validate_input_evidence(db, requested_evidence)
        current = db.execute(
            "SELECT * FROM event_versions WHERE id=?", (expected_event_version_id,)
        ).fetchone()
        if not current:
            raise EventRelationError("current event version is missing")

    payload = {
        "schema_version": "event-retraction-v1", "request_sha256": request_sha,
        **request, "retracted_event_version_id": new_version_id,
        "method_version": TRANSITION_VERSION,
    }
    change = ChangeRequest(
        idempotency_key=idempotency_key, resource_type="event", resource_id=event_id,
        version_id=new_version_id, operation="withdraw", payload=payload,
    )

    def persist(db: sqlite3.Connection, changes: tuple[PublishedChange, ...]) -> None:
        if len(changes) != 1 or changes[0].version_id != new_version_id:
            raise EventRelationError("retraction publication does not match its change record")
        if resolve_event(db, event_id).canonical_id != event_id:
            raise EventRelationError("event was merged before retraction publication")
        event = _event_version(db, event_id, expected_event_version_id)
        if event["status"] in TERMINAL_STATUSES:
            raise EventRelationError("event became terminal before retraction publication")
        evidence_rows = _validate_input_evidence(db, clean_evidence)
        current = db.execute(
            "SELECT * FROM event_versions WHERE id=?", (expected_event_version_id,)
        ).fetchone()
        semantic = {
            "schema_version": current["schema_version"], "title": current["title"],
            "event_type": current["event_type"],
            "event_time_start": current["event_time_start"],
            "event_time_end": current["event_time_end"],
            "time_precision": current["time_precision"],
            "primary_entities": json.loads(current["primary_entities_json"]),
            "object_entities": json.loads(current["object_entities_json"]),
            "facts": json.loads(current["facts_json"]),
            "topics": json.loads(current["topics_json"]),
            "knowledge_status": "retracted",
            "created_by": "event_retraction",
            "method_version": TRANSITION_VERSION,
        }
        db.execute(
            """INSERT INTO event_versions(
                   id,event_id,version,previous_version_id,schema_version,title,event_type,
                   event_time_start,event_time_end,time_precision,primary_entities_json,
                   object_entities_json,facts_json,topics_json,knowledge_status,
                   version_sha256,available_at,created_by,method_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_version_id, event_id, current["version"] + 1, current["id"],
                semantic["schema_version"], semantic["title"], semantic["event_type"],
                semantic["event_time_start"], semantic["event_time_end"],
                semantic["time_precision"], _json(semantic["primary_entities"]),
                _json(semantic["object_entities"]), _json(semantic["facts"]),
                _json(semantic["topics"]), "retracted", _sha(semantic),
                changes[0].available_at, "event_retraction", TRANSITION_VERSION,
            ),
        )
        for item in evidence_rows:
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,fact_id,role,
                       available_at
                   ) VALUES(?,?,?,?,NULL,?,?)""",
                (
                    f"event_evidence_{hashlib.sha256((new_version_id + ':' + item.document_version_id + ':' + item.evidence_id + ':' + item.role).encode()).hexdigest()[:32]}",
                    new_version_id, item.document_version_id, item.evidence_id, item.role,
                    changes[0].available_at,
                ),
            )
        db.execute(
            """INSERT INTO event_retractions(
                   id,event_id,previous_status,retracted_event_version_id,
                   evidence_ids_json,reason,available_at,publication_seq
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                retraction_id, event_id, event["status"], new_version_id,
                _json(sorted({item.evidence_id for item in evidence_rows})), clean_reason,
                changes[0].available_at, changes[0].seq,
            ),
        )
        updated = db.execute(
            """UPDATE events SET status='retracted',current_version_id=?,last_fact_change_at=?
               WHERE id=? AND current_version_id=? AND status=?""",
            (
                new_version_id, changes[0].available_at, event_id,
                expected_event_version_id, event["status"],
            ),
        )
        if updated.rowcount != 1:
            raise EventRelationError("event changed before retraction publication")

    return publish_job_result(
        job_id=job_id, lease_token=lease_token,
        expected_input_version=expected_input_version, changes=(change,),
        persist=persist, now=now,
    )


def event_status_at_sequence(
    db: sqlite3.Connection, event_id: str, sequence: int
) -> str:
    """Return the event's terminal-aware status at a committed high-water mark."""
    event = db.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise EventRelationError(f"event {event_id} does not exist")
    transitions = []
    for table, key, status in (
        ("event_merges", "absorbed_event_id", "merged"),
        ("event_splits", "original_event_id", "split"),
        ("event_retractions", "event_id", "retracted"),
    ):
        row = db.execute(
            f"SELECT previous_status,publication_seq FROM {table} WHERE {key}=?",
            (event_id,),
        ).fetchone()
        if row:
            transitions.append((row["publication_seq"], row["previous_status"], status))
    if not transitions:
        return event["status"]
    transition = min(transitions)
    if sequence < transition[0]:
        if transition[1] is None:
            raise EventRelationError("pre-transition status is unknown for this legacy merge")
        return transition[1]
    return transition[2]
