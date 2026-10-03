from __future__ import annotations

"""Import one independent human impact-review batch into a new private dataset."""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import (
    EvaluationDatasetError,
    _load_cases,
    _parse_cases,
    _require_text,
    _review_time,
)
from .impact_evaluation import (
    _validate_impact_label,
    validate_impact_evaluation_dataset,
)

BATCH_VERSION = "human-impact-review-batch-v1"


@dataclass(frozen=True)
class ImpactReviewIntakeReport:
    source_dataset_version: str
    dataset_version: str
    reviewed_cases: int
    one_review_cases: int
    two_review_cases: int
    adjudicated_cases: int
    publishable_impact_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


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


def _require_verified_holdout(source: Path, manifest: dict) -> None:
    if manifest.get("split_policy") != "blind-holdout":
        return
    from .evaluation import _verified_holdout

    cases = _load_cases(source / "cases.jsonl")
    if (
        manifest.get("source_database_verified_at_admission") is not True
        or not _verified_holdout(manifest, cases, source)
    ):
        raise EvaluationDatasetError(
            "impact review requires verified human leakage review before labeling"
        )


def _copy_holdout_record(source: Path, staging: Path, manifest: dict) -> None:
    _require_verified_holdout(source, manifest)
    if manifest.get("split_policy") == "blind-holdout":
        shutil.copyfile(source / "holdout-review.json", staging / "holdout-review.json")


def import_impact_review_batch(
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> ImpactReviewIntakeReport:
    source, batch, output = Path(source), Path(batch), Path(output)
    resolved_output = output.resolve()
    if (
        output.exists()
        or resolved_output.is_relative_to(source.resolve())
        or resolved_output.is_relative_to(batch.resolve())
        or resolved_output in {source.resolve(), batch.resolve()}
    ):
        raise EvaluationDatasetError("impact review output must be a new immutable directory")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("impact review output parent directory must already exist")
    repository = Path(__file__).parents[1].resolve()
    private_root = repository / "evaluation" / "private"
    if resolved_output.is_relative_to(repository) and not resolved_output.is_relative_to(private_root):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if batch.resolve().is_relative_to(repository) and not batch.resolve().is_relative_to(private_root):
        raise EvaluationDatasetError("impact review batch must be private")

    source_report = validate_impact_evaluation_dataset(source)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version)
        or dataset_version == source_report.dataset_version
    ):
        raise EvaluationDatasetError("impact review output requires a new dataset_version")
    # Parse exactly the bytes that are hashed so recorded parent hashes describe the import.
    manifest_bytes = _read(source / "manifest.json")
    cases_bytes = _read(source / "cases.jsonl")
    source_manifest = _json_object(manifest_bytes, source / "manifest.json")
    # Validate leakage controls before parsing a batch so labels never enter an open holdout.
    _require_verified_holdout(source, source_manifest)

    batch_manifest_bytes = _read(batch / "manifest.json")
    batch_reviews_bytes = _read(batch / "reviews.jsonl")
    batch_manifest = _json_object(batch_manifest_bytes, batch / "manifest.json")
    if not isinstance(batch_manifest, dict) or batch_manifest.get("schema_version") != BATCH_VERSION:
        raise EvaluationDatasetError("unsupported human impact review batch schema")
    if set(batch_manifest) != {
        "schema_version", "source_dataset_version", "source_manifest_sha256",
        "source_cases_sha256", "reviewer_id", "source", "independent",
        "model_assistance",
    }:
        raise EvaluationDatasetError("impact review batch manifest has invalid fields")
    reviewer = _require_text(batch_manifest.get("reviewer_id"), "reviewer_id", "batch")
    if (
        batch_manifest.get("source_dataset_version") != source_report.dataset_version
        or batch_manifest.get("source_manifest_sha256")
        != _sha(manifest_bytes)
        or batch_manifest.get("source_cases_sha256") != _sha(cases_bytes)
    ):
        raise EvaluationDatasetError("impact review batch does not match frozen source dataset")
    if (
        batch_manifest.get("source") != "human"
        or batch_manifest.get("independent") is not True
        or batch_manifest.get("model_assistance") is not False
    ):
        raise EvaluationDatasetError("impact batch requires independent human, no-model attestation")

    cases = _parse_cases(_decode(cases_bytes, source / "cases.jsonl"))
    by_id = {case["case_id"]: case for case in cases}
    rows = _parse_cases(_decode(batch_reviews_bytes, batch / "reviews.jsonl"))
    if not rows:
        raise EvaluationDatasetError("impact review batch is empty")
    seen: set[str] = set()
    for row in rows:
        if set(row) != {"case_id", "content_sha256", "recorded_at", "labels"}:
            raise EvaluationDatasetError("impact review row has invalid fields")
        case_id = _require_text(row.get("case_id"), "case_id", "review")
        if case_id in seen or case_id not in by_id:
            raise EvaluationDatasetError(f"impact review case {case_id} is duplicate or unknown")
        seen.add(case_id)
        case = by_id[case_id]
        if case.get("text_storage") != "restricted_reference":
            raise EvaluationDatasetError(
                f"impact review case {case_id} requires a restricted real-data reference"
            )
        if row.get("content_sha256") != case["content_sha256"]:
            raise EvaluationDatasetError(f"impact review case {case_id} content hash differs")
        _review_time(row.get("recorded_at"), case_id)
        _validate_impact_label(case, row.get("labels"))
        annotation = case["annotation"]
        if annotation.get("state") not in {"unlabeled", "single_annotator"}:
            raise EvaluationDatasetError(
                f"impact review case {case_id} cannot receive another review"
            )
        if annotation.get("generated_by_model") or annotation.get("labels"):
            raise EvaluationDatasetError(f"impact review case {case_id} has provisional gold")
        reviews = annotation.setdefault("reviews", [])
        if (
            not isinstance(reviews, list)
            or len(reviews) >= 2
            or any(review.get("reviewer_id") == reviewer for review in reviews)
        ):
            raise EvaluationDatasetError(
                f"impact review case {case_id} requires a distinct reviewer"
            )
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

    output_manifest = {
        **source_manifest,
        "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": _sha(manifest_bytes),
        "parent_cases_sha256": _sha(cases_bytes),
        "impact_review_batch_schema_version": BATCH_VERSION,
        "impact_review_batch_manifest_sha256": _sha(batch_manifest_bytes),
        "impact_review_batch_cases_sha256": _sha(batch_reviews_bytes),
    }
    # Evidence review binds the previous cases file and cannot survive new labels.
    output_manifest.pop("impact_evidence_review", None)
    staging = Path(tempfile.mkdtemp(prefix="infohub-impact-review-intake-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(output_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "cases.jsonl").write_text(
            "".join(
                json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
                for case in cases
            ),
            encoding="utf-8",
        )
        _copy_holdout_record(source, staging, source_manifest)
        result = validate_impact_evaluation_dataset(staging)
        if result.publishable_impact_gold:
            raise EvaluationDatasetError(
                "impact review intake cannot publish gold before adjudication"
            )
        if output.exists():
            raise EvaluationDatasetError("impact review output appeared during import")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    review_counts = [len(case["annotation"].get("reviews", [])) for case in cases]
    return ImpactReviewIntakeReport(
        source_dataset_version=source_report.dataset_version,
        dataset_version=dataset_version,
        reviewed_cases=len(seen),
        one_review_cases=review_counts.count(1),
        two_review_cases=review_counts.count(2),
        adjudicated_cases=result.annotation_state_counts.get("adjudicated", 0),
        publishable_impact_gold=result.publishable_impact_gold,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a private human impact review batch")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    report = import_impact_review_batch(
        args.dataset, args.batch, args.output, dataset_version=args.dataset_version
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
