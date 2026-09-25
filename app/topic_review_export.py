from __future__ import annotations

"""Deterministic, point-in-time exports of immutable topic review samples."""

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from .timeutil import utc_now
from .topic_review_sampling import get_sample_batch, sample_queue, sample_report


FORMAT = "infohub-topic-review-export-v1"


class TopicReviewExportError(RuntimeError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _review_snapshot(
    db: sqlite3.Connection,
    assignment_ids: tuple[str, ...],
    review_cutoff_sequence: int,
) -> dict[str, dict]:
    if not assignment_ids:
        return {}
    placeholders = ",".join("?" for _ in assignment_ids)
    rows = db.execute(
        f"""WITH snapshot_reviews AS (
                SELECT review.*,review_order.sequence AS review_sequence
                FROM topic_assignment_reviews AS review
                JOIN topic_assignment_review_order AS review_order
                  ON review_order.review_id=review.id
                WHERE review_order.sequence<=?
                  AND review.assignment_id IN ({placeholders})
            )
            SELECT review.* FROM snapshot_reviews AS review
            WHERE NOT EXISTS(
                SELECT 1 FROM snapshot_reviews AS later
                WHERE later.assignment_id=review.assignment_id
                  AND later.version>review.version
            ) ORDER BY review.assignment_id""",
        (review_cutoff_sequence, *assignment_ids),
    ).fetchall()
    return {
        row["assignment_id"]: {
            "decision": row["decision"],
            "evidence_ids": json.loads(row["evidence_ids_json"]),
            "id": row["id"],
            "previous_review_id": row["previous_review_id"],
            "reason": row["reason"],
            "review_sequence": row["review_sequence"],
            "reviewed_at": row["reviewed_at"],
            "reviewer_id": row["reviewer_id"],
            "version": row["version"],
        }
        for row in rows
    }


def build_topic_review_export(
    db: sqlite3.Connection,
    batch_id: str,
    *,
    review_cutoff_sequence: int | None = None,
) -> dict:
    batch = get_sample_batch(db, batch_id)
    current_cutoff = db.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM topic_assignment_review_order"
    ).fetchone()[0]
    if review_cutoff_sequence is None:
        review_cutoff_sequence = current_cutoff
    if not isinstance(review_cutoff_sequence, int) or review_cutoff_sequence < 0:
        raise ValueError("review_cutoff_sequence must be a non-negative integer")
    if review_cutoff_sequence > current_cutoff:
        raise TopicReviewExportError("review cutoff is beyond the current review high-water mark")

    items = []
    after = -1
    while True:
        page = sample_queue(
            db, batch_id, after_ordinal=after, limit=250, pending_only=False,
            review_cutoff_sequence=review_cutoff_sequence,
        )
        if not page:
            break
        reviews = _review_snapshot(
            db, tuple(item.assignment_id for item in page), review_cutoff_sequence
        )
        for item in page:
            value = item.to_dict()
            value["evidence_ids"] = list(value["evidence_ids"])
            value["review"] = reviews.get(item.assignment_id)
            items.append(value)
        after = page[-1].ordinal

    report = sample_report(
        db, batch_id, review_cutoff_sequence=review_cutoff_sequence
    ).to_dict()
    payload = {
        "batch": batch.to_dict(),
        "members": items,
        "review_cutoff_sequence": review_cutoff_sequence,
        "sample_report": report,
    }
    return {
        "format": FORMAT,
        "payload": payload,
        "payload_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
    }


def verify_topic_review_export(path: str | Path) -> dict:
    target = Path(path).expanduser().resolve(strict=True)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise TopicReviewExportError("unsupported topic review export format")
        payload = document["payload"]
        digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        if not isinstance(document.get("payload_sha256"), str) or not hmac.compare_digest(
            digest, document["payload_sha256"]
        ):
            raise TopicReviewExportError("topic review export digest does not match")
        members = payload["members"]
        if len(members) != payload["batch"]["member_count"]:
            raise TopicReviewExportError("topic review export member count does not match")
        batch = payload["batch"]
        sample_manifest = {
            "assignment_cutoff_sequence": batch["assignment_cutoff_sequence"],
            "candidate_count": batch["candidate_count"],
            "dataset_id": batch["dataset_id"],
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
            "per_topic_limit": batch["per_topic_limit"],
            "review_cutoff_sequence": batch["review_cutoff_sequence"],
            "seed": batch["seed"],
        }
        manifest_digest = hashlib.sha256(_canonical_bytes(sample_manifest)).hexdigest()
        if not hmac.compare_digest(manifest_digest, batch["manifest_sha256"]):
            raise TopicReviewExportError("topic review sample manifest does not match")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TopicReviewExportError("invalid topic review export") from exc
    return {
        "status": "ok",
        "path": str(target),
        "batch_id": payload["batch"]["batch_id"],
        "member_count": len(members),
        "review_cutoff_sequence": payload["review_cutoff_sequence"],
        "payload_sha256": digest,
        "manifest_sha256": manifest_digest,
    }


def export_topic_review_sample(
    db: sqlite3.Connection,
    batch_id: str,
    destination: str | Path,
    *,
    review_cutoff_sequence: int | None = None,
    exported_at: str | None = None,
) -> dict:
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"topic review export already exists: {target}")
    document = build_topic_review_export(
        db, batch_id, review_cutoff_sequence=review_cutoff_sequence
    )
    document["exported_at"] = exported_at or utc_now()
    stage = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verify_topic_review_export(stage)
        if target.exists():
            raise FileExistsError(f"topic review export already exists: {target}")
        os.replace(stage, target)
        return verify_topic_review_export(target)
    finally:
        if stage.exists():
            stage.unlink()
