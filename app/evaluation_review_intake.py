from __future__ import annotations

"""Import one independent human relevance-review batch into a new private dataset."""

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
    EvaluationDatasetError, _load_cases, _load_json, _require_text,
    _review_time, validate_evaluation_dataset,
)

BATCH_VERSION = "human-relevance-review-batch-v1"
RELEVANCE_LABELS = {"relevant", "not_relevant", "unknown"}


@dataclass(frozen=True)
class ReviewIntakeReport:
    source_dataset_version: str
    dataset_version: str
    reviewed_cases: int
    one_review_cases: int
    two_review_cases: int
    adjudicated_cases: int
    publishable_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_review_batch(source: Path | str, batch: Path | str, output: Path | str,
                        *, dataset_version: str) -> ReviewIntakeReport:
    source, batch, output = Path(source), Path(batch), Path(output)
    if (output.resolve().is_relative_to(source.resolve())
            or output.resolve().is_relative_to(batch.resolve())
            or source.resolve() == output.resolve() or batch.resolve() == output.resolve()):
        raise EvaluationDatasetError("review output must be a new dataset directory")
    if output.exists():
        raise EvaluationDatasetError("review output already exists; dataset versions are immutable")
    repository = Path(__file__).parents[1].resolve()
    if output.resolve().is_relative_to(repository) and not output.resolve().is_relative_to(
            repository / "evaluation" / "private"):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("review output parent directory must already exist")

    source_report = validate_evaluation_dataset(source)
    source_manifest = _load_json(source / "manifest.json")
    if source_manifest.get("split_policy") == "blind-holdout":
        from .evaluation import _verified_holdout
        if (source_manifest.get("source_database_verified_at_admission") is not True
                or not _verified_holdout(source_manifest, _load_cases(source / "cases.jsonl"), source)):
            raise EvaluationDatasetError("blind holdout requires verified human leakage review before labeling")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version)
            or dataset_version == source_report.dataset_version):
        raise EvaluationDatasetError("review output requires a new dataset_version")
    manifest_bytes = (source / "manifest.json").read_bytes()
    cases_bytes = (source / "cases.jsonl").read_bytes()
    batch_manifest = _load_json(batch / "manifest.json")
    if not isinstance(batch_manifest, dict):
        raise EvaluationDatasetError("review batch manifest must be an object")
    if batch_manifest.get("schema_version") != BATCH_VERSION:
        raise EvaluationDatasetError("unsupported human review batch schema")
    reviewer = _require_text(batch_manifest.get("reviewer_id"), "reviewer_id", "batch")
    if (batch_manifest.get("source_dataset_version") != source_report.dataset_version
            or batch_manifest.get("source_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or batch_manifest.get("source_cases_sha256") != hashlib.sha256(cases_bytes).hexdigest()):
        raise EvaluationDatasetError("review batch does not match frozen source dataset")
    if (batch_manifest.get("source") != "human"
            or batch_manifest.get("independent") is not True
            or batch_manifest.get("model_assistance") is not False):
        raise EvaluationDatasetError("batch requires independent human, no-model attestation")

    cases = _load_cases(source / "cases.jsonl")
    by_id = {case["case_id"]: case for case in cases}
    rows = _load_cases(batch / "reviews.jsonl")
    if not rows:
        raise EvaluationDatasetError("review batch is empty")
    seen = set()
    for row in rows:
        case_id = _require_text(row.get("case_id"), "case_id", "review")
        if case_id in seen or case_id not in by_id:
            raise EvaluationDatasetError(f"review case {case_id} is duplicate or unknown")
        seen.add(case_id)
        case = by_id[case_id]
        if case.get("text_storage") != "restricted_reference":
            raise EvaluationDatasetError(f"review case {case_id} requires a restricted real-data reference")
        if row.get("content_sha256") != case["content_sha256"]:
            raise EvaluationDatasetError(f"review case {case_id} content hash differs")
        _review_time(row.get("recorded_at"), case_id)
        if row.get("labels") not in ({"relevance": label} for label in RELEVANCE_LABELS):
            raise EvaluationDatasetError(f"review case {case_id} has invalid relevance label")
        annotation = case["annotation"]
        if annotation.get("state") not in {"unlabeled", "single_annotator"}:
            raise EvaluationDatasetError(f"review case {case_id} cannot receive another review")
        if annotation.get("generated_by_model") or annotation.get("labels"):
            raise EvaluationDatasetError(f"review case {case_id} has nonempty provisional gold")
        reviews = annotation.setdefault("reviews", [])
        if (not isinstance(reviews, list) or len(reviews) >= 2
                or any(review.get("reviewer_id") == reviewer for review in reviews)):
            raise EvaluationDatasetError(f"review case {case_id} requires a distinct reviewer")
        reviews.append({
            "reviewer_id": reviewer, "source": "human", "independent": True,
            "content_sha256": case["content_sha256"],
            "recorded_at": row["recorded_at"], "labels": row["labels"],
        })
        # Two reviews still have no adjudicated final label.
        annotation["state"] = "single_annotator"

    manifest = _load_json(source / "manifest.json")
    manifest.update({
        "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "parent_cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "review_batch_schema_version": BATCH_VERSION,
        "review_batch_manifest_sha256": _sha(batch / "manifest.json"),
        "review_batch_cases_sha256": _sha(batch / "reviews.jsonl"),
    })
    staging = Path(tempfile.mkdtemp(prefix="infohub-review-intake-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (staging / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
            encoding="utf-8")
        if source_manifest.get("split_policy") == "blind-holdout":
            shutil.copyfile(source / "holdout-review.json", staging / "holdout-review.json")
        result = validate_evaluation_dataset(staging)
        if result.publishable_gold:
            raise EvaluationDatasetError("review intake cannot publish gold before adjudication")
        if output.exists():
            raise EvaluationDatasetError("review output appeared during import")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    review_counts = [len(case["annotation"].get("reviews", [])) for case in cases]
    return ReviewIntakeReport(
        source_dataset_version=source_report.dataset_version, dataset_version=dataset_version,
        reviewed_cases=len(seen), one_review_cases=review_counts.count(1),
        two_review_cases=review_counts.count(2),
        adjudicated_cases=result.annotation_state_counts.get("adjudicated", 0),
        publishable_gold=result.publishable_gold,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a private human relevance review batch")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    result = import_review_batch(args.dataset, args.batch, args.output,
                                 dataset_version=args.dataset_version)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
