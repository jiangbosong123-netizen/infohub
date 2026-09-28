from __future__ import annotations

"""Compare frozen tone baseline and candidate runs on the same evaluation split."""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_json
from .tone_evaluation_metrics import ToneClassificationReport, evaluate_tone_classification

COMPARISON_VERSION = "tone-model-comparison-v1"
MAX_REGRESSION = 0.02
MIN_MACRO_F1 = 0.80
MIN_UNKNOWN_RECALL = 0.90


@dataclass(frozen=True)
class ToneModelComparison:
    comparison_version: str
    dataset_version: str
    split: str
    baseline_run_id: str
    candidate_run_id: str
    baseline_method: str
    candidate_method: str
    measurements: dict[str, float | int | None]
    per_class_recall_delta: dict[str, float | None]
    slice_macro_f1_delta: dict[str, dict[str, float]]
    checks: dict[str, bool]
    requires_regression_review: bool
    comparison_eligible: bool
    candidate_gate_passed: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _delta(candidate: float | None, baseline: float | None) -> float | None:
    return None if candidate is None or baseline is None else candidate - baseline


def assess_tone_model_comparison(
    baseline: ToneClassificationReport,
    candidate: ToneClassificationReport,
    *,
    baseline_method: str,
    candidate_method: str,
) -> ToneModelComparison:
    if not baseline_method.strip() or not candidate_method.strip():
        raise EvaluationDatasetError("tone comparison requires named methods")
    if baseline.prediction_run_id == candidate.prediction_run_id:
        raise EvaluationDatasetError("tone comparison requires distinct prediction runs")
    if (
        baseline.dataset_version != candidate.dataset_version
        or baseline.split != candidate.split
        or baseline.total != candidate.total
    ):
        raise EvaluationDatasetError("tone comparison requires the same dataset, split and denominator")
    baseline_classes = {item.label: item for item in baseline.per_class}
    candidate_classes = {item.label: item for item in candidate.per_class}
    if set(baseline_classes) != set(candidate_classes) or any(
        baseline_classes[label].support != candidate_classes[label].support
        for label in baseline_classes
    ):
        raise EvaluationDatasetError("tone comparison class support differs")
    recall_delta = {
        label: _delta(candidate_classes[label].recall, baseline_classes[label].recall)
        for label in sorted(baseline_classes)
    }
    slice_delta: dict[str, dict[str, float]] = {}
    if set(baseline.slices) != set(candidate.slices):
        raise EvaluationDatasetError("tone comparison slice dimensions differ")
    for dimension in baseline.slices:
        if set(baseline.slices[dimension]) != set(candidate.slices[dimension]):
            raise EvaluationDatasetError("tone comparison slice members differ")
        slice_delta[dimension] = {}
        for value, base_slice in baseline.slices[dimension].items():
            current = candidate.slices[dimension][value]
            if current.total != base_slice.total:
                raise EvaluationDatasetError("tone comparison slice support differs")
            slice_delta[dimension][value] = current.macro_f1 - base_slice.macro_f1

    macro_delta = candidate.macro_f1 - baseline.macro_f1
    unknown_delta = _delta(candidate.unknown_recall, baseline.unknown_recall)
    coverage_delta = candidate.coverage - baseline.coverage
    regressions = [macro_delta, coverage_delta]
    regressions.extend(value for value in recall_delta.values() if value is not None)
    regressions.extend(value for group in slice_delta.values() for value in group.values())
    if unknown_delta is not None:
        regressions.append(unknown_delta)
    requires_review = any(value < -MAX_REGRESSION for value in regressions)
    checks = {
        "blind_test_split": baseline.split == "test",
        "baseline_quality_claim_allowed": baseline.quality_claim_allowed,
        "candidate_quality_claim_allowed": candidate.quality_claim_allowed,
        "complete_baseline_predictions": baseline.missing_predictions == 0,
        "complete_candidate_predictions": candidate.missing_predictions == 0,
        "candidate_macro_f1_at_least_0_80": candidate.macro_f1 >= MIN_MACRO_F1,
        "candidate_unknown_recall_at_least_0_90": (
            candidate.unknown_recall is not None
            and candidate.unknown_recall >= MIN_UNKNOWN_RECALL
        ),
        "no_metric_regression_over_0_02": not requires_review,
    }
    eligible = all(
        checks[key]
        for key in (
            "blind_test_split",
            "baseline_quality_claim_allowed",
            "candidate_quality_claim_allowed",
            "complete_baseline_predictions",
            "complete_candidate_predictions",
        )
    )
    warnings = []
    if requires_review:
        warnings.append("candidate regresses by more than 0.02; explanation and explicit baseline review are required")
    if not eligible:
        warnings.append("comparison is engineering-only until both runs use publishable private blind-test gold")
    if not all(checks.values()):
        warnings.append("candidate gate remains blocked")
    return ToneModelComparison(
        comparison_version=COMPARISON_VERSION,
        dataset_version=baseline.dataset_version,
        split=baseline.split,
        baseline_run_id=baseline.prediction_run_id,
        candidate_run_id=candidate.prediction_run_id,
        baseline_method=baseline_method,
        candidate_method=candidate_method,
        measurements={
            "cases": baseline.total,
            "baseline_macro_f1": baseline.macro_f1,
            "candidate_macro_f1": candidate.macro_f1,
            "macro_f1_delta": macro_delta,
            "baseline_unknown_recall": baseline.unknown_recall,
            "candidate_unknown_recall": candidate.unknown_recall,
            "unknown_recall_delta": unknown_delta,
            "baseline_coverage": baseline.coverage,
            "candidate_coverage": candidate.coverage,
            "coverage_delta": coverage_delta,
        },
        per_class_recall_delta=recall_delta,
        slice_macro_f1_delta=slice_delta,
        checks=checks,
        requires_regression_review=requires_review,
        comparison_eligible=eligible,
        candidate_gate_passed=eligible and all(checks.values()),
        warnings=tuple(warnings),
    )


def compare_tone_runs(
    dataset_path: Path | str,
    baseline_run_path: Path | str,
    candidate_run_path: Path | str,
) -> ToneModelComparison:
    baseline_run = _load_json(Path(baseline_run_path))
    candidate_run = _load_json(Path(candidate_run_path))
    baseline = evaluate_tone_classification(dataset_path, baseline_run_path)
    candidate = evaluate_tone_classification(dataset_path, candidate_run_path)
    return assess_tone_model_comparison(
        baseline,
        candidate,
        baseline_method=f"{baseline_run['method_id']}@{baseline_run['method_version']}",
        candidate_method=f"{candidate_run['method_id']}@{candidate_run['method_version']}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare frozen tone baseline and candidate runs")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--baseline-run", required=True, type=Path)
    parser.add_argument("--candidate-run", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare_tone_runs(args.dataset, args.baseline_run, args.candidate_run)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if report.candidate_gate_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
