import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_temperature_calibration import fit_and_evaluate_temperature_calibration


DATASET = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"
LABELS = ("mixed", "negative", "neutral", "positive", "unknown")


class ToneTemperatureCalibrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cases = [json.loads(line) for line in (DATASET / "cases.jsonl").read_text().splitlines()]
        self.dev_run, self.dev_input = self.write_bundle("dev", "tone-dev-v1", confidence=0.8)
        self.test_run, self.test_input = self.write_bundle("test", "tone-test-v1", confidence=0.8)

    def write_bundle(
        self,
        split,
        run_id,
        *,
        confidence,
        method_id="fixture",
        method_version="v1",
        null_last=False,
    ):
        folder = self.root / f"{split}-{run_id}"
        folder.mkdir()
        selected = [case for case in self.cases if case["split"] == split]
        predictions = []
        probability_rows = []
        other_probability = (1.0 - confidence) / (len(LABELS) - 1)
        for index, case in enumerate(selected):
            gold = case["annotation"]["labels"]["tone"]["polarity"]
            probabilities = {label: other_probability for label in LABELS}
            probabilities[gold] = confidence
            is_null = null_last and index == len(selected) - 1
            predictions.append({
                "case_id": case["case_id"],
                "content_sha256": case["content_sha256"],
                "predicted_polarity": "__abstain__" if is_null else gold,
                "raw_confidence": None if is_null else confidence,
            })
            probability_rows.append({
                "case_id": case["case_id"],
                "content_sha256": case["content_sha256"],
                "probabilities": None if is_null else probabilities,
            })
        prediction_path = folder / "predictions.jsonl"
        prediction_path.write_text("".join(json.dumps(row) + "\n" for row in predictions))
        config_hash = hashlib.sha256(b"same-method-config").hexdigest()
        run = {
            "schema_version": "tone-prediction-run-v1",
            "prediction_run_id": run_id,
            "dataset_version": "tone-contract-v1",
            "task": "tone.polarity",
            "task_contract": "infohub.tone-evaluation/1.0",
            "output_schema_version": "infohub.tone/1.1",
            "vocabulary_version": "tone-vocabulary-v1",
            "split": split,
            "method_id": method_id,
            "method_version": method_version,
            "method_config_sha256": config_hash,
            "generated_at": "2026-09-28T21:00:00Z",
            "predictions_file": "predictions.jsonl",
            "dataset_manifest_sha256": hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256((DATASET / "cases.jsonl").read_bytes()).hexdigest(),
            "predictions_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
            "abstain_label": "__abstain__",
        }
        run_path = folder / "run.json"
        run_path.write_text(json.dumps(run))
        probability_path = folder / "probabilities.jsonl"
        probability_path.write_text("".join(json.dumps(row) + "\n" for row in probability_rows))
        calibration_input = {
            "schema_version": "tone-calibration-input-v1",
            "calibration_evaluation_id": f"calibration-{run_id}",
            "prediction_run_id": run_id,
            "dataset_version": "tone-contract-v1",
            "split": split,
            "generated_at": "2026-09-28T21:01:00Z",
            "bin_count": 10,
            "prediction_run_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
            "predictions_sha256": run["predictions_sha256"],
            "dataset_manifest_sha256": run["dataset_manifest_sha256"],
            "dataset_cases_sha256": run["dataset_cases_sha256"],
            "probabilities_file": "probabilities.jsonl",
            "probabilities_sha256": hashlib.sha256(probability_path.read_bytes()).hexdigest(),
        }
        input_path = folder / "calibration.json"
        input_path.write_text(json.dumps(calibration_input))
        return run_path, input_path

    def evaluate(self, **paths):
        return fit_and_evaluate_temperature_calibration(
            DATASET,
            paths.get("fit_run", self.dev_run),
            paths.get("fit_input", self.dev_input),
            paths.get("test_run", self.test_run),
            paths.get("test_input", self.test_input),
        )

    def test_fits_on_dev_and_evaluates_once_on_test(self):
        report = self.evaluate()
        self.assertEqual(report.fit_prediction_run_id, "tone-dev-v1")
        self.assertEqual(report.test_prediction_run_id, "tone-test-v1")
        self.assertEqual((report.measurements.fit_cases, report.measurements.test_cases), (4, 6))
        self.assertLess(report.measurements.dev_nll_after, report.measurements.dev_nll_before)
        self.assertLess(report.measurements.test_brier_after, report.measurements.test_brier_before)
        self.assertLess(report.measurements.test_ece_after, report.measurements.test_ece_before)
        self.assertEqual(sum(item["count"] for item in report.reliability_bins), 6)
        self.assertFalse(report.checks["fit_support_at_least_200"])
        self.assertFalse(report.admission_ready)

    def test_mapping_version_is_deterministic_and_excludes_test_input(self):
        first = self.evaluate()
        second_test_run, second_test_input = self.write_bundle(
            "test", "tone-test-v2", confidence=0.7
        )
        second = self.evaluate(test_run=second_test_run, test_input=second_test_input)
        self.assertEqual(first.calibration_version, second.calibration_version)
        self.assertNotEqual(first.test_input_sha256, second.test_input_sha256)
        self.assertNotEqual(first.measurements.test_brier_before, second.measurements.test_brier_before)

    def test_requires_dev_fit_and_independent_test(self):
        other_test_run, other_test_input = self.write_bundle(
            "test", "tone-test-fit", confidence=0.8
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "requires dev fit"):
            self.evaluate(fit_run=other_test_run, fit_input=other_test_input)
        duplicate_dev_run, duplicate_dev_input = self.write_bundle(
            "dev", "tone-test-v1", confidence=0.8
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "requires distinct"):
            self.evaluate(fit_run=duplicate_dev_run, fit_input=duplicate_dev_input)

    def test_requires_same_model_identity_and_complete_probabilities(self):
        different_run, different_input = self.write_bundle(
            "test", "tone-test-different", confidence=0.8, method_version="v2"
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "identity differs"):
            self.evaluate(test_run=different_run, test_input=different_input)
        partial_run, partial_input = self.write_bundle(
            "dev", "tone-dev-partial", confidence=0.8, null_last=True
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "complete probabilities"):
            self.evaluate(fit_run=partial_run, fit_input=partial_input)


if __name__ == "__main__":
    unittest.main()
