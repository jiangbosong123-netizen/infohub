from __future__ import annotations

"""Read-only agreement metrics for a fixed pair of independent impact reviewers."""

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .agreement_stats import CategoricalAgreement, categorical_agreement, empty_confusion
from .evaluation import ALLOWED_SPLITS, EvaluationDatasetError, _load_cases, _require_text
from .impact_contracts import DIRECTIONS
from .impact_evaluation import (
    EVIDENCE_JUDGMENTS,
    EXPECTED_STATUSES,
    validate_impact_evaluation_dataset,
)

REPORT_VERSION = "impact-review-agreement-v1"
KAPPA_TARGET = 0.70
MINIMUM_GATE_PAIRS = 30
GATE_FIELD = "direction"
CATEGORICAL_FIELDS = {
    "direction": tuple(sorted(DIRECTIONS)),
    "expected_status": tuple(sorted(EXPECTED_STATUSES)),
    "evidence_judgment": tuple(sorted(EVIDENCE_JUDGMENTS)),
}
COMPONENTS = (
    "expected_status",
    "target",
    "aspect",
    "horizon",
    "direction",
    "intensity_band",
    "evidence_judgment",
    "evidence",
    "mechanism_presence",
    "phenomena",
    "structured_label",
    "full_label",
)


@dataclass(frozen=True)
class ComponentAgreement:
    matches: int
    total: int
    agreement: float | None


@dataclass(frozen=True)
class ImpactAgreementReport:
    report_version: str
    dataset_version: str
    scope_split: str
    reviewer_a: str
    reviewer_b: str
    dataset_cases: int
    paired_cases: int
    paired_by_split: dict[str, int]
    paired_by_language: dict[str, int]
    paired_by_source_kind: dict[str, int]
    categorical: dict[str, CategoricalAgreement]
    kappa_target: float
    direction_kappa_passed: bool
    components: dict[str, ComponentAgreement]
    quality_gate_eligible: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _evidence_signature(label: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # IDs are role-bound to the frozen case evidence, so equal IDs mean equal spans.
    return tuple(label["evidence_ids"]), tuple(label["contradicting_evidence_ids"])


def _structured_label(label: dict) -> tuple[object, ...]:
    # Free-text reasoning is compared by presence; its wording is reviewed at adjudication.
    return (
        label["expected_status"],
        label["target"],
        label["aspect"],
        label["horizon"],
        label["direction"],
        label["intensity_band"],
        label["evidence_judgment"],
        _evidence_signature(label),
        label["mechanism"] is not None,
        bool(label["assumptions"]),
        label["uncertainty_reason"] is not None,
        tuple(label["phenomena"]),
    )


def _component_value(label: dict, component: str) -> object:
    if component == "evidence":
        return _evidence_signature(label)
    if component == "mechanism_presence":
        return label["mechanism"] is not None
    if component == "phenomena":
        return tuple(label["phenomena"])
    if component == "structured_label":
        return _structured_label(label)
    if component == "full_label":
        return label
    return label[component]


def measure_impact_agreement(
    dataset_path: Path | str,
    *,
    reviewer_a: str,
    reviewer_b: str,
    split: str | None = None,
) -> ImpactAgreementReport:
    reviewer_a = _require_text(reviewer_a, "reviewer_a", "impact agreement").strip()
    reviewer_b = _require_text(reviewer_b, "reviewer_b", "impact agreement").strip()
    if reviewer_a == reviewer_b:
        raise EvaluationDatasetError("impact agreement requires two distinct reviewers")
    if split is not None and split not in ALLOWED_SPLITS:
        raise EvaluationDatasetError("impact agreement requires a valid split")

    dataset = validate_impact_evaluation_dataset(dataset_path)
    rows = _load_cases(Path(dataset_path) / "cases.jsonl")
    if split is not None:
        rows = [case for case in rows if case["split"] == split]
    confusions = {field: empty_confusion(labels) for field, labels in CATEGORICAL_FIELDS.items()}
    component_matches = Counter({name: 0 for name in COMPONENTS})
    split_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    paired = 0
    for case in rows:
        annotation = case["annotation"]
        if annotation["state"] not in {"single_annotator", "adjudicated"}:
            continue
        reviews = annotation.get("reviews", [])
        if len(reviews) != 2:
            continue
        by_reviewer = {review["reviewer_id"]: review for review in reviews}
        if set(by_reviewer) != {reviewer_a, reviewer_b}:
            continue
        # Agreement uses the original opinions, never an adjudicated final label.
        first = by_reviewer[reviewer_a]["labels"]["impact"][0]
        second = by_reviewer[reviewer_b]["labels"]["impact"][0]
        for field, confusion in confusions.items():
            confusion[first[field]][second[field]] += 1
        for component in COMPONENTS:
            if _component_value(first, component) == _component_value(second, component):
                component_matches[component] += 1
        paired += 1
        split_counts[case["split"]] += 1
        language_counts[case["language"]] += 1
        source_counts[case["source_kind"]] += 1

    categorical = {
        field: categorical_agreement(confusion, paired) for field, confusion in confusions.items()
    }
    warnings: list[str] = []
    if paired:
        for field, result in categorical.items():
            if result.cohen_kappa is None:
                warnings.append(
                    f"{field} kappa is undefined when both reviewers use one identical class"
                )
    else:
        warnings.append("no cases have two reviews by this exact reviewer pair")
    if paired < len(rows):
        warnings.append("reviewer pair does not cover the selected dataset scope")
    if paired < MINIMUM_GATE_PAIRS:
        warnings.append("fewer than 30 paired cases; do not use this estimate as a quality gate")
    if not dataset.publishable_impact_gold:
        warnings.append(
            "dataset is not publishable impact gold; this is an annotation-process metric"
        )
    components = {
        name: ComponentAgreement(
            matches=component_matches[name],
            total=paired,
            agreement=component_matches[name] / paired if paired else None,
        )
        for name in COMPONENTS
    }
    gate_kappa = categorical[GATE_FIELD].cohen_kappa
    return ImpactAgreementReport(
        report_version=REPORT_VERSION,
        dataset_version=dataset.dataset_version,
        scope_split=split or "all",
        reviewer_a=reviewer_a,
        reviewer_b=reviewer_b,
        dataset_cases=len(rows),
        paired_cases=paired,
        paired_by_split=dict(sorted(split_counts.items())),
        paired_by_language=dict(sorted(language_counts.items())),
        paired_by_source_kind=dict(sorted(source_counts.items())),
        categorical=categorical,
        kappa_target=KAPPA_TARGET,
        direction_kappa_passed=(
            paired >= MINIMUM_GATE_PAIRS
            and gate_kappa is not None
            and gate_kappa >= KAPPA_TARGET
        ),
        components=components,
        quality_gate_eligible=(
            paired >= MINIMUM_GATE_PAIRS
            and gate_kappa is not None
            and dataset.publishable_impact_gold
            and split == "test"
        ),
        warnings=tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only paired human impact agreement")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--reviewer-a", required=True)
    parser.add_argument("--reviewer-b", required=True)
    parser.add_argument("--split", choices=sorted(ALLOWED_SPLITS))
    args = parser.parse_args()
    report = measure_impact_agreement(
        args.dataset,
        reviewer_a=args.reviewer_a,
        reviewer_b=args.reviewer_b,
        split=args.split,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
