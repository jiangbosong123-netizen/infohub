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


@dataclass(frozen=True)
class TopicAssignmentReviewQueueItem:
    queue_sequence: int
    assignment_id: str
    document_id: str
    document_version_id: str
    title: str
    canonical_url: str | None
    published_at: str | None
    topic_id: str
    topic_version_id: str
    topic_slug: str
    topic_name: str
    method: str
    method_version: str
    original_status: str
    evidence_ids: tuple[str, ...]
    legacy_evidence: str | None
    available_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TopicReviewCoverage:
    topic_id: str
    topic_slug: str
    topic_name: str
    assignment_total: int
    accepted: int
    rejected: int
    candidate: int
    superseded: int
    human_reviewed: int
    decided_assignment_bps: int
    human_review_bps: int

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


def review_queue(
    db: sqlite3.Connection,
    *,
    after_sequence: int = 0,
    limit: int = 50,
    topic_id: str | None = None,
) -> tuple[TopicAssignmentReviewQueueItem, ...]:
    if not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")
    if not isinstance(after_sequence, int) or after_sequence < 0:
        raise ValueError("after_sequence must be a non-negative integer")
    if topic_id is not None and (not isinstance(topic_id, str) or not topic_id):
        raise ValueError("topic_id must be a non-empty string")
    rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT queue.sequence AS queue_sequence,
                  assignment.id AS assignment_id,document.id AS document_id,
                  version.id AS document_version_id,version.title_original,
                  version.canonical_url,item.published_at,topic.id AS topic_id,
                  topic_version.id AS topic_version_id,topic_version.slug AS topic_slug,
                  topic_version.name AS topic_name,assignment.method,
                  assignment.method_version,assignment.status,
                  assignment.evidence_ids_json,assignment.available_at,
                  snapshot.evidence_text AS legacy_evidence
           FROM topic_assignment_review_queue AS queue
           JOIN document_topic_assignments AS assignment ON assignment.id=queue.assignment_id
           JOIN document_versions AS version ON version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           LEFT JOIN items AS item ON item.id=document.legacy_item_id
           JOIN topic_versions AS topic_version ON topic_version.id=assignment.topic_version_id
           JOIN topic_catalog AS topic ON topic.id=topic_version.topic_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           LEFT JOIN legacy_topic_assignment_mappings AS mapping
             ON mapping.assignment_id=assignment.id
           LEFT JOIN legacy_topic_assignment_snapshot AS snapshot
             ON snapshot.item_id=mapping.item_id AND snapshot.topic_slug=mapping.topic_slug
           WHERE queue.sequence>? AND COALESCE(review.decision,assignment.status)='candidate'
             AND (? IS NULL OR topic.id=?)
           ORDER BY queue.sequence LIMIT ?""",
        (after_sequence, topic_id, topic_id, limit),
    ).fetchall()
    return tuple(TopicAssignmentReviewQueueItem(
        queue_sequence=row["queue_sequence"], assignment_id=row["assignment_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"], title=row["title_original"],
        canonical_url=row["canonical_url"], published_at=row["published_at"],
        topic_id=row["topic_id"], topic_version_id=row["topic_version_id"],
        topic_slug=row["topic_slug"], topic_name=row["topic_name"],
        method=row["method"], method_version=row["method_version"],
        original_status=row["status"],
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        legacy_evidence=row["legacy_evidence"], available_at=row["available_at"],
    ) for row in rows)


def topic_review_coverage(db: sqlite3.Connection) -> tuple[TopicReviewCoverage, ...]:
    rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           ), counts AS (
               SELECT topic_version.topic_id,
                      COUNT(assignment.id) AS assignment_total,
                      SUM(CASE WHEN COALESCE(review.decision,assignment.status)='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN COALESCE(review.decision,assignment.status)='rejected' THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN COALESCE(review.decision,assignment.status)='candidate' THEN 1 ELSE 0 END) AS candidate,
                      SUM(CASE WHEN COALESCE(review.decision,assignment.status)='superseded' THEN 1 ELSE 0 END) AS superseded,
                      SUM(CASE WHEN review.id IS NOT NULL THEN 1 ELSE 0 END) AS human_reviewed
               FROM document_topic_assignments AS assignment
               JOIN topic_versions AS topic_version ON topic_version.id=assignment.topic_version_id
               LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
               GROUP BY topic_version.topic_id
           )
           SELECT topic.id AS topic_id,version.slug,version.name,
                  COALESCE(counts.assignment_total,0) AS assignment_total,
                  COALESCE(counts.accepted,0) AS accepted,
                  COALESCE(counts.rejected,0) AS rejected,
                  COALESCE(counts.candidate,0) AS candidate,
                  COALESCE(counts.superseded,0) AS superseded,
                  COALESCE(counts.human_reviewed,0) AS human_reviewed
           FROM topic_catalog AS topic
           JOIN topic_versions AS version ON version.id=topic.current_version_id
           LEFT JOIN counts ON counts.topic_id=topic.id
           ORDER BY version.group_key,version.name,topic.id"""
    ).fetchall()
    result = []
    for row in rows:
        total = row["assignment_total"]
        decided = row["accepted"] + row["rejected"]
        result.append(TopicReviewCoverage(
            topic_id=row["topic_id"], topic_slug=row["slug"], topic_name=row["name"],
            assignment_total=total, accepted=row["accepted"], rejected=row["rejected"],
            candidate=row["candidate"], superseded=row["superseded"],
            human_reviewed=row["human_reviewed"],
            decided_assignment_bps=10_000 if total == 0 else decided * 10_000 // total,
            human_review_bps=(
                10_000 if total == 0 else row["human_reviewed"] * 10_000 // total
            ),
        ))
    return tuple(result)


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
