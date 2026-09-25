from __future__ import annotations

"""Append-only, evidence-backed revisions of one stable event identity."""

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


REVISION_METHOD_VERSION = "event-fact-revisions-v1"
TERMINAL_STATUSES = {"merged", "split", "retracted"}
EVENT_TYPES = {
    "model_release", "product_update", "research_result", "earnings", "financing",
    "ma", "personnel", "buyback", "regulation", "litigation", "macro_release",
    "monetary_policy", "other",
}
TIME_PRECISIONS = {"unknown", "year", "month", "day", "minute", "second", "range"}
KNOWLEDGE_STATUSES = {
    "reported", "corroborated", "disputed", "confirmed_by_primary", "unknown",
}
REVISION_KINDS = {"fact_update", "correction", "knowledge_update"}
SEMANTIC_FIELDS = (
    "schema_version", "title", "event_type", "event_time_start", "event_time_end",
    "time_precision", "primary_entities", "object_entities", "facts", "topics",
    "knowledge_status",
)
FACT_FIELDS = {
    "title", "event_type", "event_time_start", "event_time_end", "time_precision",
    "primary_entities", "object_entities", "facts",
}


@dataclass(frozen=True)
class EventRevision:
    schema_version: str
    title: str
    event_type: str
    event_time_start: str | None
    event_time_end: str | None
    time_precision: str
    primary_entities: tuple[str, ...]
    object_entities: tuple[str, ...]
    facts: tuple[dict, ...]
    topics: tuple[str, ...]
    knowledge_status: str


@dataclass(frozen=True, order=True)
class RevisionEvidence:
    document_version_id: str
    evidence_id: str
    role: str = "supports"
    fact_id: str | None = None


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _semantic(revision: EventRevision) -> dict:
    value = asdict(revision)
    for field in ("primary_entities", "object_entities", "facts", "topics"):
        value[field] = list(value[field])
    return value


def _current_semantic(row: sqlite3.Row) -> dict:
    return {
        "schema_version": row["schema_version"], "title": row["title"],
        "event_type": row["event_type"], "event_time_start": row["event_time_start"],
        "event_time_end": row["event_time_end"], "time_precision": row["time_precision"],
        "primary_entities": json.loads(row["primary_entities_json"]),
        "object_entities": json.loads(row["object_entities_json"]),
        "facts": json.loads(row["facts_json"]), "topics": json.loads(row["topics_json"]),
        "knowledge_status": row["knowledge_status"],
    }


def _validate_revision(revision: EventRevision) -> dict:
    value = _semantic(revision)
    if (
        not revision.schema_version.strip()
        or revision.schema_version != revision.schema_version.strip()
        or not revision.title.strip()
        or revision.title != revision.title.strip()
    ):
        raise EventRelationError("event schema version and title are required")
    if revision.event_type not in EVENT_TYPES:
        raise EventRelationError("unsupported event type")
    if revision.time_precision not in TIME_PRECISIONS:
        raise EventRelationError("unsupported event time precision")
    if revision.knowledge_status not in KNOWLEDGE_STATUSES:
        raise EventRelationError("ordinary revisions cannot retract an event")
    if revision.event_time_end is not None and revision.event_time_start is None:
        raise EventRelationError("an event time range requires a start")
    if (
        revision.event_time_start is not None and revision.event_time_end is not None
        and revision.event_time_end < revision.event_time_start
    ):
        raise EventRelationError("event time end precedes its start")
    for field in ("primary_entities", "object_entities", "topics"):
        values = value[field]
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise EventRelationError(f"{field} must contain non-empty IDs")
        if len(values) != len(set(values)):
            raise EventRelationError(f"{field} contains duplicate IDs")
        if values != sorted(values):
            raise EventRelationError(f"{field} IDs must be sorted")
    if any(not isinstance(fact, dict) for fact in value["facts"]):
        raise EventRelationError("facts must be objects")
    fact_ids = [fact.get("fact_id") for fact in value["facts"]]
    if any(not isinstance(fact_id, str) or not fact_id.strip() for fact_id in fact_ids):
        raise EventRelationError("every fact requires a non-empty fact ID")
    if len(fact_ids) != len(set(fact_ids)):
        raise EventRelationError("fact IDs must be unique within an event version")
    _json(value)
    return value


def _validate_evidence(
    db: sqlite3.Connection, evidence: Iterable[RevisionEvidence], facts: list[dict]
) -> tuple[RevisionEvidence, ...]:
    rows = tuple(sorted(set(evidence)))
    if not rows:
        raise EventRelationError("a revision requires version-pinned evidence")
    fact_ids = {fact.get("fact_id") for fact in facts if fact.get("fact_id")}
    for item in rows:
        if item.role not in {"supports", "contradicts", "context"}:
            raise EventRelationError("unsupported revision evidence role")
        if item.fact_id is not None and item.fact_id not in fact_ids:
            raise EventRelationError("revision evidence refers to an unknown fact ID")
        if not db.execute(
            """SELECT 1 FROM document_version_inputs
               WHERE version_id=? AND raw_record_id=?""",
            (item.document_version_id, item.evidence_id),
        ).fetchone():
            raise EventRelationError("revision evidence does not belong to its document version")
    supported_facts = {
        item.fact_id for item in rows
        if item.fact_id is not None and item.role in {"supports", "contradicts"}
    }
    if fact_ids - supported_facts:
        raise EventRelationError("every revised fact requires direct evidence")
    return rows


def publish_event_revision(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    event_id: str,
    expected_event_version_id: str,
    revision: EventRevision,
    evidence: Iterable[RevisionEvidence],
    revision_kind: str,
    reason: str,
    idempotency_key: str,
    now: datetime | str | None = None,
) -> PublicationResult:
    """Append a new current event version and its complete evidence snapshot."""
    clean_reason = _clean_reason(reason)
    if revision_kind not in REVISION_KINDS:
        raise EventRelationError("unsupported event revision kind")
    proposed = _validate_revision(revision)
    requested_evidence = tuple(sorted(set(evidence)))
    new_version_id = _stable_id("event_version", idempotency_key)
    revision_id = _stable_id("event_revision", idempotency_key)

    with get_db() as db:
        existing = _existing_change(db, new_version_id)
        request = {
            "event_id": event_id, "previous_version_id": expected_event_version_id,
            "revision": proposed, "evidence": [asdict(item) for item in requested_evidence],
            "revision_kind": revision_kind, "reason": clean_reason,
        }
        request_sha = _sha({"method_version": REVISION_METHOD_VERSION, "request": request})
        if existing:
            if existing.payload.get("request_sha256") != request_sha:
                raise EventRelationError("revision retry inputs do not match the published revision")
            return publish_job_result(
                job_id=job_id, lease_token=lease_token,
                expected_input_version=expected_input_version, changes=(existing,),
                persist=_retry_persist_should_not_run, now=now,
            )
        if resolve_event(db, event_id).canonical_id != event_id:
            raise EventRelationError("only a canonical event can be revised")
        event = _event_version(db, event_id, expected_event_version_id)
        if event["status"] in TERMINAL_STATUSES:
            raise EventRelationError("a terminal event cannot be revised")
        current = db.execute(
            "SELECT * FROM event_versions WHERE id=?", (expected_event_version_id,)
        ).fetchone()
        previous = _current_semantic(current)
        changed_fields = sorted(
            field for field in SEMANTIC_FIELDS if previous[field] != proposed[field]
        )
        if not changed_fields:
            raise EventRelationError("revision does not change the event")
        if revision_kind == "fact_update" and not (set(changed_fields) & FACT_FIELDS):
            raise EventRelationError("fact_update must change at least one event fact field")
        if revision_kind == "knowledge_update" and changed_fields != ["knowledge_status"]:
            raise EventRelationError("knowledge_update may only change knowledge_status")
        clean_evidence = _validate_evidence(db, requested_evidence, proposed["facts"])

    payload = {
        "schema_version": "event-revision-v1", "request_sha256": request_sha,
        **request, "revised_version_id": new_version_id,
        "changed_fields": changed_fields, "method_version": REVISION_METHOD_VERSION,
    }
    change = ChangeRequest(
        idempotency_key=idempotency_key, resource_type="event", resource_id=event_id,
        version_id=new_version_id, operation="update", payload=payload,
    )

    def persist(db: sqlite3.Connection, changes: tuple[PublishedChange, ...]) -> None:
        if len(changes) != 1 or changes[0].version_id != new_version_id:
            raise EventRelationError("revision publication does not match its change record")
        if resolve_event(db, event_id).canonical_id != event_id:
            raise EventRelationError("event was merged before revision publication")
        event = _event_version(db, event_id, expected_event_version_id)
        if event["status"] in TERMINAL_STATUSES:
            raise EventRelationError("event became terminal before revision publication")
        current = db.execute(
            "SELECT * FROM event_versions WHERE id=?", (expected_event_version_id,)
        ).fetchone()
        if sorted(
            field for field in SEMANTIC_FIELDS
            if _current_semantic(current)[field] != proposed[field]
        ) != changed_fields:
            raise EventRelationError("event revision basis changed before publication")
        evidence_rows = _validate_evidence(db, clean_evidence, proposed["facts"])
        version_semantic = {
            **proposed, "created_by": "event_revision",
            "method_version": REVISION_METHOD_VERSION,
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
                proposed["schema_version"], proposed["title"], proposed["event_type"],
                proposed["event_time_start"], proposed["event_time_end"],
                proposed["time_precision"], _json(proposed["primary_entities"]),
                _json(proposed["object_entities"]), _json(proposed["facts"]),
                _json(proposed["topics"]), proposed["knowledge_status"],
                _sha(version_semantic), changes[0].available_at, "event_revision",
                REVISION_METHOD_VERSION,
            ),
        )
        for item in evidence_rows:
            evidence_key = ":".join((
                new_version_id, item.document_version_id, item.evidence_id,
                item.fact_id or "", item.role,
            ))
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,fact_id,role,
                       available_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    f"event_evidence_{hashlib.sha256(evidence_key.encode()).hexdigest()[:32]}",
                    new_version_id, item.document_version_id, item.evidence_id,
                    item.fact_id, item.role, changes[0].available_at,
                ),
            )
        db.execute(
            """INSERT INTO event_revisions(
                   id,event_id,previous_version_id,revised_version_id,revision_kind,
                   changed_fields_json,evidence_ids_json,reason,available_at,publication_seq
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                revision_id, event_id, expected_event_version_id, new_version_id,
                revision_kind, _json(changed_fields),
                _json(sorted({item.evidence_id for item in evidence_rows})), clean_reason,
                changes[0].available_at, changes[0].seq,
            ),
        )
        fact_change = bool(set(changed_fields) & FACT_FIELDS)
        updated = db.execute(
            """UPDATE events
               SET current_version_id=?,latest_report_at=MAX(latest_report_at,?),
                   last_fact_change_at=CASE WHEN ? THEN ? ELSE last_fact_change_at END
               WHERE id=? AND current_version_id=? AND status=?""",
            (
                new_version_id, changes[0].available_at, fact_change,
                changes[0].available_at, event_id, expected_event_version_id, event["status"],
            ),
        )
        if updated.rowcount != 1:
            raise EventRelationError("event changed before revision publication")

    return publish_job_result(
        job_id=job_id, lease_token=lease_token,
        expected_input_version=expected_input_version, changes=(change,),
        persist=persist, now=now,
    )
