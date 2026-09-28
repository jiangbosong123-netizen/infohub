import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_operational_evaluation import evaluate_tone_operations


DATASET = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneOperationalEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cases = [json.loads(line) for line in (DATASET / "cases.jsonl").read_text().splitlines()]
        self.cases = [case for case in cases if case["split"] == "test"]
        predictions = []
        for case in self.cases:
            predictions.append({
                "case_id": case["case_id"],
                "content_sha256": case["content_sha256"],
                "predicted_polarity": case["annotation"]["labels"]["tone"]["polarity"],
                "raw_confidence": 0.8,
            })
        predictions_path = self.root / "predictions.jsonl"
        predictions_path.write_text("".join(json.dumps(row) + "\n" for row in predictions))
        self.run = {
            "schema_version": "tone-prediction-run-v1",
            "prediction_run_id": "tone-test-v1",
            "dataset_version": "tone-contract-v1",
            "task": "tone.polarity",
            "task_contract": "infohub.tone-evaluation/1.0",
            "output_schema_version": "infohub.tone/1.1",
            "vocabulary_version": "tone-vocabulary-v1",
            "split": "test",
            "method_id": "fixture",
            "method_version": "v1",
            "method_config_sha256": hashlib.sha256(b"fixture").hexdigest(),
            "generated_at": "2026-09-28T22:00:00Z",
            "predictions_file": "predictions.jsonl",
            "dataset_manifest_sha256": hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest(),
            "dataset_cases_sha256": hashlib.sha256((DATASET / "cases.jsonl").read_bytes()).hexdigest(),
            "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
            "abstain_label": "__abstain__",
        }
        self.run_path = self.root / "run.json"
        self.run_path.write_text(json.dumps(self.run))
        self.rows = [self.attempt(case["case_id"], 1) for case in self.cases]
        self.operations_path = self.write_operations(self.rows)

    @staticmethod
    def attempt(
        case_id,
        number,
        *,
        kind="primary",
        status="succeeded",
        schema_valid=True,
        usage_status="reported",
        started="2026-09-28T22:01:00.000Z",
        finished="2026-09-28T22:01:00.100Z",
    ):
        known = usage_status != "unknown"
        return {
            "case_id": case_id,
            "attempt_number": number,
            "attempt_kind": kind,
            "status": status,
            "started_at": started,
            "finished_at": finished,
            "schema_valid": schema_valid,
            "usage_status": usage_status,
            "input_tokens": 20 if known else None,
            "output_tokens": 5 if known else None,
            "cost_microusd": 100 if known else None,
        }

    def write_operations(self, rows, **changes):
        attempts = self.root / "attempts.jsonl"
        attempts.write_text("".join(json.dumps(row) + "\n" for row in rows))
        record = {
            "schema_version": "tone-operational-run-v1",
            "operational_run_id": "tone-ops-v1",
            "prediction_run_id": "tone-test-v1",
            "dataset_version": "tone-contract-v1",
            "split": "test",
            "generated_at": "2026-09-28T22:05:00Z",
            "prediction_run_sha256": hashlib.sha256(self.run_path.read_bytes()).hexdigest(),
            "predictions_sha256": self.run["predictions_sha256"],
            "dataset_manifest_sha256": self.run["dataset_manifest_sha256"],
            "dataset_cases_sha256": self.run["dataset_cases_sha256"],
            "attempts_file": "attempts.jsonl",
            "attempts_sha256": hashlib.sha256(attempts.read_bytes()).hexdigest(),
            "policy_id": "tone-budget-v1",
            "max_cost_microusd_per_1000_input_tokens": 6000,
            "max_cost_microusd_per_100_cases": 12000,
            "max_p95_latency_ms": 1000,
        }
        record.update(changes)
        path = self.root / "operations.json"
        path.write_text(json.dumps(record))
        return path

    def test_reports_all_attempt_cost_latency_and_schema_rates(self):
        rows = json.loads(json.dumps(self.rows))
        rows[0].update(schema_valid=False)
        rows.append(self.attempt(
            self.cases[0]["case_id"],
            2,
            kind="repair",
            started="2026-09-28T22:01:01.000Z",
            finished="2026-09-28T22:01:01.300Z",
        ))
        rows[1].update(status="failed", schema_valid=False)
        rows.append(self.attempt(
            self.cases[1]["case_id"],
            2,
            kind="retry",
            started="2026-09-28T22:01:01.000Z",
            finished="2026-09-28T22:01:01.200Z",
        ))
        path = self.write_operations(rows)
        report = evaluate_tone_operations(DATASET, self.run_path, path)
        self.assertEqual((report.cases, report.attempts, report.failed_attempts), (6, 8, 1))
        self.assertEqual(report.recovered_cases, 2)
        self.assertEqual(report.first_attempt_schema_valid, 4)
        self.assertEqual(report.final_schema_valid, 6)
        self.assertAlmostEqual(report.first_attempt_schema_rate, 4 / 6)
        self.assertEqual((report.latency_ms_p50, report.latency_ms_p95), (100, 300))
        self.assertEqual((report.total_input_tokens, report.total_cost_microusd), (160, 800))
        self.assertFalse(report.checks["first_attempt_schema_valid_rate_at_least_0_99"])
        self.assertFalse(report.admission_ready)

    def test_clean_fixture_meets_operations_but_not_private_gold_gate(self):
        report = evaluate_tone_operations(DATASET, self.run_path, self.operations_path)
        self.assertTrue(report.checks["final_schema_valid_rate_is_1_00"])
        self.assertTrue(report.checks["first_attempt_schema_valid_rate_at_least_0_99"])
        self.assertTrue(report.checks["cost_per_1000_input_within_policy"])
        self.assertTrue(report.checks["cost_per_100_cases_within_policy"])
        self.assertFalse(report.checks["publishable_gold_and_quality_run"])
        self.assertFalse(report.admission_ready)

    def test_unknown_usage_stays_in_attempt_denominator_and_blocks_gate(self):
        rows = json.loads(json.dumps(self.rows))
        rows[0] = self.attempt(self.cases[0]["case_id"], 1, usage_status="unknown")
        report = evaluate_tone_operations(DATASET, self.run_path, self.write_operations(rows))
        self.assertEqual(report.attempts, 6)
        self.assertEqual(report.unknown_usage_attempts, 1)
        self.assertEqual(report.total_input_tokens, 100)
        self.assertFalse(report.checks["all_usage_known"])

    def test_all_unknown_usage_emits_null_cost_rate_not_infinity(self):
        rows = [
            self.attempt(case["case_id"], 1, usage_status="unknown")
            for case in self.cases
        ]
        report = evaluate_tone_operations(DATASET, self.run_path, self.write_operations(rows))
        self.assertIsNone(report.cost_microusd_per_1000_input_tokens)
        self.assertFalse(report.checks["input_tokens_nonzero"])
        self.assertFalse(report.checks["cost_per_1000_input_within_policy"])
        self.assertNotIn("Infinity", json.dumps(report.to_dict(), allow_nan=False))

    def test_rejects_bad_sequences_fields_timestamps_and_hashes(self):
        cases = []
        rows = json.loads(json.dumps(self.rows))
        rows[0]["attempt_number"] = 2
        cases.append((rows, {}, "attempt sequence is invalid"))
        rows = json.loads(json.dumps(self.rows))
        rows[0].update(status="failed", schema_valid=True)
        cases.append((rows, {}, "failed tone attempt cannot be schema valid"))
        rows = json.loads(json.dumps(self.rows))
        rows[0]["finished_at"] = "2026-09-28T22:00:59.999Z"
        cases.append((rows, {}, "latency must be nonnegative"))
        rows = json.loads(json.dumps(self.rows))
        rows[0].update(status="failed", schema_valid=False)
        rows.append(self.attempt(
            self.cases[0]["case_id"], 2, kind="repair",
            started="2026-09-28T22:01:01.000Z", finished="2026-09-28T22:01:01.100Z",
        ))
        cases.append((rows, {}, "retry or repair kind is invalid"))
        rows = json.loads(json.dumps(self.rows))
        rows[0].update(schema_valid=False)
        rows.append(self.attempt(
            self.cases[0]["case_id"], 2, kind="repair",
            started="2026-09-28T22:00:00.000Z", finished="2026-09-28T22:00:00.100Z",
        ))
        cases.append((rows, {}, "overlap or run backwards"))
        cases.append((self.rows, {"attempts_sha256": "0" * 64}, "attempts_sha256 mismatch"))
        for rows, changes, message in cases:
            with self.subTest(message=message):
                path = self.write_operations(rows, **changes)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    evaluate_tone_operations(DATASET, self.run_path, path)


if __name__ == "__main__":
    unittest.main()
