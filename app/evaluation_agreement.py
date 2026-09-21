from __future__ import annotations

"""Read-only agreement report for a named pair of independent relevance reviewers."""

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_cases, _require_text, validate_evaluation_dataset
from .evaluation_review_intake import RELEVANCE_LABELS

REPORT_VERSION = "relevance-agreement-v1"
LABELS = tuple(sorted(RELEVANCE_LABELS))


@dataclass(frozen=True)
class AgreementReport:
    report_version: str
    dataset_version: str
    reviewer_a: str
    reviewer_b: str
    dataset_cases: int
    paired_cases: int
    paired_by_split: dict[str, int]
    paired_by_language: dict[str, int]
    confusion: dict[str, dict[str, int]]
    observed_agreement: float | None
    expected_agreement: float | None
    cohen_kappa: float | None
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def measure_relevance_agreement(dataset_path: Path | str, *, reviewer_a: str,
                                reviewer_b: str) -> AgreementReport:
    """Measure a fixed pair; never compare adjudicated gold with a provisional review."""
    reviewer_a = _require_text(reviewer_a, "reviewer_a", "agreement").strip()
    reviewer_b = _require_text(reviewer_b, "reviewer_b", "agreement").strip()
    if reviewer_a == reviewer_b:
        raise EvaluationDatasetError("agreement requires two distinct reviewers")

    dataset = validate_evaluation_dataset(dataset_path)
    rows = _load_cases(Path(dataset_path) / "cases.jsonl")
    confusion = {first: {second: 0 for second in LABELS} for first in LABELS}
    split_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    for case in rows:
        annotation = case["annotation"]
        if annotation["state"] not in {"single_annotator", "adjudicated"}:
            continue
        reviews = annotation["reviews"]
        if len(reviews) != 2:
            continue
        by_reviewer = {review["reviewer_id"]: review for review in reviews}
        if set(by_reviewer) != {reviewer_a, reviewer_b}:
            continue
        first = by_reviewer[reviewer_a]["labels"]
        second = by_reviewer[reviewer_b]["labels"]
        if first not in ({"relevance": label} for label in LABELS) or second not in (
                {"relevance": label} for label in LABELS):
            raise EvaluationDatasetError(f"case {case['case_id']} has invalid relevance reviews")
        confusion[first["relevance"]][second["relevance"]] += 1
        split_counts[case["split"]] += 1
        language_counts[case["language"]] += 1

    total = sum(sum(row.values()) for row in confusion.values())
    warnings: list[str] = []
    observed: float | None = None
    expected: float | None = None
    kappa: float | None = None
    if total:
        observed = sum(confusion[label][label] for label in LABELS) / total
        expected = sum(
            sum(confusion[label].values())
            * sum(confusion[first][label] for first in LABELS)
            for label in LABELS
        ) / (total * total)
        if expected < 1:
            kappa = (observed - expected) / (1 - expected)
        else:
            warnings.append("kappa is undefined when both reviewers use only one identical label")
    else:
        warnings.append("no cases have two reviews by this exact reviewer pair")
    if total < dataset.cases:
        warnings.append("reviewer pair does not cover the complete dataset")
    if not dataset.publishable_gold:
        warnings.append("dataset is not publishable gold; this is an annotation-process metric only")
    if total < 30:
        warnings.append("fewer than 30 paired cases; do not use this estimate as a quality gate")

    return AgreementReport(
        REPORT_VERSION, dataset.dataset_version, reviewer_a, reviewer_b,
        dataset.cases, total, dict(sorted(split_counts.items())),
        dict(sorted(language_counts.items())), confusion, observed, expected,
        kappa, tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only paired human relevance agreement")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--reviewer-a", required=True)
    parser.add_argument("--reviewer-b", required=True)
    args = parser.parse_args()
    report = measure_relevance_agreement(
        args.dataset, reviewer_a=args.reviewer_a, reviewer_b=args.reviewer_b)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
