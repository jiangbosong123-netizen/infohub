from __future__ import annotations

"""Freeze third-person tone adjudications into a new private dataset version."""

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
    EvaluationDatasetError,
    _load_cases,
    _load_json,
    _require_text,
    _review_time,
    _validate_human_reviews,
)
from .tone_evaluation import _validate_tone_label, validate_tone_evaluation_dataset
from .tone_review_intake import _copy_holdout_record, _require_verified_holdout

BATCH_VERSION = "human-tone-adjudication-batch-v1"


@dataclass(frozen=True)
class ToneAdjudicationReport:
    source_dataset_version: str
    dataset_version: str
    adjudicated_in_batch: int
    total_adjudicated: int
    publishable_tone_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def adjudicate_tone(
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> ToneAdjudicationReport:
    source, batch, output = Path(source), Path(batch), Path(output)
    resolved_output = output.resolve()
    if (
        output.exists()
        or resolved_output.is_relative_to(source.resolve())
        or resolved_output.is_relative_to(batch.resolve())
    ):
        raise EvaluationDatasetError("tone adjudication output must be a new immutable directory")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("tone adjudication output parent directory must exist")
    repository = Path(__file__).parents[1].resolve()
    private_root = repository / "evaluation" / "private"
    if resolved_output.is_relative_to(repository) and not resolved_output.is_relative_to(private_root):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if batch.resolve().is_relative_to(repository) and not batch.resolve().is_relative_to(private_root):
        raise EvaluationDatasetError("tone adjudication batch must be private")

    source_report = validate_tone_evaluation_dataset(source)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version)
        or dataset_version == source_report.dataset_version
    ):
        raise EvaluationDatasetError("tone adjudication requires a new dataset_version")
    manifest_bytes = (source / "manifest.json").read_bytes()
    cases_bytes = (source / "cases.jsonl").read_bytes()
    source_manifest = _load_json(source / "manifest.json")
    cases = _load_cases(source / "cases.jsonl")
    _require_verified_holdout(source, source_manifest)

    batch_manifest = _load_json(batch / "manifest.json")
    if not isinstance(batch_manifest, dict) or batch_manifest.get("schema_version") != BATCH_VERSION:
        raise EvaluationDatasetError("unsupported human tone adjudication batch schema")
    adjudicator = _require_text(
        batch_manifest.get("adjudicator_id"), "adjudicator_id", "tone batch"
    )
    if (
        batch_manifest.get("source_dataset_version") != source_report.dataset_version
        or batch_manifest.get("source_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
        or batch_manifest.get("source_cases_sha256") != hashlib.sha256(cases_bytes).hexdigest()
    ):
        raise EvaluationDatasetError("tone adjudication batch does not match frozen dataset")
    if batch_manifest.get("source") != "human" or batch_manifest.get("model_assistance") is not False:
        raise EvaluationDatasetError("tone adjudication requires human no-model attestation")

    by_id = {case["case_id"]: case for case in cases}
    rows = _load_cases(batch / "decisions.jsonl")
    if not rows:
        raise EvaluationDatasetError("tone adjudication batch is empty")
    seen: set[str] = set()
    for row in rows:
        if set(row) != {"case_id", "content_sha256", "recorded_at", "labels", "reason"}:
            raise EvaluationDatasetError("tone adjudication row has invalid fields")
        case_id = _require_text(row.get("case_id"), "case_id", "tone decision")
        if case_id in seen or case_id not in by_id:
            raise EvaluationDatasetError(f"tone adjudication case {case_id} is duplicate or unknown")
        seen.add(case_id)
        case = by_id[case_id]
        annotation = case["annotation"]
        if case["text_storage"] != "restricted_reference" or annotation.get(
            "state"
        ) != "single_annotator":
            raise EvaluationDatasetError(
                f"tone adjudication case {case_id} is not awaiting a real-data decision"
            )
        if row.get("content_sha256") != case["content_sha256"]:
            raise EvaluationDatasetError(f"tone adjudication case {case_id} content hash differs")
        reviewers = _validate_human_reviews(
            case_id, annotation, case["content_sha256"], require_two=True
        )
        if adjudicator in reviewers:
            raise EvaluationDatasetError(f"tone adjudication case {case_id} requires a third person")
        _validate_tone_label(case, row.get("labels"))
        _review_time(row.get("recorded_at"), case_id)
        decision_time = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
        if any(
            datetime.fromisoformat(review["recorded_at"].replace("Z", "+00:00"))
            > decision_time
            for review in annotation["reviews"]
        ):
            raise EvaluationDatasetError(f"tone adjudication case {case_id} precedes a human review")
        reason = _require_text(row.get("reason"), "reason", case_id)
        if len(reason) > 2_000:
            raise EvaluationDatasetError(f"tone adjudication case {case_id} reason is too long")
        annotation["labels"] = row["labels"]
        annotation["state"] = "adjudicated"
        annotation["adjudication"] = {
            "adjudicator_id": adjudicator,
            "source": "human",
            "content_sha256": case["content_sha256"],
            "recorded_at": row["recorded_at"],
            "labels": row["labels"],
            "reason": reason,
        }

    output_manifest = {
        **source_manifest,
        "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "parent_cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "tone_adjudication_batch_schema_version": BATCH_VERSION,
        "tone_adjudication_batch_manifest_sha256": hashlib.sha256(
            (batch / "manifest.json").read_bytes()
        ).hexdigest(),
        "tone_adjudication_batch_decisions_sha256": hashlib.sha256(
            (batch / "decisions.jsonl").read_bytes()
        ).hexdigest(),
    }
    output_manifest.pop("tone_evidence_review", None)
    staging = Path(tempfile.mkdtemp(prefix="infohub-tone-adjudication-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(output_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
            encoding="utf-8",
        )
        _copy_holdout_record(source, staging, source_manifest)
        result = validate_tone_evaluation_dataset(staging)
        if output.exists():
            raise EvaluationDatasetError("tone adjudication output appeared during import")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return ToneAdjudicationReport(
        source_dataset_version=source_report.dataset_version,
        dataset_version=dataset_version,
        adjudicated_in_batch=len(seen),
        total_adjudicated=result.annotation_state_counts.get("adjudicated", 0),
        publishable_tone_gold=result.publishable_tone_gold,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze private third-person tone adjudications")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    report = adjudicate_tone(
        args.dataset, args.batch, args.output, dataset_version=args.dataset_version
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
