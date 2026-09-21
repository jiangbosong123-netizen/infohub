from __future__ import annotations

"""Freeze a human blind-holdout inspection as a new private dataset version."""

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
    HOLDOUT_CHECKS, EvaluationDatasetError, _load_cases, _load_json,
    _require_text, _review_time, _verified_holdout, validate_evaluation_dataset,
)

REVIEW_VERSION = "holdout-review-v1"


@dataclass(frozen=True)
class HoldoutReviewReport:
    source_dataset_version: str
    dataset_version: str
    cases: int
    holdout_status: str
    publishable_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def freeze_holdout_review(source: Path | str, review_file: Path | str,
                          output: Path | str, *, dataset_version: str) -> HoldoutReviewReport:
    source, review_file, output = Path(source), Path(review_file), Path(output)
    if output.exists() or output.resolve().is_relative_to(source.resolve()):
        raise EvaluationDatasetError("holdout output must be a new immutable directory")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("holdout output parent directory must exist")
    repository = Path(__file__).parents[1].resolve()
    if output.resolve().is_relative_to(repository) and not output.resolve().is_relative_to(
            repository / "evaluation" / "private"):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if review_file.resolve().is_relative_to(repository) and not review_file.resolve().is_relative_to(
            repository / "evaluation" / "private"):
        raise EvaluationDatasetError("review record must be kept under evaluation/private or outside the repository")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version):
        raise EvaluationDatasetError("dataset_version must be a simple identifier")

    source_report = validate_evaluation_dataset(source)
    if dataset_version == source_report.dataset_version:
        raise EvaluationDatasetError("holdout review requires a new dataset_version")
    source_manifest_path = source / "manifest.json"
    source_cases_path = source / "cases.jsonl"
    manifest_bytes, cases_bytes = source_manifest_path.read_bytes(), source_cases_path.read_bytes()
    manifest = _load_json(source_manifest_path)
    cases = _load_cases(source_cases_path)
    if any(case["annotation"]["state"] != "unlabeled" for case in cases):
        raise EvaluationDatasetError("blind holdout must be reviewed before labeling")
    plan = manifest.get("holdout_review")
    if (manifest.get("split_policy") != "blind-holdout"
            or manifest.get("source_database_verified_at_admission") is not True
            or not isinstance(plan, dict) or plan.get("status") != "pending"):
        raise EvaluationDatasetError("requires a source-verified pending blind holdout")
    record_bytes = review_file.read_bytes()
    record = _load_json(review_file)
    if not isinstance(record, dict) or record.get("schema_version") != REVIEW_VERSION:
        raise EvaluationDatasetError("unsupported holdout review record")
    if record.get("source") != "human" or record.get("model_assistance") is not False:
        raise EvaluationDatasetError("holdout review requires human no-model attestation")
    reviewer = _require_text(record.get("reviewer_id"), "reviewer_id", "holdout")
    _review_time(record.get("recorded_at"), "holdout")
    if (record.get("source_dataset_version") != source_report.dataset_version
            or record.get("source_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or record.get("source_cases_sha256") != hashlib.sha256(cases_bytes).hexdigest()
            or record.get("heldout_after") != plan.get("heldout_after")
            or record.get("heldout_source_refs") != plan.get("heldout_source_refs")):
        raise EvaluationDatasetError("holdout review differs from frozen dataset or plan")
    checks = record.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in HOLDOUT_CHECKS):
        raise EvaluationDatasetError("holdout review is missing required human checks")
    _require_text(record.get("inspection_notes"), "inspection_notes", "holdout")

    reviewed = {
        "status": "verified", "protocol_version": REVIEW_VERSION,
        "reviewer_id": reviewer, "recorded_at": record["recorded_at"],
        "heldout_after": plan["heldout_after"],
        "heldout_source_refs": plan["heldout_source_refs"], "checks": checks,
        "source_manifest_sha256": record["source_manifest_sha256"],
        "source_cases_sha256": record["source_cases_sha256"],
        "review_record_sha256": hashlib.sha256(record_bytes).hexdigest(),
    }
    output_manifest = {
        **manifest, "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "parent_cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "holdout_review": reviewed,
    }
    if not _verified_holdout(output_manifest, cases):
        raise EvaluationDatasetError("holdout evidence does not match blind-test assignments")
    staging = Path(tempfile.mkdtemp(prefix="infohub-holdout-review-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(output_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        (staging / "cases.jsonl").write_bytes(cases_bytes)
        (staging / "holdout-review.json").write_bytes(record_bytes)
        result = validate_evaluation_dataset(staging)
        if result.publishable_gold:
            raise EvaluationDatasetError("holdout review cannot produce gold without adjudication")
        if output.exists():
            raise EvaluationDatasetError("holdout output appeared during review")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return HoldoutReviewReport(source_report.dataset_version, dataset_version,
                               len(cases), "verified", result.publishable_gold)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a private human blind-holdout review")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    result = freeze_holdout_review(args.dataset, args.review, args.output,
                                   dataset_version=args.dataset_version)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
