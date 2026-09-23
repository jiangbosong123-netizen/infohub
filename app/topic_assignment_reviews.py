from __future__ import annotations

"""Append-only human decisions for immutable topic assignment assertions."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


class TopicAssignmentReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class TopicAssignmentReviewPreview:
    assignment_id: str
    original_status: str
    effective_status: str
    current_review_id: str | None
    current_review_version: int | None
    current_reviewer_id: str | None
    current_reason: str | None
    current_reviewed_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def review_preview(
    db: sqlite3.Connection, assignment_id: str
) -> TopicAssignmentReviewPreview:
    assignment = db.execute(
        "SELECT id,status FROM document_topic_assignments WHERE id=?", (assignment_id,)
    ).fetchone()
    if assignment is None:
        raise TopicAssignmentReviewError("topic assignment does not exist")
    review = db.execute(
        """SELECT id,version,decision,reviewer_id,reason,reviewed_at
           FROM topic_assignment_reviews WHERE assignment_id=?
           ORDER BY version DESC LIMIT 1""",
        (assignment_id,),
    ).fetchone()
    return TopicAssignmentReviewPreview(
        assignment_id=assignment_id,
        original_status=assignment["status"],
        effective_status=review["decision"] if review else assignment["status"],
        current_review_id=review["id"] if review else None,
        current_review_version=review["version"] if review else None,
        current_reviewer_id=review["reviewer_id"] if review else None,
        current_reason=review["reason"] if review else None,
        current_reviewed_at=review["reviewed_at"] if review else None,
    )


def record_topic_assignment_review(
    db: sqlite3.Connection,
    *,
    assignment_id: str,
    decision: str,
    expected_previous_review_id: str | None,
    reviewer_id: str,
    reason: str,
    evidence_ids: tuple[str, ...] = (),
    now: str | None = None,
) -> TopicAssignmentReviewPreview:
    if decision not in {"accepted", "rejected"}:
        raise ValueError("decision must be accepted or rejected")
    reviewer_id, reason = reviewer_id.strip(), reason.strip()
    if not reviewer_id or len(reviewer_id) > 120:
        raise ValueError("reviewer_id must contain 1 to 120 characters")
    if not reason or len(reason) > 1000:
        raise ValueError("reason must contain 1 to 1000 characters")
    if any(not isinstance(value, str) or not value for value in evidence_ids):
        raise ValueError("evidence IDs must be non-empty strings")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("evidence IDs must be unique")
    current = review_preview(db, assignment_id)
    if current.current_review_id != expected_previous_review_id:
        raise TopicAssignmentReviewError(
            "topic assignment review changed; refresh the preview before deciding"
        )
    if evidence_ids:
        marks = ",".join("?" for _ in evidence_ids)
        found = {
            row[0] for row in db.execute(
                f"SELECT id FROM raw_records WHERE id IN ({marks})", evidence_ids
            )
        }
        missing = sorted(set(evidence_ids) - found)
        if missing:
            raise TopicAssignmentReviewError(
                "unknown evidence ID(s): " + ", ".join(missing[:5])
            )
    version = (current.current_review_version or 0) + 1
    db.execute(
        """INSERT INTO topic_assignment_reviews(
               id,assignment_id,version,previous_review_id,decision,reviewer_id,
               reason,evidence_ids_json,reviewed_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            f"topic_review_{uuid4().hex}", assignment_id, version,
            current.current_review_id, decision, reviewer_id, reason,
            json.dumps(list(evidence_ids), ensure_ascii=False, separators=(",", ":")),
            now or utc_now(),
        ),
    )
    return review_preview(db, assignment_id)
