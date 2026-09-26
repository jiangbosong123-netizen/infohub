from __future__ import annotations

"""Append-only human review of immutable event match decisions."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


class EventMatchReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class EventMatchReviewPreview:
    queue_sequence: int
    decision_id: str
    machine_decision: str
    matcher_version: str
    score: float | None
    reason: str
    input_version_ids: tuple[str, ...]
    candidate_event_version_ids: tuple[str, ...]
    current_review_id: str | None
    current_review_version: int | None
    current_decision: str | None
    current_reviewer_id: str | None
    current_reason: str | None
    current_reviewed_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _ids(value: str, field: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventMatchReviewError(f"{field} is not valid JSON") from exc
    if (
        not isinstance(decoded, list)
        or any(not isinstance(item, str) or not item.strip() for item in decoded)
        or decoded != sorted(set(decoded))
    ):
        raise EventMatchReviewError(f"{field} must contain sorted unique non-empty IDs")
    return tuple(decoded)


def review_preview(db: sqlite3.Connection, decision_id: str) -> EventMatchReviewPreview:
    row = db.execute(
        """SELECT queue.sequence,decision.*
           FROM match_decisions AS decision
           JOIN event_match_review_queue AS queue ON queue.decision_id=decision.id
           WHERE decision.id=?""",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise EventMatchReviewError("match decision does not exist or is not queued")
    review = db.execute(
        """SELECT * FROM event_match_reviews
           WHERE decision_id=? ORDER BY version DESC LIMIT 1""",
        (decision_id,),
    ).fetchone()
    return EventMatchReviewPreview(
        queue_sequence=row["sequence"], decision_id=decision_id,
        machine_decision=row["decision"], matcher_version=row["matcher_version"],
        score=row["score"], reason=row["reason"],
        input_version_ids=_ids(row["input_versions_json"], "input versions"),
        candidate_event_version_ids=_ids(
            row["candidate_event_versions_json"], "candidate event versions"
        ),
        current_review_id=review["id"] if review else None,
        current_review_version=review["version"] if review else None,
        current_decision=review["decision"] if review else None,
        current_reviewer_id=review["reviewer_id"] if review else None,
        current_reason=review["reason"] if review else None,
        current_reviewed_at=review["reviewed_at"] if review else None,
    )


def review_queue(
    db: sqlite3.Connection,
    *,
    after_sequence: int = 0,
    limit: int = 50,
    pending_only: bool = True,
) -> tuple[EventMatchReviewPreview, ...]:
    if after_sequence < 0:
        raise ValueError("after_sequence cannot be negative")
    if not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")
    rows = db.execute(
        """SELECT queue.decision_id
           FROM event_match_review_queue AS queue
           WHERE queue.sequence>?
             AND (?=0 OR NOT EXISTS(
                 SELECT 1 FROM event_match_reviews AS review
                 WHERE review.decision_id=queue.decision_id))
           ORDER BY queue.sequence LIMIT ?""",
        (after_sequence, int(pending_only), limit),
    ).fetchall()
    return tuple(review_preview(db, row["decision_id"]) for row in rows)


def _validate_evidence(
    db: sqlite3.Connection,
    preview: EventMatchReviewPreview,
    evidence_ids: tuple[str, ...],
) -> None:
    if not evidence_ids:
        raise EventMatchReviewError("event match review requires raw evidence")
    for evidence_id in evidence_ids:
        if not any(
            db.execute(
                """SELECT 1 FROM document_version_inputs
                   WHERE version_id=? AND raw_record_id=?""",
                (version_id, evidence_id),
            ).fetchone()
            for version_id in preview.input_version_ids
        ):
            raise EventMatchReviewError(
                "review evidence does not belong to a decision input version"
            )
    if preview.machine_decision == "candidate_link":
        if not db.execute(
            "SELECT 1 FROM document_event_links WHERE decision_id=?",
            (preview.decision_id,),
        ).fetchone():
            raise EventMatchReviewError("candidate link decision has no event link")
        for evidence_id in evidence_ids:
            if not any(
                db.execute(
                    """SELECT 1 FROM event_evidence
                       WHERE event_version_id=? AND evidence_id=?""",
                    (event_version_id, evidence_id),
                ).fetchone()
                for event_version_id in preview.candidate_event_version_ids
            ):
                raise EventMatchReviewError(
                    "candidate link review evidence is not attached to its event version"
                )


def record_match_review(
    db: sqlite3.Connection,
    *,
    decision_id: str,
    decision: str,
    expected_previous_review_id: str | None,
    evidence_ids: tuple[str, ...] | list[str],
    reviewer_id: str,
    reason: str,
    now: str | None = None,
) -> EventMatchReviewPreview:
    if decision not in {"accepted", "rejected"}:
        raise ValueError("review decision must be accepted or rejected")
    reviewer_id, reason = reviewer_id.strip(), reason.strip()
    if not reviewer_id or len(reviewer_id) > 120:
        raise ValueError("reviewer_id must contain 1 to 120 characters")
    if not reason or len(reason) > 1000:
        raise ValueError("reason must contain 1 to 1000 characters")
    supplied_evidence = tuple(evidence_ids)
    if any(
        not isinstance(item, str) or not item.strip() for item in supplied_evidence
    ):
        raise ValueError("evidence IDs must be non-empty strings")
    evidence = tuple(sorted(set(supplied_evidence)))
    preview = review_preview(db, decision_id)
    if preview.current_review_id != expected_previous_review_id:
        raise EventMatchReviewError("match review changed; refresh before deciding")
    _validate_evidence(db, preview, evidence)
    version = (preview.current_review_version or 0) + 1
    db.execute(
        """INSERT INTO event_match_reviews(
               id,decision_id,version,previous_review_id,decision,evidence_ids_json,
               reviewer_id,reason,reviewed_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            f"event_match_review_{uuid4().hex}", decision_id, version,
            preview.current_review_id, decision,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            reviewer_id, reason, now or utc_now(),
        ),
    )
    return review_preview(db, decision_id)


def effective_match_status(db: sqlite3.Connection, decision_id: str) -> str:
    row = db.execute(
        """SELECT COALESCE(
                 (SELECT review.decision FROM event_match_reviews AS review
                  WHERE review.decision_id=decision.id
                  ORDER BY review.version DESC LIMIT 1),
                 decision.review_status) AS status
           FROM match_decisions AS decision WHERE decision.id=?""",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise EventMatchReviewError("match decision does not exist")
    return row["status"]


def verify_match_reviews(db: sqlite3.Connection) -> None:
    missing = db.execute(
        """SELECT COUNT(*) FROM match_decisions AS decision
           LEFT JOIN event_match_review_queue AS queue ON queue.decision_id=decision.id
           WHERE queue.decision_id IS NULL"""
    ).fetchone()[0]
    extra = db.execute(
        """SELECT COUNT(*) FROM event_match_review_queue AS queue
           LEFT JOIN match_decisions AS decision ON decision.id=queue.decision_id
           WHERE decision.id IS NULL"""
    ).fetchone()[0]
    if missing or extra:
        raise EventMatchReviewError("event match review queue is incomplete")
    for review in db.execute("SELECT * FROM event_match_reviews"):
        preview = review_preview(db, review["decision_id"])
        evidence = _ids(review["evidence_ids_json"], "review evidence")
        _validate_evidence(db, preview, evidence)
