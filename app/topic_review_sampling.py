from __future__ import annotations

"""Immutable, reproducible, per-topic samples for human assignment review."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


class TopicReviewSamplingError(RuntimeError):
    pass


@dataclass(frozen=True)
class TopicReviewSampleBatch:
    batch_id: str
    dataset_id: str
    seed: str
    per_topic_limit: int
    assignment_cutoff_sequence: int
    review_cutoff_sequence: int
    candidate_count: int
    topic_count: int
    member_count: int
    manifest_sha256: str
    created_by: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TopicReviewSampleItem:
    ordinal: int
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
    effective_status: str
    current_review_id: str | None
    evidence_ids: tuple[str, ...]
    legacy_evidence: str | None
    available_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TopicReviewSampleTopicResult:
    topic_id: str
    topic_slug: str
    topic_name: str
    selected: int
    pending: int
    accepted: int
    rejected: int
    decided_bps: int
    acceptance_bps: int | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TopicReviewSampleReport:
    batch: TopicReviewSampleBatch
    pending: int
    accepted: int
    rejected: int
    decided_bps: int
    acceptance_bps: int | None
    topics: tuple[TopicReviewSampleTopicResult, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["batch"] = self.batch.to_dict()
        value["topics"] = [topic.to_dict() for topic in self.topics]
        return value


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _selection_sha256(
    dataset_id: str, seed: str, topic_id: str, assignment_id: str, queue_sequence: int
) -> str:
    return _canonical_sha256({
        "assignment_id": assignment_id,
        "dataset_id": dataset_id,
        "queue_sequence": queue_sequence,
        "seed": seed,
        "topic_id": topic_id,
    })


def _candidate_rows(
    db: sqlite3.Connection,
    *,
    assignment_cutoff_sequence: int,
    review_cutoff_sequence: int,
) -> list[sqlite3.Row]:
    return db.execute(
        """WITH snapshot_reviews AS (
               SELECT review.*,review_order.sequence AS review_sequence
               FROM topic_assignment_reviews AS review
               JOIN topic_assignment_review_order AS review_order
                 ON review_order.review_id=review.id
               WHERE review_order.sequence<=?
           ), latest_review AS (
               SELECT review.* FROM snapshot_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM snapshot_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT queue.sequence AS queue_sequence,assignment.id AS assignment_id,
                  topic_version.topic_id
           FROM topic_assignment_review_queue AS queue
           JOIN document_topic_assignments AS assignment ON assignment.id=queue.assignment_id
           JOIN topic_versions AS topic_version ON topic_version.id=assignment.topic_version_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE queue.sequence<=?
             AND COALESCE(review.decision,assignment.status)='candidate'
           ORDER BY queue.sequence,assignment.id""",
        (review_cutoff_sequence, assignment_cutoff_sequence),
    ).fetchall()


def _select_members(
    *, dataset_id: str, seed: str, per_topic_limit: int, rows: list[sqlite3.Row]
) -> list[dict]:
    by_topic: dict[str, list[dict]] = {}
    for row in rows:
        member = {
            "assignment_id": row["assignment_id"],
            "topic_id": row["topic_id"],
            "queue_sequence": row["queue_sequence"],
            "selection_sha256": _selection_sha256(
                dataset_id, seed, row["topic_id"], row["assignment_id"],
                row["queue_sequence"],
            ),
        }
        by_topic.setdefault(row["topic_id"], []).append(member)
    selected: list[dict] = []
    for topic_id in sorted(by_topic):
        ranked = sorted(
            by_topic[topic_id],
            key=lambda member: (
                member["selection_sha256"], member["queue_sequence"],
                member["assignment_id"],
            ),
        )
        selected.extend(ranked[:per_topic_limit])
    for ordinal, member in enumerate(selected):
        member["ordinal"] = ordinal
    return selected


def _manifest(
    *,
    dataset_id: str,
    seed: str,
    per_topic_limit: int,
    assignment_cutoff_sequence: int,
    review_cutoff_sequence: int,
    candidate_count: int,
    members: list[dict],
) -> dict:
    return {
        "assignment_cutoff_sequence": assignment_cutoff_sequence,
        "candidate_count": candidate_count,
        "dataset_id": dataset_id,
        "members": [
            {
                "assignment_id": member["assignment_id"],
                "ordinal": member["ordinal"],
                "queue_sequence": member["queue_sequence"],
                "selection_sha256": member["selection_sha256"],
                "topic_id": member["topic_id"],
            }
            for member in members
        ],
        "per_topic_limit": per_topic_limit,
        "review_cutoff_sequence": review_cutoff_sequence,
        "seed": seed,
    }


def _batch_from_row(row: sqlite3.Row) -> TopicReviewSampleBatch:
    return TopicReviewSampleBatch(
        batch_id=row["id"], dataset_id=row["dataset_id"], seed=row["seed"],
        per_topic_limit=row["per_topic_limit"],
        assignment_cutoff_sequence=row["assignment_cutoff_sequence"],
        review_cutoff_sequence=row["review_cutoff_sequence"],
        candidate_count=row["candidate_count"], topic_count=row["topic_count"],
        member_count=row["member_count"], manifest_sha256=row["manifest_sha256"],
        created_by=row["created_by"], created_at=row["created_at"],
    )


def get_sample_batch(db: sqlite3.Connection, batch_id: str) -> TopicReviewSampleBatch:
    row = db.execute(
        "SELECT * FROM topic_review_sampling_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if row is None:
        raise TopicReviewSamplingError("topic review sampling batch does not exist")
    return _batch_from_row(row)


def create_sample_batch(
    db: sqlite3.Connection,
    *,
    seed: str,
    per_topic_limit: int,
    created_by: str,
    now: str | None = None,
) -> TopicReviewSampleBatch:
    seed, created_by = seed.strip(), created_by.strip()
    if not 1 <= len(seed) <= 120:
        raise ValueError("seed must contain 1 to 120 characters")
    if not isinstance(per_topic_limit, int) or not 1 <= per_topic_limit <= 250:
        raise ValueError("per_topic_limit must be between 1 and 250")
    if not 1 <= len(created_by) <= 120:
        raise ValueError("created_by must contain 1 to 120 characters")
    dataset_id = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()[0]
    assignment_cutoff = db.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM topic_assignment_review_queue"
    ).fetchone()[0]
    review_cutoff = db.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM topic_assignment_review_order"
    ).fetchone()[0]
    candidates = _candidate_rows(
        db, assignment_cutoff_sequence=assignment_cutoff,
        review_cutoff_sequence=review_cutoff,
    )
    members = _select_members(
        dataset_id=dataset_id, seed=seed, per_topic_limit=per_topic_limit,
        rows=candidates,
    )
    manifest_sha256 = _canonical_sha256(_manifest(
        dataset_id=dataset_id, seed=seed, per_topic_limit=per_topic_limit,
        assignment_cutoff_sequence=assignment_cutoff,
        review_cutoff_sequence=review_cutoff, candidate_count=len(candidates),
        members=members,
    ))
    existing = db.execute(
        """SELECT * FROM topic_review_sampling_batches
           WHERE dataset_id=? AND seed=? AND per_topic_limit=?
             AND assignment_cutoff_sequence=? AND review_cutoff_sequence=?
             AND manifest_sha256=?""",
        (dataset_id, seed, per_topic_limit, assignment_cutoff, review_cutoff,
         manifest_sha256),
    ).fetchone()
    if existing is not None:
        return _batch_from_row(existing)
    batch_id = f"topic_sample_{uuid4().hex}"
    topic_count = len({member["topic_id"] for member in members})
    db.execute(
        """INSERT INTO topic_review_sampling_batches(
               id,dataset_id,seed,per_topic_limit,assignment_cutoff_sequence,
               review_cutoff_sequence,candidate_count,topic_count,member_count,
               manifest_sha256,created_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (batch_id, dataset_id, seed, per_topic_limit, assignment_cutoff,
         review_cutoff, len(candidates), topic_count, len(members),
         manifest_sha256, created_by, now or utc_now()),
    )
    db.executemany(
        """INSERT INTO topic_review_sampling_members(
               batch_id,ordinal,assignment_id,topic_id,queue_sequence,selection_sha256)
           VALUES(?,?,?,?,?,?)""",
        [
            (batch_id, member["ordinal"], member["assignment_id"], member["topic_id"],
             member["queue_sequence"], member["selection_sha256"])
            for member in members
        ],
    )
    return get_sample_batch(db, batch_id)


def sample_queue(
    db: sqlite3.Connection,
    batch_id: str,
    *,
    after_ordinal: int = -1,
    limit: int = 50,
    pending_only: bool = True,
) -> tuple[TopicReviewSampleItem, ...]:
    get_sample_batch(db, batch_id)
    if not isinstance(after_ordinal, int) or after_ordinal < -1:
        raise ValueError("after_ordinal must be at least -1")
    if not isinstance(limit, int) or not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")
    rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT member.ordinal,member.queue_sequence,assignment.id AS assignment_id,
                  document.id AS document_id,version.id AS document_version_id,
                  version.title_original,version.canonical_url,item.published_at,
                  topic.id AS topic_id,topic_version.id AS topic_version_id,
                  topic_version.slug AS topic_slug,topic_version.name AS topic_name,
                  assignment.method,assignment.method_version,assignment.status,
                  COALESCE(review.decision,assignment.status) AS effective_status,
                  review.id AS current_review_id,assignment.evidence_ids_json,
                  snapshot.evidence_text AS legacy_evidence,assignment.available_at
           FROM topic_review_sampling_members AS member
           JOIN document_topic_assignments AS assignment ON assignment.id=member.assignment_id
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
           WHERE member.batch_id=? AND member.ordinal>?
             AND (?=0 OR COALESCE(review.decision,assignment.status)='candidate')
           ORDER BY member.ordinal LIMIT ?""",
        (batch_id, after_ordinal, int(pending_only), limit),
    ).fetchall()
    return tuple(TopicReviewSampleItem(
        ordinal=row["ordinal"], queue_sequence=row["queue_sequence"],
        assignment_id=row["assignment_id"], document_id=row["document_id"],
        document_version_id=row["document_version_id"], title=row["title_original"],
        canonical_url=row["canonical_url"], published_at=row["published_at"],
        topic_id=row["topic_id"], topic_version_id=row["topic_version_id"],
        topic_slug=row["topic_slug"], topic_name=row["topic_name"],
        method=row["method"], method_version=row["method_version"],
        original_status=row["status"], effective_status=row["effective_status"],
        current_review_id=row["current_review_id"],
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        legacy_evidence=row["legacy_evidence"], available_at=row["available_at"],
    ) for row in rows)


def sample_report(db: sqlite3.Connection, batch_id: str) -> TopicReviewSampleReport:
    batch = get_sample_batch(db, batch_id)
    rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT topic.id AS topic_id,version.slug,version.name,
                  COUNT(*) AS selected,
                  SUM(CASE WHEN COALESCE(review.decision,assignment.status)='candidate' THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN COALESCE(review.decision,assignment.status)='accepted' THEN 1 ELSE 0 END) AS accepted,
                  SUM(CASE WHEN COALESCE(review.decision,assignment.status)='rejected' THEN 1 ELSE 0 END) AS rejected
           FROM topic_review_sampling_members AS member
           JOIN document_topic_assignments AS assignment ON assignment.id=member.assignment_id
           JOIN topic_catalog AS topic ON topic.id=member.topic_id
           JOIN topic_versions AS version ON version.id=topic.current_version_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE member.batch_id=?
           GROUP BY topic.id,version.slug,version.name
           ORDER BY version.group_key,version.name,topic.id""",
        (batch_id,),
    ).fetchall()
    topics = []
    for row in rows:
        decided = row["accepted"] + row["rejected"]
        topics.append(TopicReviewSampleTopicResult(
            topic_id=row["topic_id"], topic_slug=row["slug"], topic_name=row["name"],
            selected=row["selected"], pending=row["pending"], accepted=row["accepted"],
            rejected=row["rejected"], decided_bps=decided * 10_000 // row["selected"],
            acceptance_bps=(row["accepted"] * 10_000 // decided if decided else None),
        ))
    selected = sum(topic.selected for topic in topics)
    accepted = sum(topic.accepted for topic in topics)
    rejected = sum(topic.rejected for topic in topics)
    decided = accepted + rejected
    return TopicReviewSampleReport(
        batch=batch, pending=sum(topic.pending for topic in topics), accepted=accepted,
        rejected=rejected, decided_bps=(decided * 10_000 // selected if selected else 10_000),
        acceptance_bps=(accepted * 10_000 // decided if decided else None),
        topics=tuple(topics),
    )


def verify_sampling_batches(db: sqlite3.Connection) -> None:
    missing_review_order = db.execute(
        """SELECT COUNT(*) FROM topic_assignment_reviews AS review
           LEFT JOIN topic_assignment_review_order AS review_order
             ON review_order.review_id=review.id
           WHERE review_order.review_id IS NULL"""
    ).fetchone()[0]
    extra_review_order = db.execute(
        """SELECT COUNT(*) FROM topic_assignment_review_order AS review_order
           LEFT JOIN topic_assignment_reviews AS review ON review.id=review_order.review_id
           WHERE review.id IS NULL"""
    ).fetchone()[0]
    if missing_review_order or extra_review_order:
        raise TopicReviewSamplingError("topic assignment review order is incomplete")
    for row in db.execute("SELECT * FROM topic_review_sampling_batches ORDER BY id"):
        batch = _batch_from_row(row)
        candidates = _candidate_rows(
            db, assignment_cutoff_sequence=batch.assignment_cutoff_sequence,
            review_cutoff_sequence=batch.review_cutoff_sequence,
        )
        members = _select_members(
            dataset_id=batch.dataset_id, seed=batch.seed,
            per_topic_limit=batch.per_topic_limit, rows=candidates,
        )
        stored = [dict(item) for item in db.execute(
            """SELECT ordinal,assignment_id,topic_id,queue_sequence,selection_sha256
               FROM topic_review_sampling_members WHERE batch_id=? ORDER BY ordinal""",
            (batch.batch_id,),
        )]
        manifest_sha256 = _canonical_sha256(_manifest(
            dataset_id=batch.dataset_id, seed=batch.seed,
            per_topic_limit=batch.per_topic_limit,
            assignment_cutoff_sequence=batch.assignment_cutoff_sequence,
            review_cutoff_sequence=batch.review_cutoff_sequence,
            candidate_count=len(candidates), members=members,
        ))
        if (
            stored != members
            or batch.candidate_count != len(candidates)
            or batch.topic_count != len({member["topic_id"] for member in members})
            or batch.member_count != len(members)
            or batch.manifest_sha256 != manifest_sha256
        ):
            raise TopicReviewSamplingError(
                f"topic review sampling batch {batch.batch_id} is invalid"
            )
