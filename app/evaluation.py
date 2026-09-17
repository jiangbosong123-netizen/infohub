from __future__ import annotations

"""Versioned evaluation datasets, leakage checks, and honest coverage reports."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ALLOWED_SPLITS = {"train", "dev", "test", "security"}
ALLOWED_ANNOTATION_STATES = {"unlabeled", "single_annotator", "adjudicated", "synthetic_fixture"}
SCHEMA_VERSION = "evaluation-dataset-v1"


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
        impact = labels.get("impact")
        if isinstance(impact, list):
            impact_count += len(impact)
        if annotation.get("generated_by_model") and state in {"single_annotator", "adjudicated"}:
            raise EvaluationDatasetError(f"case {case_id} cannot use model output as gold")

    target_values = {
        "documents": int(targets.get("documents", 0)),
        "event_groups": int(targets.get("event_groups", 0)),
        "impact_annotations": int(targets.get("impact_annotations", 0)),
        "security_cases": int(targets.get("security_cases", 0)),
    }
    actual = {
        "documents": len(documents), "event_groups": len(event_groups),
        "impact_annotations": impact_count, "security_cases": security_count,
    }
    gaps = {key: max(0, target_values[key] - actual[key]) for key in target_values}
    publishable = (
        not any(gaps.values())
        and states.get("adjudicated", 0) == len(cases)
        and len(cases) > 0
    )
    if not publishable:
        warnings.append("dataset is not publishable gold; target size and/or adjudication is incomplete")
    if states.get("synthetic_fixture"):
        warnings.append("synthetic fixtures validate tooling only and do not measure model quality")
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
