from __future__ import annotations

"""Conservative read-only release gate for relevance on a blind test split."""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_json
from .evaluation_agreement import AgreementReport, measure_relevance_agreement
from .evaluation_metrics import ClassificationReport, evaluate_classification
from .evaluation_review_intake import RELEVANCE_LABELS

GATE_VERSION = "relevance-release-gate-v1"
MIN_CLASS_SUPPORT = 30
MIN_KAPPA = 0.70
MIN_MACRO_F1 = 0.85
MIN_RELEVANT_RECALL = 0.95


@dataclass(frozen=True)
class RelevanceReleaseDecision:
    gate_version: str
    dataset_version: str
    prediction_run_id: str
    reviewer_a: str
    reviewer_b: str
    passed: bool
    checks: dict[str, bool]
    measurements: dict[str, float | int | None]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def assess_relevance_release(metrics: ClassificationReport,
                             agreement: AgreementReport) -> RelevanceReleaseDecision:
    """Assess frozen reports; passing does not replace human source/identity verification."""
    classes = {item.label: item for item in metrics.per_class}
    relevant = classes.get("relevant")
    kappa = agreement.cohen_kappa
    checks = {
        "matching_dataset_version": metrics.dataset_version == agreement.dataset_version,
        "verified_blind_test_gold": metrics.task == "relevance" and metrics.split == "test"
        and metrics.quality_claim_allowed,
        "test_only_agreement": agreement.scope_split == "test",
        "all_test_cases_double_reviewed_by_pair": agreement.paired_cases == metrics.total
        and agreement.dataset_cases == metrics.total
        and agreement.paired_by_split.get("test") == metrics.total,
        "complete_predictions": metrics.missing_predictions == 0,
        "label_support_at_least_30_each": set(classes) == RELEVANCE_LABELS
        and all(item.support >= MIN_CLASS_SUPPORT for item in classes.values()),
        "independent_review_kappa_at_least_0_70": kappa is not None and kappa >= MIN_KAPPA,
        "macro_f1_at_least_0_85": metrics.macro_f1 >= MIN_MACRO_F1,
        "relevant_recall_at_least_0_95": relevant is not None
        and relevant.recall is not None and relevant.recall >= MIN_RELEVANT_RECALL,
    }
    warnings = []
    if not all(checks.values()):
        warnings.append("release gate blocked; failed checks require review and a new frozen report")
    warnings.append("human verification of reviewer identity, independence and blind-test exclusion remains required")
    return RelevanceReleaseDecision(
        GATE_VERSION, metrics.dataset_version, metrics.prediction_run_id,
        agreement.reviewer_a, agreement.reviewer_b,
        all(checks.values()), checks,
        {"test_cases": metrics.total, "paired_test_cases": agreement.paired_cases,
         "cohen_kappa": kappa, "macro_f1": metrics.macro_f1,
         "relevant_recall": relevant.recall if relevant is not None else None,
         "missing_predictions": metrics.missing_predictions,
         "abstained": metrics.abstained}, tuple(warnings),
    )


def evaluate_relevance_release(dataset_path: Path | str, prediction_run_path: Path | str,
                               *, reviewer_a: str, reviewer_b: str) -> RelevanceReleaseDecision:
    run = _load_json(Path(prediction_run_path))
    if not isinstance(run, dict) or run.get("task") != "relevance" or run.get("label_path") != "relevance":
        raise EvaluationDatasetError("relevance release requires the relevance label path")
    metrics = evaluate_classification(dataset_path, prediction_run_path)
    agreement = measure_relevance_agreement(
        dataset_path, reviewer_a=reviewer_a, reviewer_b=reviewer_b, split="test")
    return assess_relevance_release(metrics, agreement)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only relevance blind-test release gate")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--reviewer-a", required=True)
    parser.add_argument("--reviewer-b", required=True)
    args = parser.parse_args()
    decision = evaluate_relevance_release(
        args.dataset, args.run, reviewer_a=args.reviewer_a, reviewer_b=args.reviewer_b)
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0 if decision.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
