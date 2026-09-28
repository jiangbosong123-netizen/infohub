from __future__ import annotations

"""Read-only agreement metrics for a fixed pair of independent tone reviewers."""

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import ALLOWED_SPLITS, EvaluationDatasetError, _load_cases, _require_text
from .tone_contracts import POLARITIES
from .tone_evaluation import validate_tone_evaluation_dataset

REPORT_VERSION = "tone-review-agreement-v1"
POLARITY_KAPPA_TARGET = 0.70
LABELS = tuple(sorted(POLARITIES))
COMPONENTS = (
    "speaker",
    "target",
    "aspect",
    "polarity",
    "intensity_band",
    "evidence",
    "phenomena",
    "full_label",
)


@dataclass(frozen=True)
class ComponentAgreement:
    matches: int
    total: int
    agreement: float | None


@dataclass(frozen=True)
class ToneAgreementReport:
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
    polarity_confusion: dict[str, dict[str, int]]
    polarity_observed_agreement: float | None
    polarity_expected_agreement: float | None
    polarity_cohen_kappa: float | None
    polarity_kappa_target: float
    polarity_kappa_passed: bool
    components: dict[str, ComponentAgreement]
    quality_gate_eligible: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _evidence_signature(label: dict) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                span["quote_sha256"],
                span["start_offset"],
                span["end_offset"],
                span["offset_unit"],
            )
            for span in label["evidence"]
        )
    )


def _component_value(label: dict, component: str) -> object:
    if component == "evidence":
        return _evidence_signature(label)
    if component == "phenomena":
        return tuple(label["phenomena"])
    if component == "full_label":
        return label
    return label[component]


def measure_tone_agreement(
    dataset_path: Path | str,
    *,
    reviewer_a: str,
    reviewer_b: str,
    split: str | None = None,
) -> ToneAgreementReport:
    reviewer_a = _require_text(reviewer_a, "reviewer_a", "tone agreement").strip()
    reviewer_b = _require_text(reviewer_b, "reviewer_b", "tone agreement").strip()
    if reviewer_a == reviewer_b:
        raise EvaluationDatasetError("tone agreement requires two distinct reviewers")
    if split is not None and split not in ALLOWED_SPLITS:
        raise EvaluationDatasetError("tone agreement requires a valid split")

    dataset = validate_tone_evaluation_dataset(dataset_path)
    rows = _load_cases(Path(dataset_path) / "cases.jsonl")
    if split is not None:
        rows = [case for case in rows if case["split"] == split]
    confusion = {first: {second: 0 for second in LABELS} for first in LABELS}
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
        first = by_reviewer[reviewer_a]["labels"]["tone"]
        second = by_reviewer[reviewer_b]["labels"]["tone"]
        confusion[first["polarity"]][second["polarity"]] += 1
        for component in COMPONENTS:
            if _component_value(first, component) == _component_value(second, component):
                component_matches[component] += 1
        paired += 1
        split_counts[case["split"]] += 1
        language_counts[case["language"]] += 1
        source_counts[case["source_kind"]] += 1

    warnings: list[str] = []
    observed: float | None = None
    expected: float | None = None
    kappa: float | None = None
    if paired:
        observed = sum(confusion[label][label] for label in LABELS) / paired
        expected = sum(
            sum(confusion[label].values())
            * sum(confusion[first][label] for first in LABELS)
            for label in LABELS
        ) / (paired * paired)
        if expected < 1:
            kappa = (observed - expected) / (1 - expected)
        else:
            warnings.append("polarity kappa is undefined when both reviewers use one identical class")
    else:
        warnings.append("no cases have two reviews by this exact reviewer pair")
    if paired < len(rows):
        warnings.append("reviewer pair does not cover the selected dataset scope")
    if paired < 30:
        warnings.append("fewer than 30 paired cases; do not use this estimate as a quality gate")
    if not dataset.publishable_tone_gold:
        warnings.append("dataset is not publishable tone gold; this is an annotation-process metric")
    components = {
        name: ComponentAgreement(
            matches=component_matches[name],
            total=paired,
            agreement=component_matches[name] / paired if paired else None,
        )
        for name in COMPONENTS
    }
    eligible = (
        paired >= 30
        and kappa is not None
        and dataset.publishable_tone_gold
        and split == "test"
    )
    return ToneAgreementReport(
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
        polarity_confusion=confusion,
        polarity_observed_agreement=observed,
        polarity_expected_agreement=expected,
        polarity_cohen_kappa=kappa,
        polarity_kappa_target=POLARITY_KAPPA_TARGET,
        polarity_kappa_passed=(
            paired >= 30 and kappa is not None and kappa >= POLARITY_KAPPA_TARGET
        ),
        components=components,
        quality_gate_eligible=eligible,
        warnings=tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only paired human tone agreement")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--reviewer-a", required=True)
    parser.add_argument("--reviewer-b", required=True)
    parser.add_argument("--split", choices=sorted(ALLOWED_SPLITS))
    args = parser.parse_args()
    report = measure_tone_agreement(
        args.dataset,
        reviewer_a=args.reviewer_a,
        reviewer_b=args.reviewer_b,
        split=args.split,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
