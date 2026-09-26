from __future__ import annotations

"""Immutable, reproducible, stratified samples for event match review."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


class EventReviewSamplingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EventReviewSampleBatch:
    batch_id: str
    dataset_id: str
    seed: str
    per_stratum_limit: int
    decision_cutoff_sequence: int
    review_cutoff_sequence: int
    candidate_count: int
    stratum_count: int
    member_count: int
    manifest_sha256: str
    created_by: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EventReviewSampleItem:
    ordinal: int
    queue_sequence: int
    selection_sha256: str
    stratum_key: str
    decision_id: str
    machine_decision: str
    matcher_version: str
    score: float | None
    reason: str
    input_version_ids: tuple[str, ...]
    candidate_event_version_ids: tuple[str, ...]
    effective_status: str
    current_review_id: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EventReviewSampleStratumResult:
    stratum_key: str
    selected: int
    pending: int
    accepted: int
    rejected: int
    decided_bps: int
    acceptance_bps: int | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EventReviewSampleReport:
    batch: EventReviewSampleBatch
    pending: int
    accepted: int
    rejected: int
    decided_bps: int
    acceptance_bps: int | None
    strata: tuple[EventReviewSampleStratumResult, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["batch"] = self.batch.to_dict()
        value["strata"] = [item.to_dict() for item in self.strata]
        return value


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _score_band(score: float | None) -> str:
    if score is None:
        return "missing"
    if score < 0.50:
        return "lt-0.50"
    if score < 0.78:
        return "0.50-0.779999"
    if score < 0.90:
        return "0.78-0.899999"
    return "ge-0.90"


def _stratum(row: sqlite3.Row) -> str:
    return _canonical({
        "decision": row["decision"],
        "matcher_version": row["matcher_version"],
        "score_band": _score_band(row["score"]),
    })


def _candidate_rows(
    db: sqlite3.Connection, *, decision_cutoff: int, review_cutoff: int,
) -> list[sqlite3.Row]:
    return db.execute(
        """WITH snapshot_reviews AS (
               SELECT review.* FROM event_match_reviews AS review
               JOIN event_match_review_order AS review_order
                 ON review_order.review_id=review.id
               WHERE review_order.sequence<=?
           ), latest_review AS (
               SELECT review.* FROM snapshot_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM snapshot_reviews AS later
                   WHERE later.decision_id=review.decision_id
                     AND later.version>review.version
               )
           )
           SELECT queue.sequence AS queue_sequence,decision.*
           FROM event_match_review_queue AS queue
           JOIN match_decisions AS decision ON decision.id=queue.decision_id
           LEFT JOIN latest_review AS review ON review.decision_id=decision.id
           WHERE queue.sequence<=?
             AND COALESCE(review.decision,decision.review_status)='pending'
           ORDER BY queue.sequence,decision.id""",
        (review_cutoff, decision_cutoff),
    ).fetchall()


def _select(
    *, dataset_id: str, seed: str, per_stratum_limit: int,
    rows: list[sqlite3.Row],
) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        stratum_key = _stratum(row)
        member = {
            "decision_id": row["id"], "stratum_key": stratum_key,
            "queue_sequence": row["queue_sequence"],
        }
        member["selection_sha256"] = _sha({
            "dataset_id": dataset_id, "seed": seed, **member,
        })
        groups.setdefault(stratum_key, []).append(member)
    selected: list[dict] = []
    for stratum_key in sorted(groups):
        ranked = sorted(groups[stratum_key], key=lambda item: (
            item["selection_sha256"], item["queue_sequence"], item["decision_id"],
        ))
        selected.extend(ranked[:per_stratum_limit])
    for ordinal, member in enumerate(selected):
        member["ordinal"] = ordinal
    return selected


def _manifest(batch: dict, members: list[dict]) -> dict:
    return {
        "dataset_id": batch["dataset_id"], "seed": batch["seed"],
        "per_stratum_limit": batch["per_stratum_limit"],
        "decision_cutoff_sequence": batch["decision_cutoff_sequence"],
        "review_cutoff_sequence": batch["review_cutoff_sequence"],
        "candidate_count": batch["candidate_count"], "members": members,
    }


def _batch(row: sqlite3.Row) -> EventReviewSampleBatch:
    return EventReviewSampleBatch(
        batch_id=row["id"], dataset_id=row["dataset_id"], seed=row["seed"],
        per_stratum_limit=row["per_stratum_limit"],
        decision_cutoff_sequence=row["decision_cutoff_sequence"],
        review_cutoff_sequence=row["review_cutoff_sequence"],
        candidate_count=row["candidate_count"], stratum_count=row["stratum_count"],
        member_count=row["member_count"], manifest_sha256=row["manifest_sha256"],
        created_by=row["created_by"], created_at=row["created_at"],
    )


def get_sample_batch(db: sqlite3.Connection, batch_id: str) -> EventReviewSampleBatch:
    row = db.execute(
        "SELECT * FROM event_review_sampling_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if row is None:
        raise EventReviewSamplingError("event review sampling batch does not exist")
    return _batch(row)


def create_sample_batch(
    db: sqlite3.Connection, *, seed: str, per_stratum_limit: int,
    created_by: str, now: str | None = None,
) -> EventReviewSampleBatch:
    seed, created_by = seed.strip(), created_by.strip()
    if not 1 <= len(seed) <= 120:
        raise ValueError("seed must contain 1 to 120 characters")
    if not isinstance(per_stratum_limit, int) or not 1 <= per_stratum_limit <= 250:
        raise ValueError("per_stratum_limit must be between 1 and 250")
    if not 1 <= len(created_by) <= 120:
        raise ValueError("created_by must contain 1 to 120 characters")
    dataset_id = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()[0]
    decision_cutoff = db.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM event_match_review_queue"
    ).fetchone()[0]
    review_cutoff = db.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM event_match_review_order"
    ).fetchone()[0]
    candidates = _candidate_rows(
        db, decision_cutoff=decision_cutoff, review_cutoff=review_cutoff,
    )
    members = _select(
        dataset_id=dataset_id, seed=seed, per_stratum_limit=per_stratum_limit,
        rows=candidates,
    )
    values = {
        "dataset_id": dataset_id, "seed": seed,
        "per_stratum_limit": per_stratum_limit,
        "decision_cutoff_sequence": decision_cutoff,
        "review_cutoff_sequence": review_cutoff,
        "candidate_count": len(candidates),
    }
    digest = _sha(_manifest(values, members))
    existing = db.execute(
        """SELECT * FROM event_review_sampling_batches
           WHERE dataset_id=? AND seed=? AND per_stratum_limit=?
             AND decision_cutoff_sequence=? AND review_cutoff_sequence=?
             AND manifest_sha256=?""",
        (dataset_id, seed, per_stratum_limit, decision_cutoff, review_cutoff, digest),
    ).fetchone()
    if existing:
        return _batch(existing)
    batch_id = f"event_sample_{uuid4().hex}"
    stratum_count = len({item["stratum_key"] for item in members})
    db.execute(
        """INSERT INTO event_review_sampling_batches(
               id,dataset_id,seed,per_stratum_limit,decision_cutoff_sequence,
               review_cutoff_sequence,candidate_count,stratum_count,member_count,
               manifest_sha256,created_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (batch_id, dataset_id, seed, per_stratum_limit, decision_cutoff,
         review_cutoff, len(candidates), stratum_count, len(members), digest,
         created_by, now or utc_now()),
    )
    db.executemany(
        """INSERT INTO event_review_sampling_members(
               batch_id,ordinal,decision_id,stratum_key,queue_sequence,selection_sha256)
           VALUES(?,?,?,?,?,?)""",
        [(batch_id, item["ordinal"], item["decision_id"], item["stratum_key"],
          item["queue_sequence"], item["selection_sha256"]) for item in members],
    )
    return get_sample_batch(db, batch_id)


def _cutoff(db: sqlite3.Connection, value: int | None) -> int:
    if value is None:
        value = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM event_match_review_order"
        ).fetchone()[0]
    if not isinstance(value, int) or value < 0:
        raise ValueError("review_cutoff_sequence must be a non-negative integer")
    return value


def sample_queue(
    db: sqlite3.Connection, batch_id: str, *, after_ordinal: int = -1,
    limit: int = 50, pending_only: bool = True,
    review_cutoff_sequence: int | None = None,
) -> tuple[EventReviewSampleItem, ...]:
    get_sample_batch(db, batch_id)
    if not isinstance(after_ordinal, int) or after_ordinal < -1:
        raise ValueError("after_ordinal must be at least -1")
    if not isinstance(limit, int) or not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")
    cutoff = _cutoff(db, review_cutoff_sequence)
    rows = db.execute(
        """WITH snapshot_reviews AS (
               SELECT review.* FROM event_match_reviews AS review
               JOIN event_match_review_order AS review_order
                 ON review_order.review_id=review.id
               WHERE review_order.sequence<=?
           ), latest_review AS (
               SELECT review.* FROM snapshot_reviews AS review
               WHERE NOT EXISTS(SELECT 1 FROM snapshot_reviews AS later
                                WHERE later.decision_id=review.decision_id
                                  AND later.version>review.version)
           )
           SELECT member.*,decision.decision,decision.matcher_version,decision.score,
                  decision.reason,decision.input_versions_json,
                  decision.candidate_event_versions_json,
                  COALESCE(review.decision,decision.review_status) AS effective_status,
                  review.id AS current_review_id
           FROM event_review_sampling_members AS member
           JOIN match_decisions AS decision ON decision.id=member.decision_id
           LEFT JOIN latest_review AS review ON review.decision_id=decision.id
           WHERE member.batch_id=? AND member.ordinal>?
             AND (?=0 OR COALESCE(review.decision,decision.review_status)='pending')
           ORDER BY member.ordinal LIMIT ?""",
        (cutoff, batch_id, after_ordinal, int(pending_only), limit),
    ).fetchall()
    return tuple(EventReviewSampleItem(
        ordinal=row["ordinal"], queue_sequence=row["queue_sequence"],
        selection_sha256=row["selection_sha256"], stratum_key=row["stratum_key"],
        decision_id=row["decision_id"], machine_decision=row["decision"],
        matcher_version=row["matcher_version"], score=row["score"], reason=row["reason"],
        input_version_ids=tuple(json.loads(row["input_versions_json"])),
        candidate_event_version_ids=tuple(json.loads(row["candidate_event_versions_json"])),
        effective_status=row["effective_status"], current_review_id=row["current_review_id"],
    ) for row in rows)


def sample_report(
    db: sqlite3.Connection, batch_id: str, *,
    review_cutoff_sequence: int | None = None,
) -> EventReviewSampleReport:
    batch = get_sample_batch(db, batch_id)
    cutoff = _cutoff(db, review_cutoff_sequence)
    rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM event_match_reviews AS review
               JOIN event_match_review_order AS review_order
                 ON review_order.review_id=review.id
               WHERE review_order.sequence<=? AND NOT EXISTS(
                   SELECT 1 FROM event_match_reviews AS later
                   JOIN event_match_review_order AS later_order
                     ON later_order.review_id=later.id
                   WHERE later.decision_id=review.decision_id
                     AND later.version>review.version AND later_order.sequence<=?)
           )
           SELECT member.stratum_key,COUNT(*) AS selected,
                  SUM(CASE WHEN COALESCE(review.decision,decision.review_status)='pending' THEN 1 ELSE 0 END) pending,
                  SUM(CASE WHEN COALESCE(review.decision,decision.review_status)='accepted' THEN 1 ELSE 0 END) accepted,
                  SUM(CASE WHEN COALESCE(review.decision,decision.review_status)='rejected' THEN 1 ELSE 0 END) rejected
           FROM event_review_sampling_members AS member
           JOIN match_decisions AS decision ON decision.id=member.decision_id
           LEFT JOIN latest_review AS review ON review.decision_id=decision.id
           WHERE member.batch_id=? GROUP BY member.stratum_key ORDER BY member.stratum_key""",
        (cutoff, cutoff, batch_id),
    ).fetchall()
    strata = []
    for row in rows:
        decided = row["accepted"] + row["rejected"]
        strata.append(EventReviewSampleStratumResult(
            stratum_key=row["stratum_key"], selected=row["selected"],
            pending=row["pending"], accepted=row["accepted"], rejected=row["rejected"],
            decided_bps=decided * 10_000 // row["selected"],
            acceptance_bps=row["accepted"] * 10_000 // decided if decided else None,
        ))
    selected = sum(row.selected for row in strata)
    accepted, rejected = sum(row.accepted for row in strata), sum(row.rejected for row in strata)
    decided = accepted + rejected
    return EventReviewSampleReport(
        batch=batch, pending=sum(row.pending for row in strata), accepted=accepted,
        rejected=rejected, decided_bps=decided * 10_000 // selected if selected else 10_000,
        acceptance_bps=accepted * 10_000 // decided if decided else None,
        strata=tuple(strata),
    )


def verify_sampling_batches(db: sqlite3.Connection) -> None:
    missing = db.execute(
        """SELECT COUNT(*) FROM event_match_reviews AS review
           LEFT JOIN event_match_review_order AS ordering ON ordering.review_id=review.id
           WHERE ordering.review_id IS NULL"""
    ).fetchone()[0]
    extra = db.execute(
        """SELECT COUNT(*) FROM event_match_review_order AS ordering
           LEFT JOIN event_match_reviews AS review ON review.id=ordering.review_id
           WHERE review.id IS NULL"""
    ).fetchone()[0]
    if missing or extra:
        raise EventReviewSamplingError("event match review order is incomplete")
    for row in db.execute("SELECT * FROM event_review_sampling_batches ORDER BY id"):
        batch = _batch(row)
        candidates = _candidate_rows(
            db, decision_cutoff=batch.decision_cutoff_sequence,
            review_cutoff=batch.review_cutoff_sequence,
        )
        members = _select(
            dataset_id=batch.dataset_id, seed=batch.seed,
            per_stratum_limit=batch.per_stratum_limit, rows=candidates,
        )
        stored = [dict(item) for item in db.execute(
            """SELECT decision_id,stratum_key,queue_sequence,selection_sha256,ordinal
               FROM event_review_sampling_members WHERE batch_id=? ORDER BY ordinal""",
            (batch.batch_id,),
        )]
        values = {
            "dataset_id": batch.dataset_id, "seed": batch.seed,
            "per_stratum_limit": batch.per_stratum_limit,
            "decision_cutoff_sequence": batch.decision_cutoff_sequence,
            "review_cutoff_sequence": batch.review_cutoff_sequence,
            "candidate_count": len(candidates),
        }
        if (
            stored != members or batch.candidate_count != len(candidates)
            or batch.stratum_count != len({item["stratum_key"] for item in members})
            or batch.member_count != len(members)
            or batch.manifest_sha256 != _sha(_manifest(values, members))
        ):
            raise EventReviewSamplingError(
                f"event review sampling batch {batch.batch_id} is invalid"
            )
