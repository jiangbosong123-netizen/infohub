from __future__ import annotations

"""Shared hash-bound intake of one human annotation batch.

Each task (relevance, tone, impact) keeps its own batch schema, label validator and report
type; this module owns the immutability, privacy, hash-binding and atomic-write rules so
they cannot drift between tasks. Two intake kinds share those rules: provisional
multi-reviewer opinions (`run_review_intake`) and final single-owner labels
(`run_owner_label_intake`, D23 single-owner-v1).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evaluation import (
    OWNER_PROTOCOL_VERSION,
    EvaluationDatasetError,
    _load_cases,
    _parse_cases,
    _require_text,
    _review_time,
    _verified_holdout,
    owner_protocol,
)

BATCH_MANIFEST_FIELDS = frozenset({
    "schema_version", "source_dataset_version", "source_manifest_sha256",
    "source_cases_sha256", "reviewer_id", "source", "independent", "model_assistance",
})
OWNER_BATCH_VERSION = "owner-label-batch-v1"
OWNER_BATCH_MANIFEST_FIELDS = frozenset({
    "schema_version", "task", "label_definition", "source_dataset_version",
    "source_manifest_sha256", "source_cases_sha256", "owner_id", "source", "blind",
    "model_assistance",
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
    task_id: str = ""

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


@dataclass(frozen=True)
class OwnerLabelIntakeReport:
    task: str
    source_dataset_version: str
    dataset_version: str
    owner_id: str
    labeled_cases: int
    owner_labeled_cases: int
    unlabeled_cases: int
    publishable_gold: bool

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "source_dataset_version": self.source_dataset_version,
            "dataset_version": self.dataset_version,
            "owner_id": self.owner_id,
            "labeled_cases": self.labeled_cases,
            "owner_labeled_cases": self.owner_labeled_cases,
            "unlabeled_cases": self.unlabeled_cases,
            "publishable_gold": self.publishable_gold,
            "annotation_tier": "owner",
            "claim_scope": "experimental",
        }


@dataclass
class _Source:
    report: Any
    manifest: dict
    manifest_bytes: bytes
    cases_bytes: bytes
    cases: list[dict]


@dataclass
class _Batch:
    manifest: dict
    manifest_bytes: bytes
    reviews_bytes: bytes


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


def _check_paths(label: str, source: Path, batch: Path, output: Path) -> None:
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


def _load_source(task: ReviewIntakeTask, label: str, source: Path, dataset_version: str) -> _Source:
    report = task.validate_dataset(source)
    if (
        DATASET_VERSION_RE.fullmatch(dataset_version) is None
        or dataset_version == report.dataset_version
    ):
        raise EvaluationDatasetError(f"{label} output requires a new dataset_version")
    # Parse exactly the bytes that are hashed so recorded parent hashes describe the import.
    manifest_bytes = _read(source / "manifest.json")
    cases_bytes = _read(source / "cases.jsonl")
    manifest = _json_object(manifest_bytes, source / "manifest.json")
    # Check before reading the batch so labels never enter an unverified holdout.
    require_verified_holdout(source, manifest, label)
    cases = _parse_cases(_decode(cases_bytes, source / "cases.jsonl"))
    return _Source(report, manifest, manifest_bytes, cases_bytes, cases)


def _load_batch(
    label: str, batch: Path, schema_version: str, fields: frozenset[str], source: _Source
) -> _Batch:
    manifest_bytes = _read(batch / "manifest.json")
    reviews_bytes = _read(batch / "reviews.jsonl")
    manifest = _json_object(manifest_bytes, batch / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != schema_version:
        raise EvaluationDatasetError(f"unsupported human {label} batch schema")
    if set(manifest) != fields:
        raise EvaluationDatasetError(f"{label} batch manifest has invalid fields")
    if (
        manifest.get("source_dataset_version") != source.report.dataset_version
        or manifest.get("source_manifest_sha256") != _sha(source.manifest_bytes)
        or manifest.get("source_cases_sha256") != _sha(source.cases_bytes)
    ):
        raise EvaluationDatasetError(f"{label} batch does not match frozen source dataset")
    return _Batch(manifest, manifest_bytes, reviews_bytes)


def _rows(
    task: ReviewIntakeTask, label: str, batch_path: Path, batch: _Batch, source: _Source
) -> Iterator[tuple[dict, dict]]:
    """Yield (row, case) after the checks every human label row must pass."""
    by_id = {case["case_id"]: case for case in source.cases}
    rows = _parse_cases(_decode(batch.reviews_bytes, batch_path / "reviews.jsonl"))
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
        yield row, case


def _publish(
    task: ReviewIntakeTask,
    label: str,
    source_path: Path,
    output: Path,
    source: _Source,
    batch: _Batch,
    *,
    dataset_version: str,
    prefix: str,
    schema_version: str,
    extra_manifest: dict | None = None,
) -> Any:
    output_manifest = {
        **source.manifest,
        **(extra_manifest or {}),
        "dataset_version": dataset_version,
        "parent_dataset_version": source.report.dataset_version,
        "parent_manifest_sha256": _sha(source.manifest_bytes),
        "parent_cases_sha256": _sha(source.cases_bytes),
        f"{prefix}_schema_version": schema_version,
        f"{prefix}_manifest_sha256": _sha(batch.manifest_bytes),
        f"{prefix}_cases_sha256": _sha(batch.reviews_bytes),
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
            "".join(
                json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
                for case in source.cases
            ),
            encoding="utf-8",
        )
        copy_holdout_record(source_path, staging, source.manifest, label)
        result = task.validate_dataset(staging)
        if task.is_publishable(result):
            raise EvaluationDatasetError(f"{label} intake cannot publish gold before adjudication")
        if output.exists():
            raise EvaluationDatasetError(f"{label} output appeared during import")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return result


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
    _check_paths(label, source, batch, output)
    loaded = _load_source(task, label, source, dataset_version)
    bound = _load_batch(label, batch, task.batch_version, BATCH_MANIFEST_FIELDS, loaded)
    reviewer = _require_text(bound.manifest.get("reviewer_id"), "reviewer_id", "batch")
    if (
        bound.manifest.get("source") != "human"
        or bound.manifest.get("independent") is not True
        or bound.manifest.get("model_assistance") is not False
    ):
        raise EvaluationDatasetError(
            f"{task.name} batch requires independent human, no-model attestation".strip()
        )

    reviewed = 0
    for row, case in _rows(task, label, batch, bound, loaded):
        case_id = case["case_id"]
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
        reviewed += 1

    result = _publish(
        task, label, source, output, loaded, bound,
        dataset_version=dataset_version,
        prefix=task.manifest_prefix,
        schema_version=task.batch_version,
    )
    review_counts = [len(case["annotation"].get("reviews", [])) for case in loaded.cases]
    return ReviewIntakeCounts(
        source_dataset_version=loaded.report.dataset_version,
        dataset_version=dataset_version,
        reviewed_cases=reviewed,
        one_review_cases=review_counts.count(1),
        two_review_cases=review_counts.count(2),
        adjudicated_cases=result.annotation_state_counts.get("adjudicated", 0),
        publishable=task.is_publishable(result),
    )


def run_owner_label_intake(
    task: ReviewIntakeTask,
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> OwnerLabelIntakeReport:
    """Freeze blind single-owner labels as final owner-tier labels (experimental, never gold)."""
    label = "owner label"
    source, batch, output = Path(source), Path(batch), Path(output)
    _check_paths(label, source, batch, output)
    loaded = _load_source(task, label, source, dataset_version)
    bound = _load_batch(label, batch, OWNER_BATCH_VERSION, OWNER_BATCH_MANIFEST_FIELDS, loaded)
    if bound.manifest.get("task") != task.task_id:
        raise EvaluationDatasetError(f"owner label batch is not for task {task.task_id}")
    owner = _require_text(bound.manifest.get("owner_id"), "owner_id", "batch")
    if (
        bound.manifest.get("source") != "human"
        or bound.manifest.get("blind") is not True
        or bound.manifest.get("model_assistance") is not False
    ):
        raise EvaluationDatasetError("owner label batch requires a blind, no-model human attestation")
    definition = _require_text(bound.manifest.get("label_definition"), "label_definition", "batch")
    declared = owner_protocol(loaded.manifest)
    if declared is not None and declared["owner_id"] != owner:
        raise EvaluationDatasetError("owner label batch owner differs from the dataset owner")
    if declared is not None and declared["label_definition"] != definition:
        # Labels made under different definitions are not comparable within one dataset.
        raise EvaluationDatasetError("owner label batch definition differs from the dataset definition")

    labeled = 0
    for row, case in _rows(task, label, batch, bound, loaded):
        annotation = case["annotation"]
        if (
            annotation.get("state") != "unlabeled"
            or annotation.get("labels")
            or annotation.get("generated_by_model")
            or any(key in annotation for key in ("reviews", "adjudication", "owner_label", "labeler"))
        ):
            raise EvaluationDatasetError(
                f"owner label case {case['case_id']} already has annotations"
            )
        annotation.update({
            "state": "owner_labeled",
            "generated_by_model": False,
            "labels": row["labels"],
            "owner_label": {
                "owner_id": owner,
                "source": "human",
                "blind": True,
                "model_assistance": False,
                "content_sha256": case["content_sha256"],
                "recorded_at": row["recorded_at"],
                "labels": row["labels"],
            },
        })
        labeled += 1

    result = _publish(
        task, label, source, output, loaded, bound,
        dataset_version=dataset_version,
        prefix="owner_label_batch",
        schema_version=OWNER_BATCH_VERSION,
        extra_manifest={
            "annotation_protocol": {
                "version": OWNER_PROTOCOL_VERSION,
                "owner_id": owner,
                "label_definition": definition,
            },
        },
    )
    states = result.annotation_state_counts
    return OwnerLabelIntakeReport(
        task=task.task_id,
        source_dataset_version=loaded.report.dataset_version,
        dataset_version=dataset_version,
        owner_id=owner,
        labeled_cases=labeled,
        owner_labeled_cases=states.get("owner_labeled", 0),
        unlabeled_cases=states.get("unlabeled", 0),
        publishable_gold=task.is_publishable(result),
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
