import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation_metrics import ToneSliceMetrics, evaluate_tone_classification
from app.tone_security_gate import assess_tone_security, evaluate_tone_security


DATASET = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneSecurityGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cases = [json.loads(line) for line in (DATASET / "cases.jsonl").read_text().splitlines()]
        test_cases = [case for case in cases if case["split"] == "test"]
        security_cases = [case for case in cases if case["split"] == "security"]
        test_rows = [{
            "case_id": case["case_id"],
            "content_sha256": case["content_sha256"],
            "predicted_polarity": case["annotation"]["labels"]["tone"]["polarity"],
            "raw_confidence": 0.8,
        } for case in test_cases]
        security_rows = [{
            "case_id": case["case_id"],
            "content_sha256": case["content_sha256"],
            "predicted_polarity": case["annotation"]["labels"]["tone"]["polarity"],
            "raw_confidence": 0.8,
        } for case in security_cases]
        self.test_run = self.write_run("test", "tone-test-v1", test_rows)
        self.security_run = self.write_run("security", "tone-security-v1", security_rows)

    def write_run(self, split, run_id, rows, **changes):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        predictions = folder / "predictions.jsonl"
        predictions.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = {
            "schema_version": "tone-prediction-run-v1",
            "prediction_run_id": run_id,
            "dataset_version": "tone-contract-v1",
            "task": "tone.polarity",
            "task_contract": "infohub.tone-evaluation/1.0",
            "output_schema_version": "infohub.tone/1.1",
            "vocabulary_version": "tone-vocabulary-v1",
            "split": split,
            "method_id": "fixture",
            "method_version": "v1",
            "method_config_sha256": hashlib.sha256(b"fixture").hexdigest(),
            "generated_at": "2026-09-28T23:30:00Z",
            "predictions_file": "predictions.jsonl",
            "dataset_manifest_sha256": hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256((DATASET / "cases.jsonl").read_bytes()).hexdigest(),
            "predictions_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
            "abstain_label": "__abstain__",
        }
        run.update(changes)
        path = folder / "run.json"
        path.write_text(json.dumps(run))
        return path

    def test_fixture_is_exact_but_cannot_claim_real_security(self):
        decision = evaluate_tone_security(DATASET, self.test_run, self.security_run)
        self.assertEqual((decision.security_cases, decision.security_correct), (2, 2))
        self.assertEqual(decision.phenomenon_support["prompt_injection"], 2)
        self.assertTrue(decision.checks["zero_security_errors"])
        self.assertFalse(decision.checks["security_support_at_least_50"])
        self.assertFalse(decision.checks["publishable_private_gold"])
        self.assertFalse(decision.passed)

    def test_any_error_or_abstention_blocks_security(self):
        test = evaluate_tone_classification(DATASET, self.test_run)
        security = dataclasses.replace(
            test,
            prediction_run_id="security",
            split="security",
            total=50,
            correct=49,
            abstained=1,
            missing_predictions=0,
        )
        decision = assess_tone_security(
            dataclasses.replace(test, quality_claim_allowed=True),
            security,
            same_method_identity=True,
            publishable_private_gold=True,
        )
        self.assertFalse(decision.checks["zero_security_errors"])
        self.assertFalse(decision.checks["no_security_abstentions"])
        self.assertFalse(decision.passed)

    def test_all_explicit_security_checks_can_pass_on_synthetic_reports(self):
        test = dataclasses.replace(
            evaluate_tone_classification(DATASET, self.test_run),
            quality_claim_allowed=True,
        )
        perfect_slice = ToneSliceMetrics(50, 50, 50, 1.0, 1.0, 50, 1.0, False)
        security = dataclasses.replace(
            test,
            prediction_run_id="security",
            split="security",
            total=50,
            correct=50,
            covered=50,
            abstained=0,
            missing_predictions=0,
            slices={"phenomenon": {"prompt_injection": perfect_slice}},
        )
        decision = assess_tone_security(
            test,
            security,
            same_method_identity=True,
            publishable_private_gold=True,
        )
        self.assertTrue(decision.passed)
        self.assertTrue(all(decision.checks.values()))

    def test_method_identity_and_run_ids_cannot_be_reused(self):
        security_rows = [
            json.loads(line)
            for line in self.security_run.with_name("predictions.jsonl").read_text().splitlines()
        ]
        different = self.write_run(
            "security", "tone-security-different",
            security_rows,
            method_version="different",
        )
        decision = evaluate_tone_security(DATASET, self.test_run, different)
        self.assertFalse(decision.checks["same_candidate_method_identity"])
        duplicate = self.write_run(
            "security",
            json.loads(self.test_run.read_text())["prediction_run_id"],
            security_rows,
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "requires distinct"):
            evaluate_tone_security(DATASET, self.test_run, duplicate)


if __name__ == "__main__":
    unittest.main()
