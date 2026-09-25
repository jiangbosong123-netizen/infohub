from __future__ import annotations

"""Append-only, fail-closed admission of event versions for public use."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


POLICY_VERSION = "event-admission-v1"
PUBLIC_STATES = {"reported", "corroborated", "confirmed"}
KNOWLEDGE_BY_STATE = {
    "reported": {"reported", "corroborated", "confirmed_by_primary"},
    "corroborated": {"corroborated", "confirmed_by_primary"},
    "confirmed": {"confirmed_by_primary"},
}
TERMINAL_EVENT_STATUSES = {"merged", "split", "retracted"}


class EventAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class EventAdmissionPreview:
    event_id: str
    event_version_id: str
    metrics: dict
    metrics_sha256: str
    current_review_id: str | None
    current_review_version: int | None
    current_decision: str | None
    current_reviewer_id: str | None
    current_reason: str | None
    current_reviewed_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AdmittedEvent:
    event_id: str
    event_version_id: str
    public_state: str
    review_id: str
    review_version: int
    reviewed_at: str
    metrics_sha256: str
    policy_version: str

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_ids(value: str, field: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventAdmissionError(f"event {field} is not valid JSON") from exc
    if (
        not isinstance(decoded, list)
        or any(not isinstance(item, str) or not item.strip() for item in decoded)
        or decoded != sorted(set(decoded))
    ):
        raise EventAdmissionError(f"event {field} must be sorted unique IDs")
    return decoded


def _decode_facts(value: str) -> list[dict]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventAdmissionError("event facts are not valid JSON") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        raise EventAdmissionError("event facts must be objects")
    fact_ids = [item.get("fact_id") for item in decoded]
    if (
        any(not isinstance(item, str) or not item.strip() for item in fact_ids)
        or len(fact_ids) != len(set(fact_ids))
    ):
        raise EventAdmissionError("event facts require unique non-empty fact IDs")
    return decoded


def _event_semantic(version: sqlite3.Row) -> dict:
    return {
        "schema_version": version["schema_version"],
        "title": version["title"],
        "event_type": version["event_type"],
        "event_time_start": version["event_time_start"],
        "event_time_end": version["event_time_end"],
        "time_precision": version["time_precision"],
        "primary_entities": _decode_ids(version["primary_entities_json"], "primary entities"),
        "object_entities": _decode_ids(version["object_entities_json"], "object entities"),
        "facts": _decode_facts(version["facts_json"]),
        "topics": _decode_ids(version["topics_json"], "topics"),
        "knowledge_status": version["knowledge_status"],
        "created_by": version["created_by"],
        "method_version": version["method_version"],
    }


def _metrics(db: sqlite3.Connection, event_version_id: str) -> dict:
    row = db.execute(
        """SELECT event.id AS event_id,event.dataset_id,event.current_version_id,
                  event.status AS event_status,version.*
           FROM event_versions AS version
           JOIN events AS event ON event.id=version.event_id
           WHERE version.id=?""",
        (event_version_id,),
    ).fetchone()
    if row is None:
        raise EventAdmissionError("event version does not exist")
    semantic = _event_semantic(row)
    primary_entities = semantic["primary_entities"]
    object_entities = semantic["object_entities"]
    topics = semantic["topics"]
    entity_ids = sorted(set(primary_entities + object_entities))
    entity_valid = 0
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        entity_valid = db.execute(
            f"""SELECT COUNT(*) FROM entities AS entity
                 JOIN entity_versions AS version ON version.id=entity.current_version_id
                 WHERE entity.id IN ({placeholders}) AND entity.status='active'
                   AND version.status='active'""",
            entity_ids,
        ).fetchone()[0]
    topic_valid = 0
    if topics:
        placeholders = ",".join("?" for _ in topics)
        topic_valid = db.execute(
            f"""SELECT COUNT(*) FROM topic_versions AS version
                 JOIN topic_catalog AS topic ON topic.current_version_id=version.id
                 WHERE version.id IN ({placeholders}) AND topic.status='active'
                   AND version.status='active'""",
            topics,
        ).fetchone()[0]
    evidence = db.execute(
        """SELECT evidence.document_version_id,evidence.evidence_id,evidence.fact_id,
                  evidence.role,
                  EXISTS(SELECT 1 FROM document_version_inputs AS input
                         WHERE input.version_id=evidence.document_version_id
                           AND input.raw_record_id=evidence.evidence_id) AS valid_pair
           FROM event_evidence AS evidence WHERE evidence.event_version_id=?""",
        (event_version_id,),
    ).fetchall()
    support = [item for item in evidence if item["role"] == "supports"]
    facts = {item["fact_id"] for item in semantic["facts"]}
    supported_facts = {item["fact_id"] for item in support if item["fact_id"]}
    link_statuses = db.execute(
        """SELECT decision.review_status,COUNT(*) AS count
           FROM document_event_links AS link
           JOIN match_decisions AS decision ON decision.id=link.decision_id
           WHERE link.event_version_id=?
             AND NOT EXISTS(SELECT 1 FROM document_event_links AS later
                            WHERE later.supersedes_link_id=link.id)
           GROUP BY decision.review_status""",
        (event_version_id,),
    ).fetchall()
    links = {item["review_status"]: item["count"] for item in link_statuses}
    verified_publishers = db.execute(
        """SELECT COUNT(DISTINCT attribution.publisher_id)
           FROM event_evidence AS evidence
           JOIN document_attributions AS attribution
             ON attribution.document_version_id=evidence.document_version_id
            AND attribution.evidence_id=evidence.evidence_id
            AND attribution.relation='original' AND attribution.status='verified'
           JOIN publishers AS publisher ON publisher.id=attribution.publisher_id
           JOIN publisher_versions AS publisher_version
             ON publisher_version.id=publisher.current_version_id
           WHERE evidence.event_version_id=? AND evidence.role='supports'
             AND attribution.publisher_id IS NOT NULL
             AND publisher.status='active' AND publisher_version.status='active'""",
        (event_version_id,),
    ).fetchone()[0]
    verified_primary_publishers = 0
    if primary_entities:
        placeholders = ",".join("?" for _ in primary_entities)
        verified_primary_publishers = db.execute(
            f"""SELECT COUNT(DISTINCT attribution.publisher_id)
                 FROM event_evidence AS evidence
                 JOIN document_attributions AS attribution
                   ON attribution.document_version_id=evidence.document_version_id
                  AND attribution.evidence_id=evidence.evidence_id
                  AND attribution.relation='original' AND attribution.status='verified'
                 JOIN publishers AS publisher ON publisher.id=attribution.publisher_id
                 JOIN publisher_versions AS publisher_version
                   ON publisher_version.id=publisher.current_version_id
                 WHERE evidence.event_version_id=? AND evidence.role='supports'
                   AND publisher.status='active' AND publisher_version.status='active'
                   AND publisher.organization_entity_id IN ({placeholders})""",
            (event_version_id, *primary_entities),
        ).fetchone()[0]
    return {
        "event_id": row["event_id"],
        "event_version_id": event_version_id,
        "dataset_id": row["dataset_id"],
        "is_current_version": row["current_version_id"] == event_version_id,
        "event_status": row["event_status"],
        "knowledge_status": row["knowledge_status"],
        "version_sha256_valid": hashlib.sha256(_canonical(semantic)).hexdigest()
        == row["version_sha256"],
        "entity_references": len(entity_ids),
        "valid_entity_references": entity_valid,
        "topic_references": len(topics),
        "valid_topic_references": topic_valid,
        "primary_entities": len(primary_entities),
        "evidence_records": len(evidence),
        "valid_evidence_pairs": sum(bool(item["valid_pair"]) for item in evidence),
        "supporting_evidence": len(support),
        "supporting_documents": len({item["document_version_id"] for item in support}),
        "contradicting_evidence": sum(item["role"] == "contradicts" for item in evidence),
        "facts": len(facts),
        "supported_facts": len(facts & supported_facts),
        "active_links": sum(links.values()),
        "accepted_links": links.get("accepted", 0),
        "pending_links": links.get("pending", 0),
        "rejected_links": links.get("rejected", 0),
        "verified_original_publishers": verified_publishers,
        "verified_primary_publishers": verified_primary_publishers,
    }


def _qualification_error(metrics: dict, decision: str) -> str | None:
    if decision not in PUBLIC_STATES:
        return "decision is not a public event state"
    if not metrics.get("is_current_version"):
        return "only the current event version can be admitted"
    if metrics.get("event_status") in TERMINAL_EVENT_STATUSES:
        return "terminal events cannot be admitted"
    if not metrics.get("version_sha256_valid"):
        return "event version hash is invalid"
    if metrics.get("entity_references") != metrics.get("valid_entity_references"):
        return "event entity references are not current active identities"
    if metrics.get("topic_references") != metrics.get("valid_topic_references"):
        return "event topic references are not current active versions"
    if metrics.get("evidence_records") != metrics.get("valid_evidence_pairs"):
        return "event evidence does not belong to its document version"
    if metrics.get("supporting_evidence", 0) < 1:
        return "public events require supporting evidence"
    if metrics.get("facts") != metrics.get("supported_facts"):
        return "every event fact requires direct supporting evidence"
    if metrics.get("pending_links", 0):
        return "pending candidate links prevent event admission"
    if metrics.get("rejected_links", 0):
        return "rejected candidate links prevent event admission"
    if metrics.get("accepted_links", 0) < 1:
        return "event admission requires an accepted document link"
    if metrics.get("knowledge_status") not in KNOWLEDGE_BY_STATE[decision]:
        return f"event knowledge status does not support {decision} publication"
    if decision == "corroborated" and (
        metrics.get("supporting_documents", 0) < 2
        or metrics.get("verified_original_publishers", 0) < 2
    ):
        return "corroborated events require two verified independent publishers"
    if decision == "confirmed" and (
        metrics.get("primary_entities", 0) < 1
        or metrics.get("verified_primary_publishers", 0) < 1
    ):
        return "confirmed events require verified evidence from a primary entity publisher"
    return None


def admission_preview(
    db: sqlite3.Connection, event_version_id: str
) -> EventAdmissionPreview:
    metrics = _metrics(db, event_version_id)
    digest = hashlib.sha256(_canonical(metrics)).hexdigest()
    review = db.execute(
        """SELECT * FROM event_admission_reviews
           WHERE event_version_id=? ORDER BY version DESC LIMIT 1""",
        (event_version_id,),
    ).fetchone()
    return EventAdmissionPreview(
        event_id=metrics["event_id"], event_version_id=event_version_id,
        metrics=metrics, metrics_sha256=digest,
        current_review_id=review["id"] if review else None,
        current_review_version=review["version"] if review else None,
        current_decision=review["decision"] if review else None,
        current_reviewer_id=review["reviewer_id"] if review else None,
        current_reason=review["reason"] if review else None,
        current_reviewed_at=review["reviewed_at"] if review else None,
    )


def record_admission_review(
    db: sqlite3.Connection,
    *,
    event_version_id: str,
    decision: str,
    expected_previous_review_id: str | None,
    reviewer_id: str,
    reason: str,
    now: str | None = None,
) -> EventAdmissionPreview:
    if decision not in PUBLIC_STATES | {"rejected"}:
        raise ValueError("unsupported event admission decision")
    reviewer_id, reason = reviewer_id.strip(), reason.strip()
    if not reviewer_id or len(reviewer_id) > 120:
        raise ValueError("reviewer_id must contain 1 to 120 characters")
    if not reason or len(reason) > 1000:
        raise ValueError("reason must contain 1 to 1000 characters")
    preview = admission_preview(db, event_version_id)
    if preview.current_review_id != expected_previous_review_id:
        raise EventAdmissionError("event admission changed; refresh the preview before deciding")
    if decision != "rejected":
        error = _qualification_error(preview.metrics, decision)
        if error:
            raise EventAdmissionError(error)
    version = (preview.current_review_version or 0) + 1
    db.execute(
        """INSERT INTO event_admission_reviews(
               id,event_version_id,version,previous_review_id,decision,
               metrics_json,metrics_sha256,reviewer_id,reason,reviewed_at,policy_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"event_admission_{uuid4().hex}", event_version_id, version,
            preview.current_review_id, decision,
            _canonical(preview.metrics).decode("utf-8"), preview.metrics_sha256,
            reviewer_id, reason, now or utc_now(), POLICY_VERSION,
        ),
    )
    return admission_preview(db, event_version_id)


def admitted_event(db: sqlite3.Connection, event_id: str) -> AdmittedEvent:
    event = db.execute(
        "SELECT current_version_id FROM events WHERE id=?", (event_id,)
    ).fetchone()
    if event is None or not event["current_version_id"]:
        raise EventAdmissionError("event has no current version")
    preview = admission_preview(db, event["current_version_id"])
    review = db.execute(
        """SELECT * FROM event_admission_reviews
           WHERE event_version_id=? ORDER BY version DESC LIMIT 1""",
        (event["current_version_id"],),
    ).fetchone()
    if review is None or review["decision"] == "rejected":
        raise EventAdmissionError("event has no current public admission")
    if review["policy_version"] != POLICY_VERSION:
        raise EventAdmissionError("event admission uses an unsupported policy")
    if review["metrics_sha256"] != preview.metrics_sha256:
        raise EventAdmissionError("event admission evidence is stale")
    error = _qualification_error(preview.metrics, review["decision"])
    if error:
        raise EventAdmissionError(error)
    return AdmittedEvent(
        event_id=event_id, event_version_id=event["current_version_id"],
        public_state=review["decision"], review_id=review["id"],
        review_version=review["version"], reviewed_at=review["reviewed_at"],
        metrics_sha256=review["metrics_sha256"], policy_version=review["policy_version"],
    )


def verify_event_admission_reviews(db: sqlite3.Connection) -> None:
    for review in db.execute("SELECT * FROM event_admission_reviews"):
        try:
            metrics = json.loads(review["metrics_json"])
            digest = hashlib.sha256(_canonical(metrics)).hexdigest()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EventAdmissionError("event admission metrics are invalid") from exc
        if (
            not isinstance(metrics, dict)
            or digest != review["metrics_sha256"]
            or metrics.get("event_version_id") != review["event_version_id"]
            or review["policy_version"] != POLICY_VERSION
        ):
            raise EventAdmissionError("event admission review integrity check failed")
        if review["decision"] != "rejected":
            error = _qualification_error(metrics, review["decision"])
            if error:
                raise EventAdmissionError(error)
