import dataclasses
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.evaluation_agreement import AgreementReport
from app.evaluation_metrics import ClassMetrics, ClassificationReport
from app.evaluation_release_gate import (
    assess_relevance_release, evaluate_relevance_release,
)
from tests import test_evaluation_metrics as metric_fixtures


def synthetic_reports():
    classes = (
        ClassMetrics("not_relevant", 35, 35, 35, 1.0, 1.0, 1.0),
        ClassMetrics("relevant", 40, 40, 39, 0.975, 0.975, 0.975),
        ClassMetrics("unknown", 35, 35, 35, 1.0, 1.0, 1.0),
    )
    metrics = ClassificationReport(
        "synthetic", "synthetic-dataset", "synthetic-run", "relevance", "test",
        110, 109, 109 / 110, (0.9, 1.0), 110, 1.0, 0, 0,
        0.99, ("not_relevant", "relevant", "unknown"),
        ("not_relevant", "relevant", "unknown"), classes, {}, True, (),
    )
    agreement = AgreementReport(
        "synthetic", "synthetic-dataset", "test", "fixture-a", "fixture-b",
        110, 110, {"test": 110}, {"en": 55, "zh": 55}, {}, 0.9, 0.5, 0.8, (),
    )
    return metrics, agreement


class RelevanceReleaseGateTests(unittest.TestCase):
    def test_all_required_checks_can_pass_on_synthetic_reports_only(self):
        metrics, agreement = synthetic_reports()
        decision = assess_relevance_release(metrics, agreement)
        self.assertTrue(decision.passed)
        self.assertTrue(all(decision.checks.values()))
        self.assertIn("human verification", " ".join(decision.warnings))

    def test_train_agreement_cannot_mask_blind_test_disagreement(self):
        metrics, agreement = synthetic_reports()
        agreement = dataclasses.replace(agreement, scope_split="all")
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)
        agreement = dataclasses.replace(agreement, scope_split="test", cohen_kappa=0.69)
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)

    def test_missing_class_or_prediction_blocks_claim(self):
        metrics, agreement = synthetic_reports()
        metrics = dataclasses.replace(metrics, per_class=metrics.per_class[:2])
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)
        metrics, agreement = synthetic_reports()
        metrics = dataclasses.replace(metrics, missing_predictions=1)
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)
        self.assertFalse(assess_relevance_release(metrics, agreement).checks["complete_predictions"])

    def test_low_support_recall_or_wrong_dataset_blocks_claim(self):
        metrics, agreement = synthetic_reports()
        low_support = dataclasses.replace(metrics.per_class[0], support=29)
        metrics = dataclasses.replace(metrics,
                                      per_class=(low_support,) + metrics.per_class[1:])
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)
        metrics, agreement = synthetic_reports()
        low_recall = dataclasses.replace(metrics.per_class[1], recall=0.94)
        metrics = dataclasses.replace(metrics,
                                      per_class=(metrics.per_class[0], low_recall, metrics.per_class[2]))
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)
        metrics, agreement = synthetic_reports()
        agreement = dataclasses.replace(agreement, dataset_version="different")
        self.assertFalse(assess_relevance_release(metrics, agreement).passed)

    def test_real_entry_requires_relevance_path_and_never_passes_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            run = metric_fixtures.EvaluationMetricTests().v2_run(Path(td), "test")
            decision = evaluate_relevance_release(
                metric_fixtures.DATA, run, reviewer_a="fixture-a", reviewer_b="fixture-b")
            self.assertFalse(decision.passed)
            self.assertFalse(decision.checks["verified_blind_test_gold"])
            content = run.read_text().replace('"label_path": "relevance"',
                                              '"label_path": "tone"')
            run.write_text(content)
            with self.assertRaisesRegex(EvaluationDatasetError, "relevance label path"):
                evaluate_relevance_release(
                    metric_fixtures.DATA, run, reviewer_a="fixture-a", reviewer_b="fixture-b")


if __name__ == "__main__":
    unittest.main()
