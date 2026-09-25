from __future__ import annotations

"""Publish a third person's relevance adjudication into an immutable private dataset."""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import (
    EvaluationDatasetError, _load_cases, _load_json, _require_text,
    _review_time, _validate_human_reviews, _verified_holdout,
    validate_evaluation_dataset,
)
from .evaluation_review_intake import RELEVANCE_LABELS

BATCH_VERSION = "human-relevance-adjudication-batch-v1"


@dataclass(frozen=True)
class AdjudicationReport:
    source_dataset_version: str
    dataset_version: str
    adjudicated_in_batch: int
    total_adjudicated: int
    publishable_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def adjudicate_relevance(source: Path | str, batch: Path | str,
                         output: Path | str, *, dataset_version: str) -> AdjudicationReport:
    source, batch, output = Path(source), Path(batch), Path(output)
    if (output.exists() or output.resolve().is_relative_to(source.resolve())
            or output.resolve().is_relative_to(batch.resolve())):
        raise EvaluationDatasetError("adjudication output must be a new immutable directory")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("adjudication output parent directory must exist")
    repository = Path(__file__).parents[1].resolve()
    if output.resolve().is_relative_to(repository) and not output.resolve().is_relative_to(
            repository / "evaluation" / "private"):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if batch.resolve().is_relative_to(repository) and not batch.resolve().is_relative_to(
            repository / "evaluation" / "private"):
        raise EvaluationDatasetError("adjudication batch must be private")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version):
        raise EvaluationDatasetError("dataset_version must be a simple identifier")

    source_report = validate_evaluation_dataset(source)
    if dataset_version == source_report.dataset_version:
        raise EvaluationDatasetError("adjudication requires a new dataset_version")
    manifest_bytes = (source / "manifest.json").read_bytes()
    cases_bytes = (source / "cases.jsonl").read_bytes()
    manifest = _load_json(source / "manifest.json")
    cases = _load_cases(source / "cases.jsonl")
    if (manifest.get("split_policy") == "blind-holdout"
            and (manifest.get("source_database_verified_at_admission") is not True
                 or not _verified_holdout(manifest, cases, source))):
        raise EvaluationDatasetError("adjudication requires verified blind holdout")
    batch_manifest = _load_json(batch / "manifest.json")
    if not isinstance(batch_manifest, dict) or batch_manifest.get("schema_version") != BATCH_VERSION:
        raise EvaluationDatasetError("unsupported adjudication batch schema")
    adjudicator = _require_text(batch_manifest.get("adjudicator_id"), "adjudicator_id", "batch")
    if (batch_manifest.get("source_dataset_version") != source_report.dataset_version
            or batch_manifest.get("source_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or batch_manifest.get("source_cases_sha256") != hashlib.sha256(cases_bytes).hexdigest()):
        raise EvaluationDatasetError("adjudication batch does not match frozen dataset")
    if batch_manifest.get("source") != "human" or batch_manifest.get("model_assistance") is not False:
        raise EvaluationDatasetError("adjudication requires human no-model attestation")

    by_id = {case["case_id"]: case for case in cases}
    rows = _load_cases(batch / "decisions.jsonl")
    if not rows:
        raise EvaluationDatasetError("adjudication batch is empty")
    seen = set()
    for row in rows:
        case_id = _require_text(row.get("case_id"), "case_id", "decision")
        if case_id in seen or case_id not in by_id:
            raise EvaluationDatasetError(f"adjudication case {case_id} is duplicate or unknown")
        seen.add(case_id)
        case = by_id[case_id]
        annotation = case["annotation"]
        if case["text_storage"] != "restricted_reference" or annotation.get("state") != "single_annotator":
            raise EvaluationDatasetError(f"adjudication case {case_id} is not awaiting a real-data decision")
        if row.get("content_sha256") != case["content_sha256"]:
            raise EvaluationDatasetError(f"adjudication case {case_id} content hash differs")
        reviewers = _validate_human_reviews(case_id, annotation, case["content_sha256"], require_two=True)
        if adjudicator in reviewers:
            raise EvaluationDatasetError(f"adjudication case {case_id} requires a third person")
        if any(review["labels"] not in ({"relevance": value} for value in RELEVANCE_LABELS)
               for review in annotation["reviews"]):
            raise EvaluationDatasetError(f"adjudication case {case_id} has non-relevance reviews")
        if row.get("labels") not in ({"relevance": value} for value in RELEVANCE_LABELS):
            raise EvaluationDatasetError(f"adjudication case {case_id} has invalid relevance label")
        _review_time(row.get("recorded_at"), case_id)
        decision_time = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
        if any(datetime.fromisoformat(review["recorded_at"].replace("Z", "+00:00")) > decision_time
               for review in annotation["reviews"]):
            raise EvaluationDatasetError(f"adjudication case {case_id} precedes a human review")
        reason = _require_text(row.get("reason"), "reason", case_id)
        annotation["labels"] = row["labels"]
        annotation["state"] = "adjudicated"
        annotation["adjudication"] = {
            "adjudicator_id": adjudicator, "source": "human",
            "content_sha256": case["content_sha256"],
            "recorded_at": row["recorded_at"], "labels": row["labels"], "reason": reason,
        }

    output_manifest = {
        **manifest, "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "parent_cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "adjudication_batch_schema_version": BATCH_VERSION,
        "adjudication_batch_manifest_sha256": hashlib.sha256((batch / "manifest.json").read_bytes()).hexdigest(),
        "adjudication_batch_decisions_sha256": hashlib.sha256((batch / "decisions.jsonl").read_bytes()).hexdigest(),
    }
    staging = Path(tempfile.mkdtemp(prefix="infohub-adjudication-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(output_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (staging / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
            encoding="utf-8")
        if manifest.get("split_policy") == "blind-holdout":
            shutil.copyfile(source / "holdout-review.json", staging / "holdout-review.json")
        result = validate_evaluation_dataset(staging)
        if output.exists():
            raise EvaluationDatasetError("adjudication output appeared during import")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return AdjudicationReport(source_report.dataset_version, dataset_version, len(seen),
                              result.annotation_state_counts.get("adjudicated", 0),
                              result.publishable_gold)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a private third-person relevance adjudication")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    result = adjudicate_relevance(args.dataset, args.batch, args.output,
                                  dataset_version=args.dataset_version)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
