from __future__ import annotations

"""Explicit human admission gate for one immutable statistics publication."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


class TopicStatisticsAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovedTopicStatisticsAdmission:
    review_id: str
    review_version: int
    reviewed_at: str
    metrics_sha256: str
    minimum_decided_assignment_bps: int
    allow_zero_members: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TopicStatisticsAdmissionPreview:
    publication_id: str
    publication_version: int
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


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _metrics(db: sqlite3.Connection, publication: sqlite3.Row) -> dict:
    build_id = publication["build_id"]
    dataset_id = publication["dataset_id"]
    effective_rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT COALESCE(review.decision,assignment.status) AS effective_status,
                  COUNT(*) AS count
           FROM document_topic_assignments AS assignment
           JOIN document_versions AS version ON version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE document.dataset_id=?
           GROUP BY COALESCE(review.decision,assignment.status)""",
        (dataset_id,),
    ).fetchall()
    effective = {row["effective_status"]: row["count"] for row in effective_rows}
    total = sum(effective.values())
    decided = effective.get("accepted", 0) + effective.get("rejected", 0)
    reviewed = db.execute(
        """SELECT COUNT(*) FROM topic_assignment_reviews AS review
           JOIN document_topic_assignments AS assignment ON assignment.id=review.assignment_id
           JOIN document_versions AS version ON version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           WHERE document.dataset_id=? AND NOT EXISTS(
               SELECT 1 FROM topic_assignment_reviews AS later
               WHERE later.assignment_id=review.assignment_id
                 AND later.version>review.version
           )""",
        (dataset_id,),
    ).fetchone()[0]
    accepted_topics = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT COUNT(DISTINCT topic.topic_id)
           FROM document_topic_assignments AS assignment
           JOIN topic_versions AS topic ON topic.id=assignment.topic_version_id
           JOIN document_versions AS version ON version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE document.dataset_id=?
             AND COALESCE(review.decision,assignment.status)='accepted'""",
        (dataset_id,),
    ).fetchone()[0]
    published = db.execute(
        """SELECT COUNT(*) AS topics,COALESCE(SUM(document_count),0) AS documents,
                  COALESCE(SUM(event_count),0) AS events
           FROM topic_statistics_versions WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    return {
        "publication_id": publication["publication_id"],
        "publication_version": publication["publication_version"],
        "build_id": build_id,
        "dataset_id": dataset_id,
        "assignment_total": total,
        "effective_accepted": effective.get("accepted", 0),
        "effective_rejected": effective.get("rejected", 0),
        "effective_candidate": effective.get("candidate", 0),
        "effective_superseded": effective.get("superseded", 0),
        "reviewed_assignments": reviewed,
        "decided_assignment_bps": 10_000 if total == 0 else decided * 10_000 // total,
        "human_review_bps": 10_000 if total == 0 else reviewed * 10_000 // total,
        "catalog_topics": db.execute(
            "SELECT COUNT(*) FROM topic_catalog WHERE dataset_id=?", (dataset_id,)
        ).fetchone()[0],
        "topics_with_accepted_documents": accepted_topics,
        "stable_public_events": db.execute(
            "SELECT COUNT(*) FROM events WHERE dataset_id=? AND status IN ('active','resolved')",
            (dataset_id,),
        ).fetchone()[0],
        "published_topics": published["topics"],
        "published_document_members": published["documents"],
        "published_event_members": published["events"],
        "dirty_topics": db.execute(
            "SELECT COUNT(*) FROM topic_statistics_dirty"
        ).fetchone()[0],
    }


def admission_preview(
    db: sqlite3.Connection, publication_id: str
) -> TopicStatisticsAdmissionPreview:
    publication = db.execute(
        """SELECT publication.id AS publication_id,
                  publication.version AS publication_version,publication.build_id,
                  build.dataset_id
           FROM topic_statistics_publications AS publication
           JOIN topic_statistics_builds AS build ON build.id=publication.build_id
           JOIN topic_statistics_state AS state
             ON state.current_publication_id=publication.id AND state.current_build_id=build.id
           WHERE publication.id=? AND state.status='ready' AND build.status='ready'""",
        (publication_id,),
    ).fetchone()
    if publication is None:
        raise TopicStatisticsAdmissionError(
            "admission review requires the current ready publication"
        )
    metrics = _metrics(db, publication)
    digest = hashlib.sha256(_canonical(metrics)).hexdigest()
    review = db.execute(
        """SELECT id,version,decision,reviewer_id,reason,reviewed_at
           FROM topic_statistics_admission_reviews WHERE publication_id=?
           ORDER BY version DESC LIMIT 1""",
        (publication_id,),
    ).fetchone()
    return TopicStatisticsAdmissionPreview(
        publication_id=publication_id,
        publication_version=publication["publication_version"],
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
    publication_id: str,
    decision: str,
    expected_previous_review_id: str | None,
    minimum_decided_assignment_bps: int,
    allow_zero_members: bool,
    reviewer_id: str,
    reason: str,
    now: str | None = None,
) -> TopicStatisticsAdmissionPreview:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    if not 0 <= minimum_decided_assignment_bps <= 10_000:
        raise ValueError("minimum_decided_assignment_bps must be between 0 and 10000")
    if not isinstance(allow_zero_members, bool):
        raise ValueError("allow_zero_members must be a boolean")
    reviewer_id, reason = reviewer_id.strip(), reason.strip()
    if not reviewer_id or len(reviewer_id) > 120:
        raise ValueError("reviewer_id must contain 1 to 120 characters")
    if not reason or len(reason) > 1000:
        raise ValueError("reason must contain 1 to 1000 characters")
    preview = admission_preview(db, publication_id)
    if preview.current_review_id != expected_previous_review_id:
        raise TopicStatisticsAdmissionError(
            "admission review changed; refresh the preview before deciding"
        )
    if decision == "approved":
        if preview.metrics["dirty_topics"]:
            raise TopicStatisticsAdmissionError("dirty topic inputs cannot be approved")
        if preview.metrics["decided_assignment_bps"] < minimum_decided_assignment_bps:
            raise TopicStatisticsAdmissionError(
                "decided assignment coverage is below the admission threshold"
            )
        member_count = (
            preview.metrics["published_document_members"]
            + preview.metrics["published_event_members"]
        )
        if member_count == 0 and not allow_zero_members:
            raise TopicStatisticsAdmissionError(
                "zero-member publication requires an explicit allow_zero_members decision"
            )
    version = (preview.current_review_version or 0) + 1
    db.execute(
        """INSERT INTO topic_statistics_admission_reviews(
               id,publication_id,version,previous_review_id,decision,
               minimum_decided_assignment_bps,allow_zero_members,metrics_json,
               metrics_sha256,reviewer_id,reason,reviewed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"topic_statistics_admission_{uuid4().hex}", publication_id, version,
            preview.current_review_id, decision, minimum_decided_assignment_bps,
            int(allow_zero_members), _canonical(preview.metrics).decode("utf-8"),
            preview.metrics_sha256, reviewer_id, reason, now or utc_now(),
        ),
    )
    return admission_preview(db, publication_id)


def approved_admission(
    db: sqlite3.Connection, publication_id: str
) -> ApprovedTopicStatisticsAdmission:
    """Return the current valid approval or fail closed.

    Callers should hold a read transaction so the publication, metrics, and
    admission row belong to one SQLite snapshot.
    """
    preview = admission_preview(db, publication_id)
    review = db.execute(
        """SELECT id,version,decision,minimum_decided_assignment_bps,
                  allow_zero_members,metrics_sha256,reviewed_at
           FROM topic_statistics_admission_reviews
           WHERE publication_id=? ORDER BY version DESC LIMIT 1""",
        (publication_id,),
    ).fetchone()
    if review is None or review["decision"] != "approved":
        raise TopicStatisticsAdmissionError(
            "topic statistics publication has no current approval"
        )
    if review["metrics_sha256"] != preview.metrics_sha256:
        raise TopicStatisticsAdmissionError(
            "topic statistics approval metrics are stale"
        )
    if (
        preview.metrics["dirty_topics"] != 0
        or preview.metrics["decided_assignment_bps"]
           < review["minimum_decided_assignment_bps"]
    ):
        raise TopicStatisticsAdmissionError(
            "topic statistics no longer meet the approved release policy"
        )
    members = (
        preview.metrics["published_document_members"]
        + preview.metrics["published_event_members"]
    )
    if members == 0 and not review["allow_zero_members"]:
        raise TopicStatisticsAdmissionError(
            "zero-member topic statistics publication is not approved"
        )
    return ApprovedTopicStatisticsAdmission(
        review_id=review["id"], review_version=review["version"],
        reviewed_at=review["reviewed_at"], metrics_sha256=review["metrics_sha256"],
        minimum_decided_assignment_bps=review["minimum_decided_assignment_bps"],
        allow_zero_members=bool(review["allow_zero_members"]),
    )
