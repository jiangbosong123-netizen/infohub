from __future__ import annotations

"""Versioned evaluation datasets, leakage checks, and honest coverage reports."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

ALLOWED_SPLITS = {"train", "dev", "test", "security"}
ALLOWED_ANNOTATION_STATES = {"unlabeled", "single_annotator", "adjudicated", "synthetic_fixture"}
SCHEMA_VERSION = "evaluation-dataset-v1"
MINIMUM_GOLD_TARGETS = {"documents": 600, "event_groups": 150,
                        "impact_annotations": 300, "security_cases": 50}
HOLDOUT_CHECKS = (
    "origin_and_translation_groups", "announcement_revisions",
    "source_independence", "time_window", "training_exclusion", "usage_rights",
)


class EvaluationDatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationReport:
    dataset_version: str
    cases: int
    documents: int
    event_groups: int
    impact_annotations: int
    security_cases: int
    split_counts: dict[str, int]
    annotation_state_counts: dict[str, int]
    target_gaps: dict[str, int]
    publishable_gold: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDatasetError(f"cannot read JSON {path}: {exc}") from exc


def _load_cases(path: Path) -> list[dict]:
    cases = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationDatasetError(f"cannot read cases: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationDatasetError(f"invalid case JSON at line {number}") from exc
        if not isinstance(value, dict):
            raise EvaluationDatasetError(f"case at line {number} is not an object")
        cases.append(value)
    return cases


def _require_text(value: object, field: str, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationDatasetError(f"case {case_id} requires {field}")
    return value


def _review_time(value: object, case_id: str) -> None:
    stamp = _require_text(value, "recorded_at", case_id)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError(f"case {case_id} has invalid review time") from exc
    if parsed.tzinfo is None:
        raise EvaluationDatasetError(f"case {case_id} review time must include timezone")


def _validate_human_reviews(case_id: str, annotation: dict, digest: str,
                            *, require_two: bool) -> set[str]:
    reviews = annotation.get("reviews")
    if not isinstance(reviews, list) or not 1 <= len(reviews) <= 2 or (require_two and len(reviews) != 2):
        raise EvaluationDatasetError(f"case {case_id} requires {'two' if require_two else 'one or two'} independent human reviews")
    reviewers: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise EvaluationDatasetError(f"case {case_id} has invalid review")
        reviewer = _require_text(review.get("reviewer_id"), "reviewer_id", case_id)
        if reviewer in reviewers or review.get("source") != "human" or review.get("independent") is not True:
            raise EvaluationDatasetError(f"case {case_id} requires distinct independent human reviewers")
        if review.get("content_sha256") != digest or not isinstance(review.get("labels"), dict) or not review["labels"]:
            raise EvaluationDatasetError(f"case {case_id} review lacks frozen-content labels")
        _review_time(review.get("recorded_at"), case_id)
        reviewers.add(reviewer)
    return reviewers


def _validate_adjudication(case_id: str, annotation: dict, digest: str) -> None:
    reviewers = _validate_human_reviews(case_id, annotation, digest, require_two=True)
    decision = annotation.get("adjudication")
    if not isinstance(decision, dict):
        raise EvaluationDatasetError(f"case {case_id} requires a human adjudication")
    adjudicator = _require_text(decision.get("adjudicator_id"), "adjudicator_id", case_id)
    if (adjudicator in reviewers or decision.get("source") != "human"
            or decision.get("content_sha256") != digest
            or decision.get("labels") != annotation.get("labels")):
        raise EvaluationDatasetError(f"case {case_id} has invalid adjudication provenance")
    _review_time(decision.get("recorded_at"), case_id)


def _verified_holdout(manifest: dict, cases: list[dict], root: Path | None = None) -> bool:
    review = manifest.get("holdout_review")
    if not isinstance(review, dict) or review.get("status") != "verified":
        return False
    if not isinstance(review.get("reviewer_id"), str) or not review["reviewer_id"].strip():
        return False
    if (review.get("protocol_version") != "holdout-review-v1"
            or not isinstance(review.get("checks"), dict)
            or any(review["checks"].get(key) is not True for key in HOLDOUT_CHECKS)
            or any(not isinstance(review.get(key), str)
                   or re.fullmatch(r"[0-9a-f]{64}", review[key]) is None
                   for key in ("source_manifest_sha256", "source_cases_sha256", "review_record_sha256"))):
        return False
    if root is not None:
        try:
            record_bytes = (root / "holdout-review.json").read_bytes()
            record = json.loads(record_bytes)
        except (OSError, json.JSONDecodeError):
            return False
        if (hashlib.sha256(record_bytes).hexdigest() != review["review_record_sha256"]
                or not isinstance(record, dict)
                or record.get("schema_version") != "holdout-review-v1"
                or record.get("source") != "human"
                or record.get("model_assistance") is not False
                or not isinstance(record.get("inspection_notes"), str)
                or not record["inspection_notes"].strip()
                or any(record.get(key) != review.get(key) for key in (
                    "reviewer_id", "recorded_at", "heldout_after", "heldout_source_refs", "checks"))
                or record.get("source_manifest_sha256") != review["source_manifest_sha256"]
                or record.get("source_cases_sha256") != review["source_cases_sha256"]):
            return False
    try:
        _review_time(review.get("recorded_at"), "holdout_review")
    except EvaluationDatasetError:
        return False
    sources = review.get("heldout_source_refs")
    cutoff_text = review.get("heldout_after")
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(value, str) or not value for value in sources)
            or not isinstance(cutoff_text, str)):
        return False
    try:
        cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if cutoff.tzinfo is None:
        return False
    natural = [case for case in cases if case["split"] != "security"]
    source_cases = [case for case in natural if case.get("source_kind") in sources]
    if not source_cases or any(case["split"] != "test" for case in source_cases):
        return False
    recent = []
    for case in natural:
        value = case.get("published_at")
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            return False
        if stamp >= cutoff:
            recent.append(case)
    if not recent or any(case["split"] != "test" for case in recent):
        return False
    return True


def validate_evaluation_dataset(path: Path | str) -> EvaluationReport:
    root = Path(path)
    manifest = _load_json(root / "manifest.json")
    cases = _load_cases(root / "cases.jsonl")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise EvaluationDatasetError("unsupported evaluation dataset schema")
    dataset_version = _require_text(manifest.get("dataset_version"), "dataset_version", "manifest")
    targets = manifest.get("target_plan")
    if not isinstance(targets, dict):
        raise EvaluationDatasetError("manifest target_plan must be an object")

    ids: set[str] = set()
    hashes: dict[str, str] = {}
    group_splits: dict[tuple[str, str], str] = {}
    split_counts = {name: 0 for name in sorted(ALLOWED_SPLITS)}
    states: dict[str, int] = {}
    event_groups: set[str] = set()
    documents: set[str] = set()
    impact_count = 0
    security_count = 0
    language_counts: dict[str, int] = {}
    warnings: list[str] = []

    for case in cases:
        case_id = _require_text(case.get("case_id"), "case_id", "unknown")
        if case_id in ids:
            raise EvaluationDatasetError(f"duplicate case_id {case_id}")
        ids.add(case_id)
        split = case.get("split")
        if split not in ALLOWED_SPLITS:
            raise EvaluationDatasetError(f"case {case_id} has invalid split")
        split_counts[split] += 1
        if split == "security":
            security_count += 1
        event_group = _require_text(case.get("event_group_id"), "event_group_id", case_id)
        origin_group = _require_text(case.get("origin_group_id"), "origin_group_id", case_id)
        event_groups.add(event_group)
        for kind, group in (("event", event_group), ("origin", origin_group)):
            previous = group_splits.setdefault((kind, group), split)
            if previous != split:
                raise EvaluationDatasetError(
                    f"{kind} group {group} leaks across {previous} and {split}"
                )
        document_ref = _require_text(case.get("document_ref"), "document_ref", case_id)
        documents.add(document_ref)
        language = _require_text(case.get("language"), "language", case_id)
        language_counts[language] = language_counts.get(language, 0) + 1
        storage = case.get("text_storage")
        if storage == "synthetic_embedded":
            text = _require_text(case.get("text"), "text", case_id)
            digest = _require_text(case.get("content_sha256"), "content_sha256", case_id)
            if _sha_text(text) != digest:
                raise EvaluationDatasetError(f"case {case_id} content hash mismatch")
        elif storage == "restricted_reference":
            if case.get("text") is not None:
                raise EvaluationDatasetError(f"restricted case {case_id} embeds text")
            _require_text(case.get("object_ref"), "object_ref", case_id)
            digest = _require_text(case.get("content_sha256"), "content_sha256", case_id)
        else:
            raise EvaluationDatasetError(f"case {case_id} has unsupported text_storage")
        previous_content_split = group_splits.setdefault(("content", digest), split)
        if previous_content_split != split:
            raise EvaluationDatasetError(
                f"content hash {digest} leaks across {previous_content_split} and {split}"
            )
        previous_document = hashes.setdefault(document_ref, digest)
        if previous_document != digest:
            raise EvaluationDatasetError(f"document {document_ref} has conflicting hashes")
        annotation = case.get("annotation")
        if not isinstance(annotation, dict):
            raise EvaluationDatasetError(f"case {case_id} requires annotation")
        state = annotation.get("state")
        if state not in ALLOWED_ANNOTATION_STATES:
            raise EvaluationDatasetError(f"case {case_id} has invalid annotation state")
        states[state] = states.get(state, 0) + 1
        labels = annotation.get("labels")
        if not isinstance(labels, dict):
            raise EvaluationDatasetError(f"case {case_id} labels must be an object")
        if state == "unlabeled" and labels:
            raise EvaluationDatasetError(f"case {case_id} has labels while marked unlabeled")
        if annotation.get("generated_by_model") and state in {"single_annotator", "adjudicated"}:
            raise EvaluationDatasetError(f"case {case_id} cannot use model output as gold")
        if state == "single_annotator":
            if labels:
                raise EvaluationDatasetError(f"case {case_id} provisional reviews cannot supply gold labels")
            _validate_human_reviews(case_id, annotation, digest, require_two=False)
        if state == "adjudicated":
            if storage != "restricted_reference":
                raise EvaluationDatasetError(f"case {case_id} synthetic text cannot become gold")
            if not labels:
                raise EvaluationDatasetError(f"case {case_id} has no adjudicated labels")
            _validate_adjudication(case_id, annotation, digest)
        impact = labels.get("impact")
        if isinstance(impact, list):
            impact_count += len(impact)

    target_values = {}
    for key, minimum in MINIMUM_GOLD_TARGETS.items():
        declared = targets.get(key, minimum)
        if type(declared) is not int or declared < 0:
            raise EvaluationDatasetError(f"manifest has invalid target {key}")
        target_values[key] = max(minimum, declared)
    actual = {
        "documents": len(documents), "event_groups": len(event_groups),
        "impact_annotations": impact_count, "security_cases": security_count,
    }
    gaps = {key: max(0, target_values[key] - actual[key]) for key in target_values}
    publishable = (
        not any(gaps.values())
        and states.get("adjudicated", 0) == len(cases)
        and len(cases) > 0
        and language_counts.get("en", 0) >= 200
        and language_counts.get("zh", 0) >= 200
        and all(split_counts[name] > 0 for name in ALLOWED_SPLITS)
        and manifest.get("source_database_verified_at_admission") is True
        and _verified_holdout(manifest, cases, root)
    )
    if not publishable:
        warnings.append("dataset is not publishable gold; target size and/or adjudication is incomplete")
    if states.get("synthetic_fixture"):
        warnings.append("synthetic fixtures validate tooling only and do not measure model quality")
    if not _verified_holdout(manifest, cases, root):
        warnings.append("time/source blind holdout is not verified")
    return EvaluationReport(
        dataset_version=dataset_version, cases=len(cases), documents=len(documents),
        event_groups=len(event_groups), impact_annotations=impact_count,
        security_cases=security_count, split_counts=split_counts,
        annotation_state_counts=states, target_gaps=gaps,
        publishable_gold=publishable, warnings=tuple(warnings),
    )


def write_evaluation_report(dataset_path: Path | str, output: Path | str) -> EvaluationReport:
    report = validate_evaluation_dataset(dataset_path)
    Path(output).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
