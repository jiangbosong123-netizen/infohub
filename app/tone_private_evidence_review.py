from __future__ import annotations

"""Freeze a human review of private tone evidence against immutable text artifacts."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .evaluation import EvaluationDatasetError, _load_cases, _load_json, _require_text, _review_time
from .tone_evaluation import _verified_private_evidence, validate_tone_evaluation_dataset
from .tone_review_intake import _copy_holdout_record, _require_verified_holdout

ARTIFACT_SCHEMA_VERSION = "tone-private-artifacts-v1"
REVIEW_VERSION = "tone-evidence-review-v1"
_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class TonePrivateEvidenceReviewReport:
    source_dataset_version: str
    dataset_version: str
    cases_verified: int
    labels_verified: int
    spans_verified: int
    publishable_tone_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private_path(path: Path, *, field: str) -> None:
    repository = Path(__file__).parents[1].resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(repository) and not resolved.is_relative_to(
        repository / "evaluation" / "private"
    ):
        raise EvaluationDatasetError(f"{field} must be private")


def _artifact_text(root: Path, reference: object, *, case_id: str) -> tuple[str, bytes]:
    if not isinstance(reference, str) or not reference:
        raise EvaluationDatasetError(f"tone evidence case {case_id} has invalid text_ref")
    pure = PurePosixPath(reference)
    if pure.is_absolute() or ".." in pure.parts or "\\" in reference:
        raise EvaluationDatasetError(f"tone evidence case {case_id} text_ref escapes artifact root")
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise EvaluationDatasetError(f"tone evidence case {case_id} artifact cannot use symlinks")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise EvaluationDatasetError(
            f"tone evidence case {case_id} artifact is missing"
        ) from exc
    if not resolved.is_relative_to(resolved_root):
        raise EvaluationDatasetError(f"tone evidence case {case_id} text_ref escapes artifact root")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise EvaluationDatasetError(f"tone evidence case {case_id} artifact is not a regular file")
    payload = resolved.read_bytes()
    try:
        return payload.decode("utf-8"), payload
    except UnicodeDecodeError as exc:
        raise EvaluationDatasetError(
            f"tone evidence case {case_id} artifact is not UTF-8"
        ) from exc


def _verify_label_spans(case_id: str, labels: dict, text: str) -> int:
    spans = labels["tone"]["evidence"]
    for span in spans:
        start, end = span["start_offset"], span["end_offset"]
        if end > len(text):
            raise EvaluationDatasetError(f"tone evidence case {case_id} offset exceeds artifact")
        quote = text[start:end]
        if hashlib.sha256(quote.encode("utf-8")).hexdigest() != span["quote_sha256"]:
            raise EvaluationDatasetError(
                f"tone evidence case {case_id} quote hash or offsets differ from artifact"
            )
    return len(spans)


def freeze_tone_private_evidence_review(
    source: Path | str,
    artifacts: Path | str,
    review_file: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> TonePrivateEvidenceReviewReport:
    source, artifacts, review_file, output = map(Path, (source, artifacts, review_file, output))
    resolved_output = output.resolve()
    if (
        output.exists()
        or resolved_output.is_relative_to(source.resolve())
        or resolved_output.is_relative_to(artifacts.resolve())
    ):
        raise EvaluationDatasetError("tone evidence review output must be a new immutable directory")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("tone evidence review output parent directory must exist")
    _private_path(output, field="tone evidence review output")
    _private_path(artifacts, field="tone artifact bundle")
    _private_path(review_file, field="tone evidence review record")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version):
        raise EvaluationDatasetError("tone evidence review requires a simple dataset_version")

    source_report = validate_tone_evaluation_dataset(source)
    if dataset_version == source_report.dataset_version:
        raise EvaluationDatasetError("tone evidence review requires a new dataset_version")
    source_manifest_path = source / "manifest.json"
    source_cases_path = source / "cases.jsonl"
    source_manifest_bytes = source_manifest_path.read_bytes()
    source_cases_bytes = source_cases_path.read_bytes()
    source_manifest = _load_json(source_manifest_path)
    cases = _load_cases(source_cases_path)
    _require_verified_holdout(source, source_manifest)
    if not cases or any(
        case.get("text_storage") != "restricted_reference"
        or case["annotation"].get("state") != "adjudicated"
        for case in cases
    ):
        raise EvaluationDatasetError(
            "tone evidence review requires every case to be restricted and adjudicated"
        )

    artifact_manifest_path = artifacts / "manifest.json"
    artifact_rows_path = artifacts / "artifacts.jsonl"
    artifact_manifest = _load_json(artifact_manifest_path)
    if not isinstance(artifact_manifest, dict) or set(artifact_manifest) != {
        "schema_version",
        "source_dataset_version",
        "source_manifest_sha256",
        "source_cases_sha256",
        "artifacts_sha256",
        "case_count",
    }:
        raise EvaluationDatasetError("tone artifact manifest has invalid fields")
    expected_source = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "source_dataset_version": source_report.dataset_version,
        "source_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
        "source_cases_sha256": hashlib.sha256(source_cases_bytes).hexdigest(),
        "artifacts_sha256": _sha(artifact_rows_path),
        "case_count": len(cases),
    }
    if artifact_manifest != expected_source:
        raise EvaluationDatasetError("tone artifact bundle does not match frozen source dataset")

    rows = _load_cases(artifact_rows_path)
    by_case: dict[str, dict] = {}
    exact_row_fields = {
        "case_id", "object_ref", "content_sha256", "normalizer_version",
        "text_ref", "text_sha256", "size_bytes",
    }
    for row in rows:
        if set(row) != exact_row_fields:
            raise EvaluationDatasetError("tone artifact row has invalid fields")
        case_id = _require_text(row.get("case_id"), "case_id", "tone artifact")
        if case_id in by_case:
            raise EvaluationDatasetError(f"tone artifact case {case_id} is duplicate")
        by_case[case_id] = row
    if set(by_case) != {case["case_id"] for case in cases}:
        raise EvaluationDatasetError("tone artifact bundle must cover every source case exactly once")

    labels_verified = 0
    spans_verified = 0
    people: set[str] = set()
    for case in cases:
        case_id = case["case_id"]
        row = by_case[case_id]
        if (
            row.get("object_ref") != case.get("object_ref")
            or row.get("content_sha256") != case.get("content_sha256")
            or not isinstance(row.get("normalizer_version"), str)
            or not row["normalizer_version"].strip()
            or not isinstance(row.get("text_sha256"), str)
            or _HASH.fullmatch(row["text_sha256"]) is None
            or type(row.get("size_bytes")) is not int
            or row["size_bytes"] < 0
        ):
            raise EvaluationDatasetError(f"tone artifact case {case_id} metadata differs")
        text, payload = _artifact_text(artifacts, row["text_ref"], case_id=case_id)
        if len(payload) != row["size_bytes"] or hashlib.sha256(payload).hexdigest() != row[
            "text_sha256"
        ]:
            raise EvaluationDatasetError(f"tone artifact case {case_id} text hash or size differs")
        annotation = case["annotation"]
        labels = [review["labels"] for review in annotation["reviews"]]
        labels.append(annotation["labels"])
        for review in annotation["reviews"]:
            people.add(review["reviewer_id"])
        people.add(annotation["adjudication"]["adjudicator_id"])
        for label in labels:
            spans_verified += _verify_label_spans(case_id, label, text)
            labels_verified += 1

    review_bytes = review_file.read_bytes()
    review = _load_json(review_file)
    required_review_fields = {
        "protocol_version", "source_dataset_version", "source_manifest_sha256",
        "source_cases_sha256", "cases_sha256", "artifact_manifest_sha256", "artifacts_sha256",
        "verifier_id", "recorded_at", "source", "model_assistance",
        "all_cases_verified", "quote_hash_and_offsets_verified",
        "normalized_artifacts_verified", "inspection_notes",
    }
    if not isinstance(review, dict) or set(review) != required_review_fields:
        raise EvaluationDatasetError("tone evidence review record has invalid fields")
    verifier = _require_text(review.get("verifier_id"), "verifier_id", "tone evidence review")
    _review_time(review.get("recorded_at"), "tone evidence review")
    if verifier in people:
        raise EvaluationDatasetError("tone evidence verifier must be independent of reviewers and adjudicators")
    required_attestation = {
        "protocol_version": REVIEW_VERSION,
        "source_dataset_version": source_report.dataset_version,
        "source_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
        "source_cases_sha256": hashlib.sha256(source_cases_bytes).hexdigest(),
        "cases_sha256": hashlib.sha256(source_cases_bytes).hexdigest(),
        "artifact_manifest_sha256": _sha(artifact_manifest_path),
        "artifacts_sha256": _sha(artifact_rows_path),
        "source": "human",
        "model_assistance": False,
        "all_cases_verified": True,
        "quote_hash_and_offsets_verified": True,
        "normalized_artifacts_verified": True,
    }
    if any(review.get(key) != value for key, value in required_attestation.items()):
        raise EvaluationDatasetError("tone evidence review does not match artifacts or attestation")
    _require_text(review.get("inspection_notes"), "inspection_notes", "tone evidence review")

    cases_sha256 = hashlib.sha256(source_cases_bytes).hexdigest()
    frozen_review = {
        "status": "verified",
        "protocol_version": REVIEW_VERSION,
        "source": "human",
        "model_assistance": False,
        "all_cases_verified": True,
        "quote_hash_and_offsets_verified": True,
        "normalized_artifacts_verified": True,
        "verifier_id": verifier,
        "recorded_at": review["recorded_at"],
        "cases_sha256": cases_sha256,
        "review_record_sha256": hashlib.sha256(review_bytes).hexdigest(),
        "artifact_manifest_sha256": required_attestation["artifact_manifest_sha256"],
        "artifacts_sha256": required_attestation["artifacts_sha256"],
        "normalizer_versions": sorted({row["normalizer_version"] for row in rows}),
        "labels_verified": labels_verified,
        "spans_verified": spans_verified,
    }
    output_manifest = {
        **source_manifest,
        "dataset_version": dataset_version,
        "parent_dataset_version": source_report.dataset_version,
        "parent_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
        "parent_cases_sha256": cases_sha256,
        "tone_evidence_review": frozen_review,
    }
    staging = Path(tempfile.mkdtemp(prefix="infohub-tone-evidence-review-", dir=output.parent))
    try:
        (staging / "manifest.json").write_text(
            json.dumps(output_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "cases.jsonl").write_bytes(source_cases_bytes)
        (staging / "tone-evidence-review.json").write_bytes(review_bytes)
        _copy_holdout_record(source, staging, source_manifest)
        if not _verified_private_evidence(output_manifest, staging):
            raise EvaluationDatasetError("frozen tone evidence review failed hash verification")
        result = validate_tone_evaluation_dataset(staging)
        if output.exists():
            raise EvaluationDatasetError("tone evidence review output appeared during freeze")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return TonePrivateEvidenceReviewReport(
        source_dataset_version=source_report.dataset_version,
        dataset_version=dataset_version,
        cases_verified=len(cases),
        labels_verified=labels_verified,
        spans_verified=spans_verified,
        publishable_tone_gold=result.publishable_tone_gold,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze private tone evidence verification")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    report = freeze_tone_private_evidence_review(
        args.dataset,
        args.artifacts,
        args.review,
        args.output,
        dataset_version=args.dataset_version,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
