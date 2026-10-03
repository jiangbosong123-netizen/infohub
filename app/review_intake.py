from __future__ import annotations

"""Shared hash-bound intake of one independent human review batch.

Each task (relevance, tone, impact) keeps its own batch schema, label validator and report
type; this module owns the immutability, privacy, hash-binding and atomic-write rules so
they cannot drift between tasks.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evaluation import (
    EvaluationDatasetError,
    _load_cases,
    _parse_cases,
    _require_text,
    _review_time,
    _verified_holdout,
)

BATCH_MANIFEST_FIELDS = frozenset({
    "schema_version", "source_dataset_version", "source_manifest_sha256",
    "source_cases_sha256", "reviewer_id", "source", "independent", "model_assistance",
})
REVIEW_ROW_FIELDS = frozenset({"case_id", "content_sha256", "recorded_at", "labels"})
DATASET_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class ReviewIntakeTask:
    name: str
    batch_version: str
    manifest_prefix: str
    validate_dataset: Callable[[Path], Any]
    is_publishable: Callable[[Any], bool]
    validate_labels: Callable[[dict, object], object]
    evidence_review_key: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} review".strip()


@dataclass(frozen=True)
class ReviewIntakeCounts:
    source_dataset_version: str
    dataset_version: str
    reviewed_cases: int
    one_review_cases: int
    two_review_cases: int
    adjudicated_cases: int
    publishable: bool


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EvaluationDatasetError(f"cannot read {path}: {exc}") from exc


def _decode(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationDatasetError(f"cannot decode {path}: {exc}") from exc


def _json_object(data: bytes, path: Path) -> object:
    try:
        return json.loads(_decode(data, path))
    except json.JSONDecodeError as exc:
        raise EvaluationDatasetError(f"cannot read JSON {path}: {exc}") from exc


def require_verified_holdout(source: Path, manifest: dict, label: str) -> None:
    if manifest.get("split_policy") != "blind-holdout":
        return
    cases = _load_cases(source / "cases.jsonl")
    if (
        manifest.get("source_database_verified_at_admission") is not True
        or not _verified_holdout(manifest, cases, source)
    ):
        raise EvaluationDatasetError(
            f"{label} requires verified human leakage review before labeling"
        )


def copy_holdout_record(source: Path, staging: Path, manifest: dict, label: str) -> None:
    require_verified_holdout(source, manifest, label)
    if manifest.get("split_policy") == "blind-holdout":
        shutil.copyfile(source / "holdout-review.json", staging / "holdout-review.json")


def run_review_intake(
    task: ReviewIntakeTask,
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> ReviewIntakeCounts:
    label = task.label
    source, batch, output = Path(source), Path(batch), Path(output)
    resolved_output = output.resolve()
    if (
        resolved_output.is_relative_to(source.resolve())
        or resolved_output.is_relative_to(batch.resolve())
    ):
        raise EvaluationDatasetError(f"{label} output must be a new immutable directory")
    if output.exists():
        raise EvaluationDatasetError(
            f"{label} output already exists; it must be a new immutable directory"
        )
    if not output.parent.is_dir():
        raise EvaluationDatasetError(f"{label} output parent directory must already exist")
    repository = Path(__file__).parents[1].resolve()
    private_root = repository / "evaluation" / "private"
    if resolved_output.is_relative_to(repository) and not resolved_output.is_relative_to(private_root):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if batch.resolve().is_relative_to(repository) and not batch.resolve().is_relative_to(private_root):
        raise EvaluationDatasetError(f"{label} batch must be private")

    source_report = task.validate_dataset(source)
    if (
        DATASET_VERSION_RE.fullmatch(dataset_version) is None
        or dataset_version == source_report.dataset_version
    ):
        raise EvaluationDatasetError(f"{label} output requires a new dataset_version")
    # Parse exactly the bytes that are hashed so recorded parent hashes describe the import.
    manifest_bytes = _read(source / "manifest.json")
    cases_bytes = _read(source / "cases.jsonl")
    source_manifest = _json_object(manifest_bytes, source / "manifest.json")
    # Check before reading the batch so labels never enter an unverified holdout.
    require_verified_holdout(source, source_manifest, label)

    batch_manifest_bytes = _read(batch / "manifest.json")
    batch_reviews_bytes = _read(batch / "reviews.jsonl")
    batch_manifest = _json_object(batch_manifest_bytes, batch / "manifest.json")
    if (
        not isinstance(batch_manifest, dict)
        or batch_manifest.get("schema_version") != task.batch_version
    ):
        raise EvaluationDatasetError(f"unsupported human {label} batch schema")
    if set(batch_manifest) != BATCH_MANIFEST_FIELDS:
        raise EvaluationDatasetError(f"{label} batch manifest has invalid fields")
    reviewer = _require_text(batch_manifest.get("reviewer_id"), "reviewer_id", "batch")
    if (
        batch_manifest.get("source_dataset_version") != source_report.dataset_version
        or batch_manifest.get("source_manifest_sha256") != _sha(manifest_bytes)
        or batch_manifest.get("source_cases_sha256") != _sha(cases_bytes)
    ):
        raise EvaluationDatasetError(f"{label} batch does not match frozen source dataset")
    if (
        batch_manifest.get("source") != "human"
        or batch_manifest.get("independent") is not True
        or batch_manifest.get("model_assistance") is not False
    ):
        raise EvaluationDatasetError(
            f"{task.name} batch requires independent human, no-model attestation".strip()
        )

    cases = _parse_cases(_decode(cases_bytes, source / "cases.jsonl"))
    by_id = {case["case_id"]: case for case in cases}
    rows = _parse_cases(_decode(batch_reviews_bytes, batch / "reviews.jsonl"))
    if not rows:
        raise EvaluationDatasetError(f"{label} batch is empty")
    seen: set[str] = set()
    for row in rows:
        if set(row) != REVIEW_ROW_FIELDS:
            raise EvaluationDatasetError(f"{label} row has invalid fields")
        case_id = _require_text(row.get("case_id"), "case_id", "review")
        if case_id in seen or case_id not in by_id:
            raise EvaluationDatasetError(f"{label} case {case_id} is duplicate or unknown")
        seen.add(case_id)
        case = by_id[case_id]
        if case.get("text_storage") != "restricted_reference":
            raise EvaluationDatasetError(
                f"{label} case {case_id} requires a restricted real-data reference"
            )
        if row.get("content_sha256") != case["content_sha256"]:
            raise EvaluationDatasetError(f"{label} case {case_id} content hash differs")
        _review_time(row.get("recorded_at"), case_id)
        task.validate_labels(case, row.get("labels"))
        annotation = case["annotation"]
        if annotation.get("state") not in {"unlabeled", "single_annotator"}:
            raise EvaluationDatasetError(f"{label} case {case_id} cannot receive another review")
        if annotation.get("generated_by_model") or annotation.get("labels"):
            raise EvaluationDatasetError(f"{label} case {case_id} has provisional gold")
        reviews = annotation.setdefault("reviews", [])
        if (
            not isinstance(reviews, list)
            or len(reviews) >= 2
            or any(review.get("reviewer_id") == reviewer for review in reviews)
        ):
            raise EvaluationDatasetError(f"{label} case {case_id} requires a distinct reviewer")
        reviews.append({
            "reviewer_id": reviewer,
            "source": "human",
            "independent": True,
            "content_sha256": case["content_sha256"],
            "recorded_at": row["recorded_at"],
            "labels": row["labels"],
        })
        # Two opinions remain provisional until a distinct adjudicator decides.
        annotation["state"] = "single_annotator"

    prefix = task.manifest_prefix
    output_manifest = {
        **source_manifest,
        "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": _sha(manifest_bytes),
        "parent_cases_sha256": _sha(cases_bytes),
        f"{prefix}_schema_version": task.batch_version,
        f"{prefix}_manifest_sha256": _sha(batch_manifest_bytes),
        f"{prefix}_cases_sha256": _sha(batch_reviews_bytes),
    }
    if task.evidence_review_key:
        # An evidence-review signature binds the previous cases file and cannot survive new labels.
        output_manifest.pop(task.evidence_review_key, None)
    staging = Path(tempfile.mkdtemp(prefix="infohub-review-intake-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(output_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
            encoding="utf-8",
        )
        copy_holdout_record(source, staging, source_manifest, label)
        result = task.validate_dataset(staging)
        if task.is_publishable(result):
            raise EvaluationDatasetError(f"{label} intake cannot publish gold before adjudication")
        if output.exists():
            raise EvaluationDatasetError(f"{label} output appeared during import")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    review_counts = [len(case["annotation"].get("reviews", [])) for case in cases]
    return ReviewIntakeCounts(
        source_dataset_version=source_report.dataset_version,
        dataset_version=dataset_version,
        reviewed_cases=len(seen),
        one_review_cases=review_counts.count(1),
        two_review_cases=review_counts.count(2),
        adjudicated_cases=result.annotation_state_counts.get("adjudicated", 0),
        publishable=task.is_publishable(result),
    )


def review_intake_cli(description: str, importer: Callable[..., Any]) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    report = importer(
        args.dataset, args.batch, args.output, dataset_version=args.dataset_version
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0
