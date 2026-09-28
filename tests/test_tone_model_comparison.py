import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation_metrics import ABSTAIN, evaluate_tone_classification
from app.tone_model_comparison import assess_tone_model_comparison, compare_tone_runs


DATASET = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneModelComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cases = [json.loads(line) for line in (DATASET / "cases.jsonl").read_text().splitlines()]
        self.cases = [case for case in cases if case["split"] == "test"]
        baseline = [self.row(case) for case in self.cases]
        baseline[-1]["predicted_polarity"] = ABSTAIN
        baseline[-1]["raw_confidence"] = None
        self.baseline_path = self.create_run("baseline", baseline)
        self.candidate_path = self.create_run("candidate", [self.row(case) for case in self.cases])

    @staticmethod
    def row(case):
        return {
            "case_id": case["case_id"],
            "content_sha256": case["content_sha256"],
            "predicted_polarity": case["annotation"]["labels"]["tone"]["polarity"],
            "raw_confidence": 0.8,
        }

    def create_run(self, name, rows):
        root = self.root / name
        root.mkdir()
        predictions = root / "predictions.jsonl"
        predictions.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = {
            "schema_version": "tone-prediction-run-v1",
            "prediction_run_id": f"{name}-run-v1",
            "dataset_version": "tone-contract-v1",
            "task": "tone.polarity",
            "task_contract": "infohub.tone-evaluation/1.0",
            "output_schema_version": "infohub.tone/1.1",
            "vocabulary_version": "tone-vocabulary-v1",
            "split": "test",
            "method_id": name,
            "method_version": "v1",
            "method_config_sha256": hashlib.sha256(name.encode()).hexdigest(),
            "generated_at": "2026-09-28T19:00:00Z",
            "predictions_file": "predictions.jsonl",
            "dataset_manifest_sha256": hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256((DATASET / "cases.jsonl").read_bytes()).hexdigest(),
            "predictions_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
            "abstain_label": ABSTAIN,
        }
        path = root / "run.json"
        path.write_text(json.dumps(run))
        return path

    def test_same_frozen_split_reports_absolute_and_relative_metrics(self):
        report = compare_tone_runs(DATASET, self.baseline_path, self.candidate_path)
        self.assertEqual(report.measurements["cases"], 6)
        self.assertGreater(report.measurements["macro_f1_delta"], 0)
        self.assertGreater(report.measurements["unknown_recall_delta"], 0)
        self.assertGreater(report.measurements["coverage_delta"], 0)
        self.assertEqual(report.baseline_method, "baseline@v1")
        self.assertEqual(report.candidate_method, "candidate@v1")
        self.assertFalse(report.comparison_eligible)
        self.assertFalse(report.candidate_gate_passed)

    def test_eligible_reports_can_pass_absolute_and_regression_checks(self):
        baseline = evaluate_tone_classification(DATASET, self.baseline_path)
        candidate = evaluate_tone_classification(DATASET, self.candidate_path)
        baseline = replace(baseline, quality_claim_allowed=True, macro_f1=0.82, unknown_recall=0.90)
        candidate = replace(candidate, quality_claim_allowed=True, macro_f1=0.83, unknown_recall=0.95)
        report = assess_tone_model_comparison(
            baseline, candidate, baseline_method="rules@v1", candidate_method="model@v2"
        )
        self.assertTrue(report.comparison_eligible)
        self.assertTrue(report.candidate_gate_passed)
        self.assertFalse(report.requires_regression_review)

    def test_more_than_two_point_regression_requires_review(self):
        baseline = evaluate_tone_classification(DATASET, self.candidate_path)
        candidate = replace(
            baseline,
            prediction_run_id="candidate-worse",
            quality_claim_allowed=True,
            macro_f1=baseline.macro_f1 - 0.03,
        )
        baseline = replace(baseline, quality_claim_allowed=True)
        report = assess_tone_model_comparison(
            baseline, candidate, baseline_method="base@v1", candidate_method="new@v1"
        )
        self.assertTrue(report.requires_regression_review)
        self.assertFalse(report.checks["no_metric_regression_over_0_02"])
        self.assertFalse(report.candidate_gate_passed)

    def test_distinct_runs_and_identical_denominators_are_required(self):
        baseline = evaluate_tone_classification(DATASET, self.baseline_path)
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct prediction runs"):
            assess_tone_model_comparison(
                baseline, baseline, baseline_method="base", candidate_method="same"
            )
        candidate = evaluate_tone_classification(DATASET, self.candidate_path)
        candidate = replace(candidate, dataset_version="other")
        with self.assertRaisesRegex(EvaluationDatasetError, "same dataset"):
            assess_tone_model_comparison(
                baseline, candidate, baseline_method="base", candidate_method="new"
            )


if __name__ == "__main__":
    unittest.main()
