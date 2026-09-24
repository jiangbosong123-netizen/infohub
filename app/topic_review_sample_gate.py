from __future__ import annotations

"""Append-only quality decisions for immutable topic review samples."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now
from .topic_review_sampling import sample_report


class TopicReviewSampleGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class TopicReviewSampleGatePreview:
    batch_id: str
    metrics: dict
    metrics_sha256: str
    current_evaluation_id: str | None
    current_version: int | None
    current_decision: str | None
    current_evaluator_id: str | None
    current_reason: str | None
    current_evaluated_at: str | None
    current_thresholds: dict | None

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def sample_gate_metrics(
    db: sqlite3.Connection,
    batch_id: str,
    review_cutoff_sequence: int | None = None,
) -> dict:
    if review_cutoff_sequence is None:
        review_cutoff_sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM topic_assignment_review_order"
        ).fetchone()[0]
    report = sample_report(
        db, batch_id, review_cutoff_sequence=review_cutoff_sequence
    )
    return {
        "acceptance_bps": report.acceptance_bps,
        "accepted": report.accepted,
        "batch_id": report.batch.batch_id,
        "decided_bps": report.decided_bps,
        "manifest_sha256": report.batch.manifest_sha256,
        "member_count": report.batch.member_count,
        "pending": report.pending,
        "rejected": report.rejected,
        "review_cutoff_sequence": review_cutoff_sequence,
        "topics": [
            {
                "acceptance_bps": topic.acceptance_bps,
                "accepted": topic.accepted,
                "decided_bps": topic.decided_bps,
                "pending": topic.pending,
                "rejected": topic.rejected,
                "selected": topic.selected,
                "topic_id": topic.topic_id,
            }
            for topic in sorted(report.topics, key=lambda item: item.topic_id)
        ],
    }


def sample_gate_preview(
    db: sqlite3.Connection, batch_id: str
) -> TopicReviewSampleGatePreview:
    metrics = sample_gate_metrics(db, batch_id)
    evaluation = db.execute(
        """SELECT * FROM topic_review_sample_evaluations
           WHERE batch_id=? ORDER BY version DESC LIMIT 1""",
        (batch_id,),
    ).fetchone()
    thresholds = None
    if evaluation is not None:
        thresholds = {
            "minimum_acceptance_bps": evaluation["minimum_acceptance_bps"],
            "minimum_decided_bps": evaluation["minimum_decided_bps"],
            "minimum_topic_acceptance_bps": evaluation["minimum_topic_acceptance_bps"],
            "minimum_topic_decided_bps": evaluation["minimum_topic_decided_bps"],
        }
    return TopicReviewSampleGatePreview(
        batch_id=batch_id, metrics=metrics, metrics_sha256=_digest(metrics),
        current_evaluation_id=evaluation["id"] if evaluation else None,
        current_version=evaluation["version"] if evaluation else None,
        current_decision=evaluation["decision"] if evaluation else None,
        current_evaluator_id=evaluation["evaluator_id"] if evaluation else None,
        current_reason=evaluation["reason"] if evaluation else None,
        current_evaluated_at=evaluation["evaluated_at"] if evaluation else None,
        current_thresholds=thresholds,
    )


def _thresholds(
    minimum_decided_bps: int,
    minimum_topic_decided_bps: int,
    minimum_acceptance_bps: int,
    minimum_topic_acceptance_bps: int,
) -> dict:
    values = {
        "minimum_decided_bps": minimum_decided_bps,
        "minimum_topic_decided_bps": minimum_topic_decided_bps,
        "minimum_acceptance_bps": minimum_acceptance_bps,
        "minimum_topic_acceptance_bps": minimum_topic_acceptance_bps,
    }
    for name, value in values.items():
        if not isinstance(value, int) or not 1 <= value <= 10_000:
            raise ValueError(f"{name} must be between 1 and 10000")
    return values


def _approval_failures(metrics: dict, thresholds: dict) -> list[str]:
    failures = []
    if metrics["member_count"] == 0:
        failures.append("sample has no members")
    if metrics["decided_bps"] < thresholds["minimum_decided_bps"]:
        failures.append("overall review completion is below threshold")
    if (
        metrics["acceptance_bps"] is None
        or metrics["acceptance_bps"] < thresholds["minimum_acceptance_bps"]
    ):
        failures.append("overall acceptance is below threshold")
    incomplete = [
        topic["topic_id"] for topic in metrics["topics"]
        if topic["decided_bps"] < thresholds["minimum_topic_decided_bps"]
    ]
    if incomplete:
        failures.append(f"{len(incomplete)} topic(s) are below the review completion threshold")
    inaccurate = [
        topic["topic_id"] for topic in metrics["topics"]
        if topic["acceptance_bps"] is None
        or topic["acceptance_bps"] < thresholds["minimum_topic_acceptance_bps"]
    ]
    if inaccurate:
        failures.append(f"{len(inaccurate)} topic(s) are below the acceptance threshold")
    return failures


def record_sample_evaluation(
    db: sqlite3.Connection,
    *,
    batch_id: str,
    decision: str,
    expected_previous_evaluation_id: str | None,
    minimum_decided_bps: int,
    minimum_topic_decided_bps: int,
    minimum_acceptance_bps: int,
    minimum_topic_acceptance_bps: int,
    evaluator_id: str,
    reason: str,
    now: str | None = None,
) -> TopicReviewSampleGatePreview:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    evaluator_id, reason = evaluator_id.strip(), reason.strip()
    if not 1 <= len(evaluator_id) <= 120:
        raise ValueError("evaluator_id must contain 1 to 120 characters")
    if not 1 <= len(reason) <= 1000:
        raise ValueError("reason must contain 1 to 1000 characters")
    thresholds = _thresholds(
        minimum_decided_bps, minimum_topic_decided_bps,
        minimum_acceptance_bps, minimum_topic_acceptance_bps,
    )
    preview = sample_gate_preview(db, batch_id)
    if preview.current_evaluation_id != expected_previous_evaluation_id:
        raise TopicReviewSampleGateError(
            "topic review sample evaluation changed; refresh the preview before deciding"
        )
    if decision == "approved":
        failures = _approval_failures(preview.metrics, thresholds)
        if failures:
            raise TopicReviewSampleGateError("; ".join(failures))
    version = (preview.current_version or 0) + 1
    db.execute(
        """INSERT INTO topic_review_sample_evaluations(
               id,batch_id,version,previous_evaluation_id,decision,
               review_cutoff_sequence,minimum_decided_bps,minimum_topic_decided_bps,
               minimum_acceptance_bps,minimum_topic_acceptance_bps,
               metrics_json,metrics_sha256,evaluator_id,reason,evaluated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"topic_sample_evaluation_{uuid4().hex}", batch_id, version,
            preview.current_evaluation_id, decision,
            preview.metrics["review_cutoff_sequence"], minimum_decided_bps,
            minimum_topic_decided_bps, minimum_acceptance_bps,
            minimum_topic_acceptance_bps, _canonical(preview.metrics),
            preview.metrics_sha256, evaluator_id, reason, now or utc_now(),
        ),
    )
    return sample_gate_preview(db, batch_id)


def approved_sample_evaluation(db: sqlite3.Connection, batch_id: str) -> dict:
    preview = sample_gate_preview(db, batch_id)
    if preview.current_decision != "approved":
        raise TopicReviewSampleGateError("topic review sample has no current approval")
    evaluation = db.execute(
        "SELECT * FROM topic_review_sample_evaluations WHERE id=?",
        (preview.current_evaluation_id,),
    ).fetchone()
    if evaluation["metrics_sha256"] != preview.metrics_sha256:
        raise TopicReviewSampleGateError(
            "topic review sample metrics changed after approval"
        )
    return {
        "evaluation_id": evaluation["id"],
        "batch_id": batch_id,
        "version": evaluation["version"],
        "evaluated_at": evaluation["evaluated_at"],
        "metrics_sha256": evaluation["metrics_sha256"],
    }


def verify_sample_evaluations(db: sqlite3.Connection) -> None:
    for evaluation in db.execute(
        "SELECT * FROM topic_review_sample_evaluations ORDER BY batch_id,version"
    ):
        try:
            metrics = json.loads(evaluation["metrics_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TopicReviewSampleGateError(
                f"topic review sample evaluation {evaluation['id']} has invalid metrics"
            ) from exc
        if _digest(metrics) != evaluation["metrics_sha256"]:
            raise TopicReviewSampleGateError(
                f"topic review sample evaluation {evaluation['id']} has invalid metrics digest"
            )
        if metrics.get("batch_id") != evaluation["batch_id"]:
            raise TopicReviewSampleGateError(
                f"topic review sample evaluation {evaluation['id']} has the wrong batch"
            )
        reconstructed = sample_gate_metrics(
            db, evaluation["batch_id"], evaluation["review_cutoff_sequence"]
        )
        if metrics != reconstructed:
            raise TopicReviewSampleGateError(
                f"topic review sample evaluation {evaluation['id']} metrics cannot be reconstructed"
            )
        if evaluation["decision"] == "approved":
            thresholds = {
                "minimum_decided_bps": evaluation["minimum_decided_bps"],
                "minimum_topic_decided_bps": evaluation["minimum_topic_decided_bps"],
                "minimum_acceptance_bps": evaluation["minimum_acceptance_bps"],
                "minimum_topic_acceptance_bps": evaluation["minimum_topic_acceptance_bps"],
            }
            failures = _approval_failures(metrics, thresholds)
            if failures:
                raise TopicReviewSampleGateError(
                    f"topic review sample evaluation {evaluation['id']} is an invalid approval"
                )
