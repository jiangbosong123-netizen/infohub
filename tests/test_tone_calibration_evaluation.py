import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_calibration_evaluation import evaluate_tone_calibration
from app.tone_evaluation_metrics import ABSTAIN


DATASET = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"
LABELS = ("mixed", "negative", "neutral", "positive", "unknown")


class ToneCalibrationEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cases = [json.loads(line) for line in (DATASET / "cases.jsonl").read_text().splitlines()]
        self.cases = [case for case in cases if case["split"] == "test"]
        self.predictions = []
        self.probabilities = []
        for case in self.cases:
            polarity = case["annotation"]["labels"]["tone"]["polarity"]
            vector = {label: 0.05 for label in LABELS}
            vector[polarity] = 0.8
            self.predictions.append({
                "case_id": case["case_id"], "content_sha256": case["content_sha256"],
                "predicted_polarity": polarity, "raw_confidence": 0.8,
            })
            self.probabilities.append({
                "case_id": case["case_id"], "content_sha256": case["content_sha256"],
                "probabilities": vector,
            })
        self.run_path = self.write_run()
        self.input_path = self.write_input()

    def write_run(self, rows=None):
        rows = self.predictions if rows is None else rows
        predictions = self.root / "predictions.jsonl"
        predictions.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = {
            "schema_version": "tone-prediction-run-v1", "prediction_run_id": "tone-run-v1",
            "dataset_version": "tone-contract-v1", "task": "tone.polarity",
            "task_contract": "infohub.tone-evaluation/1.0",
            "output_schema_version": "infohub.tone/1.1", "vocabulary_version": "tone-vocabulary-v1",
            "split": "test", "method_id": "fixture", "method_version": "v1",
            "method_config_sha256": hashlib.sha256(b"fixture").hexdigest(),
            "generated_at": "2026-09-28T20:00:00Z", "predictions_file": "predictions.jsonl",
            "dataset_manifest_sha256": hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256((DATASET / "cases.jsonl").read_bytes()).hexdigest(),
            "predictions_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
            "abstain_label": ABSTAIN,
        }
        path = self.root / "run.json"
        path.write_text(json.dumps(run))
        return path

    def write_input(self, rows=None, **changes):
        rows = self.probabilities if rows is None else rows
        probabilities = self.root / "probabilities.jsonl"
        probabilities.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = json.loads(self.run_path.read_text())
        record = {
            "schema_version": "tone-calibration-input-v1",
            "calibration_evaluation_id": "calibration-fixture-v1",
            "prediction_run_id": "tone-run-v1", "dataset_version": "tone-contract-v1",
            "split": "test", "generated_at": "2026-09-28T20:05:00Z", "bin_count": 10,
            "prediction_run_sha256": hashlib.sha256(self.run_path.read_bytes()).hexdigest(),
            "predictions_sha256": run["predictions_sha256"],
            "dataset_manifest_sha256": hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256((DATASET / "cases.jsonl").read_bytes()).hexdigest(),
            "probabilities_file": "probabilities.jsonl",
            "probabilities_sha256": hashlib.sha256(probabilities.read_bytes()).hexdigest(),
        }
        record.update(changes)
        path = self.root / "calibration.json"
        path.write_text(json.dumps(record))
        return path

    def test_multiclass_brier_ece_and_bins_are_deterministic(self):
        report = evaluate_tone_calibration(DATASET, self.run_path, self.input_path)
        self.assertEqual((report.total, report.scored), (6, 6))
        self.assertAlmostEqual(report.multiclass_brier_score, 0.05)
        self.assertAlmostEqual(report.expected_calibration_error, 0.2)
        self.assertEqual(len(report.reliability_bins), 10)
        self.assertEqual(report.reliability_bins[8].count, 6)
        self.assertFalse(report.calibration_claim_allowed)
        self.assertFalse(report.calibration_admission_ready)

    def test_abstention_is_retained_as_unscored_denominator(self):
        rows = json.loads(json.dumps(self.predictions))
        rows[-1]["predicted_polarity"] = ABSTAIN
        rows[-1]["raw_confidence"] = None
        self.write_run(rows)
        probability_rows = json.loads(json.dumps(self.probabilities))
        probability_rows[-1]["probabilities"] = None
        self.write_input(probability_rows)
        report = evaluate_tone_calibration(DATASET, self.run_path, self.input_path)
        self.assertEqual((report.scored, report.abstained_or_missing), (5, 1))
        self.assertAlmostEqual(report.probability_coverage, 5 / 6)
        self.assertFalse(report.checks["complete_probability_coverage"])

    def test_probability_vector_argmax_and_raw_confidence_must_agree(self):
        cases = [
            ({"positive": 0.9}, "probabilities are invalid"),
            ({"positive": math.nan}, "probabilities are invalid"),
        ]
        for update, message in cases:
            with self.subTest(update=update):
                rows = json.loads(json.dumps(self.probabilities))
                rows[0]["probabilities"].update(update)
                self.write_input(rows)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    evaluate_tone_calibration(DATASET, self.run_path, self.input_path)
        rows = json.loads(json.dumps(self.probabilities))
        gold = self.predictions[0]["predicted_polarity"]
        other = next(label for label in LABELS if label != gold)
        rows[0]["probabilities"] = {label: 1 / 30 for label in LABELS}
        rows[0]["probabilities"][gold] = 0.1
        rows[0]["probabilities"][other] = 0.8
        self.write_input(rows)
        with self.assertRaisesRegex(EvaluationDatasetError, "argmax differs"):
            evaluate_tone_calibration(DATASET, self.run_path, self.input_path)
        predictions = json.loads(json.dumps(self.predictions))
        predictions[0]["raw_confidence"] = 0.7
        self.write_run(predictions)
        self.write_input()
        with self.assertRaisesRegex(EvaluationDatasetError, "confidence differs"):
            evaluate_tone_calibration(DATASET, self.run_path, self.input_path)

    def test_exact_coverage_and_hash_binding_are_required(self):
        self.write_input(self.probabilities[:-1])
        with self.assertRaisesRegex(EvaluationDatasetError, "cover every split case"):
            evaluate_tone_calibration(DATASET, self.run_path, self.input_path)
        self.write_input(probabilities_sha256="0" * 64)
        with self.assertRaisesRegex(EvaluationDatasetError, "probabilities_sha256 mismatch"):
            evaluate_tone_calibration(DATASET, self.run_path, self.input_path)
        self.write_input(prediction_run_sha256="0" * 64)
        with self.assertRaisesRegex(EvaluationDatasetError, "differs from frozen run"):
            evaluate_tone_calibration(DATASET, self.run_path, self.input_path)


if __name__ == "__main__":
    unittest.main()
