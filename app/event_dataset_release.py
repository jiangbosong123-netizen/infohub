from __future__ import annotations

"""Dataset-level, fail-closed release gate for publicly admitted events."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .event_admission import EventAdmissionError, admitted_event
from .event_review_sample_gate import sample_gate_preview
from .timeutil import utc_now


POLICY_VERSION = "event-release-v1"
MINIMUM_DECIDED_BPS = 10_000
MINIMUM_STRATUM_DECIDED_BPS = 10_000
MINIMUM_ACCEPTANCE_BPS = 9_000
MINIMUM_STRATUM_ACCEPTANCE_BPS = 9_000


def _required_floors() -> dict[str, int]:
    return {
        "minimum_decided_bps": MINIMUM_DECIDED_BPS,
        "minimum_stratum_decided_bps": MINIMUM_STRATUM_DECIDED_BPS,
        "minimum_acceptance_bps": MINIMUM_ACCEPTANCE_BPS,
        "minimum_stratum_acceptance_bps": MINIMUM_STRATUM_ACCEPTANCE_BPS,
    }


class EventDatasetReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class EventDatasetReleasePreview:
    dataset_id: str
    dataset_epoch: str
    sample_evaluation_id: str
    event_manifest: tuple[dict, ...]
    event_manifest_sha256: str
    metrics: dict
    metrics_sha256: str
    current_review_id: str | None
    current_version: int | None
    current_decision: str | None
    current_reviewer_id: str | None
    current_reason: str | None
    current_reviewed_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sample_status(db: sqlite3.Connection, evaluation_id: str, dataset_id: str) -> dict:
    row = db.execute(
        """SELECT evaluation.*,batch.dataset_id AS batch_dataset_id,
                  batch.manifest_sha256 AS sample_manifest_sha256,
                  batch.decision_cutoff_sequence AS decision_cutoff_sequence
           FROM event_review_sample_evaluations AS evaluation
           JOIN event_review_sampling_batches AS batch ON batch.id=evaluation.batch_id
           WHERE evaluation.id=?""",
        (evaluation_id,),
    ).fetchone()
    if row is None:
        raise EventDatasetReleaseError("event sample evaluation does not exist")
    preview = sample_gate_preview(db, row["batch_id"])
    thresholds = {
        "minimum_decided_bps": row["minimum_decided_bps"],
        "minimum_stratum_decided_bps": row["minimum_stratum_decided_bps"],
        "minimum_acceptance_bps": row["minimum_acceptance_bps"],
        "minimum_stratum_acceptance_bps": row["minimum_stratum_acceptance_bps"],
    }
    floors = _required_floors()
    current_decision_cutoff = db.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM event_match_review_queue"
    ).fetchone()[0]
    return {
        "batch_id": row["batch_id"],
        "dataset_matches": row["batch_dataset_id"] == dataset_id,
        "decision": row["decision"],
        "is_current_evaluation": preview.current_evaluation_id == evaluation_id,
        "metrics_current": preview.metrics_sha256 == row["metrics_sha256"],
        "metrics_sha256": row["metrics_sha256"],
        "sample_manifest_sha256": row["sample_manifest_sha256"],
        "decision_cutoff_sequence": row["decision_cutoff_sequence"],
        "decision_population_current": (
            row["decision_cutoff_sequence"] == current_decision_cutoff
        ),
        "thresholds": thresholds,
        "required_floors": floors,
        "thresholds_meet_policy": all(thresholds[key] >= value for key, value in floors.items()),
    }


def _event_manifest(db: sqlite3.Connection, dataset_id: str) -> tuple[list[dict], list[str]]:
    rows = db.execute(
        """SELECT event.id
           FROM events AS event
           JOIN event_admission_reviews AS review
             ON review.event_version_id=event.current_version_id
           WHERE event.dataset_id=? AND review.decision<>'rejected'
             AND NOT EXISTS(
                 SELECT 1 FROM event_admission_reviews AS later
                 WHERE later.event_version_id=review.event_version_id
                   AND later.version>review.version)
           ORDER BY event.id""",
        (dataset_id,),
    ).fetchall()
    manifest, invalid = [], []
    for row in rows:
        try:
            admitted = admitted_event(db, row["id"])
        except EventAdmissionError:
            invalid.append(row["id"])
            continue
        manifest.append({
            "admission_review_id": admitted.review_id,
            "admission_review_version": admitted.review_version,
            "event_id": admitted.event_id,
            "event_version_id": admitted.event_version_id,
            "metrics_sha256": admitted.metrics_sha256,
            "public_state": admitted.public_state,
        })
    return manifest, invalid


def release_preview(
    db: sqlite3.Connection, sample_evaluation_id: str,
) -> EventDatasetReleasePreview:
    state = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    dataset_id, epoch = state["dataset_id"], state["current_epoch"]
    sample = _sample_status(db, sample_evaluation_id, dataset_id)
    manifest, invalid = _event_manifest(db, dataset_id)
    metrics = {
        "admitted_event_count": len(manifest),
        "candidate_event_count": db.execute(
            "SELECT COUNT(*) FROM events WHERE dataset_id=? AND status='candidate'",
            (dataset_id,),
        ).fetchone()[0],
        "total_event_count": db.execute(
            "SELECT COUNT(*) FROM events WHERE dataset_id=?", (dataset_id,),
        ).fetchone()[0],
        "dataset_epoch": epoch,
        "dataset_id": dataset_id,
        "invalid_public_admission_count": len(invalid),
        "invalid_public_admission_event_ids": invalid,
        "policy_version": POLICY_VERSION,
        "sample": sample,
        "sample_evaluation_id": sample_evaluation_id,
    }
    latest = db.execute(
        """SELECT * FROM event_dataset_release_reviews
           WHERE dataset_id=? AND dataset_epoch=? ORDER BY version DESC LIMIT 1""",
        (dataset_id, epoch),
    ).fetchone()
    return EventDatasetReleasePreview(
        dataset_id=dataset_id, dataset_epoch=epoch,
        sample_evaluation_id=sample_evaluation_id,
        event_manifest=tuple(manifest), event_manifest_sha256=_digest(manifest),
        metrics=metrics, metrics_sha256=_digest(metrics),
        current_review_id=latest["id"] if latest else None,
        current_version=latest["version"] if latest else None,
        current_decision=latest["decision"] if latest else None,
        current_reviewer_id=latest["reviewer_id"] if latest else None,
        current_reason=latest["reason"] if latest else None,
        current_reviewed_at=latest["reviewed_at"] if latest else None,
    )


def _approval_failures(preview: EventDatasetReleasePreview) -> list[str]:
    sample = preview.metrics["sample"]
    failures = []
    if not sample["dataset_matches"]:
        failures.append("sample belongs to another dataset")
    if sample["decision"] != "approved" or not sample["is_current_evaluation"]:
        failures.append("sample evaluation is not the current approval")
    if not sample["metrics_current"]:
        failures.append("sample evaluation metrics are stale")
    if not sample["decision_population_current"]:
        failures.append("sample does not cover the current match decision population")
    if not sample["thresholds_meet_policy"]:
        failures.append("sample evaluation thresholds are below release policy")
    if preview.metrics["invalid_public_admission_count"]:
        failures.append("one or more public event admissions are stale or invalid")
    if not preview.metrics["admitted_event_count"]:
        failures.append("release requires at least one currently admitted event")
    return failures


def record_release_review(
    db: sqlite3.Connection, *, sample_evaluation_id: str, decision: str,
    expected_previous_review_id: str | None, reviewer_id: str, reason: str,
    now: str | None = None,
) -> EventDatasetReleasePreview:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    reviewer_id, reason = reviewer_id.strip(), reason.strip()
    if not 1 <= len(reviewer_id) <= 120:
        raise ValueError("reviewer_id must contain 1 to 120 characters")
    if not 1 <= len(reason) <= 1000:
        raise ValueError("reason must contain 1 to 1000 characters")
    preview = release_preview(db, sample_evaluation_id)
    if preview.current_review_id != expected_previous_review_id:
        raise EventDatasetReleaseError("event dataset release changed; refresh before deciding")
    if decision == "approved":
        failures = _approval_failures(preview)
        if failures:
            raise EventDatasetReleaseError("; ".join(failures))
    version = (preview.current_version or 0) + 1
    db.execute(
        """INSERT INTO event_dataset_release_reviews(
               id,dataset_id,dataset_epoch,version,previous_review_id,decision,
               sample_evaluation_id,sample_metrics_sha256,event_manifest_json,
               event_manifest_sha256,metrics_json,metrics_sha256,reviewer_id,reason,
               reviewed_at,policy_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"event_release_{uuid4().hex}", preview.dataset_id, preview.dataset_epoch,
            version, preview.current_review_id, decision, sample_evaluation_id,
            preview.metrics["sample"]["metrics_sha256"],
            _canonical(list(preview.event_manifest)), preview.event_manifest_sha256,
            _canonical(preview.metrics), preview.metrics_sha256, reviewer_id, reason,
            now or utc_now(), POLICY_VERSION,
        ),
    )
    return release_preview(db, sample_evaluation_id)


def approved_event_release(db: sqlite3.Connection) -> dict:
    state = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    row = db.execute(
        """SELECT * FROM event_dataset_release_reviews
           WHERE dataset_id=? AND dataset_epoch=? ORDER BY version DESC LIMIT 1""",
        (state["dataset_id"], state["current_epoch"]),
    ).fetchone()
    if row is None or row["decision"] != "approved":
        raise EventDatasetReleaseError("event dataset has no current release approval")
    if row["policy_version"] != POLICY_VERSION:
        raise EventDatasetReleaseError("event dataset release policy is unsupported")
    preview = release_preview(db, row["sample_evaluation_id"])
    if (
        row["metrics_sha256"] != preview.metrics_sha256
        or row["event_manifest_sha256"] != preview.event_manifest_sha256
        or json.loads(row["event_manifest_json"]) != list(preview.event_manifest)
    ):
        raise EventDatasetReleaseError("event dataset release is stale")
    failures = _approval_failures(preview)
    if failures:
        raise EventDatasetReleaseError("; ".join(failures))
    return {
        "release_review_id": row["id"], "version": row["version"],
        "dataset_id": row["dataset_id"], "dataset_epoch": row["dataset_epoch"],
        "sample_evaluation_id": row["sample_evaluation_id"],
        "event_manifest": list(preview.event_manifest),
        "event_manifest_sha256": row["event_manifest_sha256"],
        "reviewed_at": row["reviewed_at"], "policy_version": row["policy_version"],
    }


def verify_release_reviews(db: sqlite3.Connection) -> None:
    for row in db.execute(
        "SELECT * FROM event_dataset_release_reviews ORDER BY dataset_id,dataset_epoch,version"
    ):
        try:
            manifest = json.loads(row["event_manifest_json"])
            metrics = json.loads(row["metrics_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise EventDatasetReleaseError("event dataset release JSON is invalid") from exc
        evaluation = db.execute(
            "SELECT * FROM event_review_sample_evaluations WHERE id=?",
            (row["sample_evaluation_id"],),
        ).fetchone()
        batch = None if evaluation is None else db.execute(
            "SELECT * FROM event_review_sampling_batches WHERE id=?",
            (evaluation["batch_id"],),
        ).fetchone()
        sample = metrics.get("sample", {}) if isinstance(metrics, dict) else {}
        if not isinstance(sample, dict):
            raise EventDatasetReleaseError("event dataset release integrity check failed")
        evaluation_thresholds = None if evaluation is None else {
            "minimum_decided_bps": evaluation["minimum_decided_bps"],
            "minimum_stratum_decided_bps": evaluation["minimum_stratum_decided_bps"],
            "minimum_acceptance_bps": evaluation["minimum_acceptance_bps"],
            "minimum_stratum_acceptance_bps": evaluation[
                "minimum_stratum_acceptance_bps"
            ],
        }
        if (
            row["policy_version"] != POLICY_VERSION
            or not isinstance(manifest, list)
            or _digest(manifest) != row["event_manifest_sha256"]
            or not isinstance(metrics, dict)
            or _digest(metrics) != row["metrics_sha256"]
            or metrics.get("dataset_id") != row["dataset_id"]
            or metrics.get("dataset_epoch") != row["dataset_epoch"]
            or metrics.get("sample_evaluation_id") != row["sample_evaluation_id"]
            or evaluation is None
            or evaluation["metrics_sha256"] != row["sample_metrics_sha256"]
            or batch is None
            or batch["dataset_id"] != row["dataset_id"]
            or sample.get("batch_id") != evaluation["batch_id"]
            or sample.get("decision") != evaluation["decision"]
            or sample.get("metrics_sha256") != evaluation["metrics_sha256"]
            or sample.get("thresholds") != evaluation_thresholds
            or sample.get("sample_manifest_sha256") != batch["manifest_sha256"]
            or sample.get("decision_cutoff_sequence")
            != batch["decision_cutoff_sequence"]
        ):
            raise EventDatasetReleaseError("event dataset release integrity check failed")
        if any(
            not isinstance(entry, dict)
            or set(entry) != {
                "admission_review_id", "admission_review_version", "event_id",
                "event_version_id", "metrics_sha256", "public_state",
            }
            or any(not isinstance(entry[key], str) or not entry[key] for key in (
                "admission_review_id", "event_id", "event_version_id",
                "metrics_sha256", "public_state",
            ))
            or not isinstance(entry["admission_review_version"], int)
            or entry["admission_review_version"] < 1
            for entry in manifest
        ):
            raise EventDatasetReleaseError("event dataset release manifest is invalid")
        event_ids = [entry["event_id"] for entry in manifest]
        if (
            metrics.get("admitted_event_count") != len(manifest)
            or event_ids != sorted(set(event_ids))
        ):
            raise EventDatasetReleaseError("event dataset release manifest is invalid")
        for entry in manifest:
            admission = db.execute(
                """SELECT review.*,version.event_id,event.dataset_id
                   FROM event_admission_reviews AS review
                   JOIN event_versions AS version ON version.id=review.event_version_id
                   JOIN events AS event ON event.id=version.event_id
                   WHERE review.id=?""",
                (entry["admission_review_id"],),
            ).fetchone()
            if (
                admission is None
                or admission["dataset_id"] != row["dataset_id"]
                or admission["event_id"] != entry["event_id"]
                or admission["event_version_id"] != entry["event_version_id"]
                or admission["version"] != entry["admission_review_version"]
                or admission["decision"] != entry["public_state"]
                or admission["decision"] == "rejected"
                or admission["metrics_sha256"] != entry["metrics_sha256"]
            ):
                raise EventDatasetReleaseError(
                    "event dataset release admission reference is invalid"
                )
        if row["decision"] == "approved":
            thresholds = metrics.get("sample", {}).get("thresholds", {})
            required = metrics.get("sample", {}).get("required_floors", {})
            if (
                not manifest or metrics.get("invalid_public_admission_count")
                or sample.get("decision") != "approved"
                or required != _required_floors()
                or not sample.get("dataset_matches")
                or not sample.get("is_current_evaluation")
                or not sample.get("metrics_current")
                or not sample.get("decision_population_current")
                or any(
                    thresholds.get(key, 0) < value
                    for key, value in _required_floors().items()
                )
            ):
                raise EventDatasetReleaseError("event dataset release approval is invalid")
