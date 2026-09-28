import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation_metrics import ABSTAIN, evaluate_tone_classification


DATASET = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneEvaluationMetricsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cases = [json.loads(line) for line in (DATASET / "cases.jsonl").read_text().splitlines()]
        self.selected = [case for case in self.cases if case["split"] == "test"]
        self.predictions = []
        for case in self.selected:
            polarity = case["annotation"]["labels"]["tone"]["polarity"]
            self.predictions.append(
                {
                    "case_id": case["case_id"],
                    "content_sha256": case["content_sha256"],
                    "predicted_polarity": polarity,
                    "raw_confidence": 0.8,
                }
            )
        self.predictions[-1]["predicted_polarity"] = ABSTAIN
        self.predictions[-1]["raw_confidence"] = None
        self.run_path = self.write_run()

    def write_run(self, *, rows=None, **changes):
        rows = self.predictions if rows is None else rows
        predictions_path = self.root / "predictions.jsonl"
        predictions_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        run = {
            "schema_version": "tone-prediction-run-v1",
            "prediction_run_id": "tone-fixture-run-v1",
            "dataset_version": "tone-contract-v1",
            "task": "tone.polarity",
            "task_contract": "infohub.tone-evaluation/1.0",
            "output_schema_version": "infohub.tone/1.1",
            "vocabulary_version": "tone-vocabulary-v1",
            "split": "test",
            "method_id": "fixture-method",
            "method_version": "v1",
            "method_config_sha256": hashlib.sha256(b"fixture config").hexdigest(),
            "generated_at": "2026-09-28T18:00:00Z",
            "predictions_file": "predictions.jsonl",
            "dataset_manifest_sha256": hashlib.sha256(
                (DATASET / "manifest.json").read_bytes()
            ).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256(
                (DATASET / "cases.jsonl").read_bytes()
            ).hexdigest(),
            "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
            "abstain_label": ABSTAIN,
        }
        run.update(changes)
        path = self.root / "run.json"
        path.write_text(json.dumps(run), encoding="utf-8")
        return path

    def test_reports_fixed_polarity_metrics_coverage_and_slices(self):
        report = evaluate_tone_classification(DATASET, self.run_path)
        self.assertEqual((report.total, report.correct, report.covered), (6, 5, 5))
        self.assertEqual((report.abstained, report.missing_predictions), (1, 0))
        self.assertAlmostEqual(report.coverage, 5 / 6)
        self.assertAlmostEqual(report.unknown_recall, 0.5)
        self.assertAlmostEqual(report.macro_f1, 11 / 15)
        self.assertEqual(set(report.confusion), {"negative", "neutral", "positive", "mixed", "unknown"})
        self.assertEqual(report.confusion["unknown"][ABSTAIN], 1)
        self.assertEqual(set(report.slices), {"language", "source_kind", "phenomenon"})
        self.assertEqual(report.slices["language"]["en"].total, 3)
        self.assertTrue(report.slices["phenomenon"]["sarcasm"].low_support)
        self.assertFalse(report.quality_claim_allowed)

    def test_missing_prediction_is_an_abstention_in_the_denominator(self):
        self.write_run(rows=self.predictions[:-1])
        report = evaluate_tone_classification(DATASET, self.root / "run.json")
        self.assertEqual(report.missing_predictions, 1)
        self.assertEqual(report.abstained, 1)
        self.assertEqual(report.total, 6)

    def test_hash_content_and_split_binding_fail_closed(self):
        cases = [
            ({"predictions_sha256": "0" * 64}, "hash binding"),
            ({"dataset_cases_sha256": "0" * 64}, "hash binding"),
            ({"split": "train"}, "requires dev, test or security"),
            ({"task": "relevance"}, "contract differs"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                self.write_run(**changes)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    evaluate_tone_classification(DATASET, self.root / "run.json")
        rows = json.loads(json.dumps(self.predictions))
        rows[0]["content_sha256"] = "0" * 64
        self.write_run(rows=rows)
        with self.assertRaisesRegex(EvaluationDatasetError, "content hash differs"):
            evaluate_tone_classification(DATASET, self.root / "run.json")

    def test_rows_are_closed_and_confidence_is_finite_probability(self):
        invalid = [
            ({"raw_confidence": math.nan}, "confidence is invalid"),
            ({"raw_confidence": 1.1}, "confidence is invalid"),
            ({"raw_confidence": True}, "confidence is invalid"),
            ({"predicted_polarity": "bullish"}, "polarity is invalid"),
            ({"extra": "leak"}, "invalid fields"),
        ]
        for update, message in invalid:
            with self.subTest(update=update):
                rows = json.loads(json.dumps(self.predictions))
                rows[0].update(update)
                self.write_run(rows=rows)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    evaluate_tone_classification(DATASET, self.root / "run.json")

    def test_duplicate_unknown_and_other_split_cases_are_rejected(self):
        duplicate = self.predictions + [self.predictions[0]]
        self.write_run(rows=duplicate)
        with self.assertRaisesRegex(EvaluationDatasetError, "duplicate, unknown or in another split"):
            evaluate_tone_classification(DATASET, self.root / "run.json")
        other = json.loads(json.dumps(self.predictions))
        other[0]["case_id"] = self.cases[0]["case_id"]
        other[0]["content_sha256"] = self.cases[0]["content_sha256"]
        self.write_run(rows=other)
        with self.assertRaisesRegex(EvaluationDatasetError, "duplicate, unknown or in another split"):
            evaluate_tone_classification(DATASET, self.root / "run.json")


if __name__ == "__main__":
    unittest.main()
