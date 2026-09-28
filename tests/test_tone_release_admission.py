import copy
import unittest
from types import SimpleNamespace

from app.evaluation import EvaluationDatasetError
from app.tone_release_admission import ARTIFACT_KEYS, assess_tone_release_evidence


def reports():
    dataset = SimpleNamespace(dataset_version="tone-private-v1", publishable_tone_gold=True)
    agreement = SimpleNamespace(
        dataset_version="tone-private-v1",
        scope_split="test",
        reviewer_a="reviewer-a",
        reviewer_b="reviewer-b",
        dataset_cases=240,
        paired_cases=240,
        paired_by_split={"test": 240},
        polarity_cohen_kappa=0.82,
        polarity_kappa_passed=True,
        quality_gate_eligible=True,
    )
    comparison = SimpleNamespace(
        dataset_version="tone-private-v1",
        baseline_run_id="baseline-test-v1",
        candidate_run_id="candidate-test-v1",
        measurements={
            "cases": 240,
            "candidate_macro_f1": 0.86,
            "candidate_unknown_recall": 0.93,
        },
        candidate_gate_passed=True,
        requires_regression_review=False,
    )
    calibration = SimpleNamespace(
        dataset_version="tone-private-v1",
        fit_prediction_run_id="candidate-dev-v1",
        test_prediction_run_id="candidate-test-v1",
        calibration_version="tone-temperature-good",
        measurements=SimpleNamespace(
            test_cases=240,
            test_ece_after=0.07,
            test_brier_after=0.18,
        ),
        admission_ready=True,
    )
    operations = SimpleNamespace(
        dataset_version="tone-private-v1",
        prediction_run_id="candidate-test-v1",
        operational_run_id="candidate-ops-v1",
        cases=240,
        policy_id="approved-budget-v1",
        first_attempt_schema_rate=0.995,
        final_schema_rate=1.0,
        cost_microusd_per_100_cases=50_000.0,
        latency_ms_p95=2_500,
        admission_ready=True,
    )
    security = SimpleNamespace(
        dataset_version="tone-private-v1",
        test_prediction_run_id="candidate-test-v1",
        security_prediction_run_id="candidate-security-v1",
        security_cases=60,
        passed=True,
    )
    return dataset, agreement, comparison, calibration, operations, security


def hashes():
    return {name: f"{index + 1:064x}" for index, name in enumerate(sorted(ARTIFACT_KEYS))}


class ToneReleaseAdmissionTests(unittest.TestCase):
    def test_complete_recomputed_evidence_can_be_ready_for_human_review(self):
        bundle = assess_tone_release_evidence(*reports(), artifact_sha256=hashes())
        self.assertTrue(bundle.evidence_ready_for_human_review)
        self.assertTrue(all(bundle.checks.values()))
        self.assertEqual(bundle.candidate_test_run_id, "candidate-test-v1")
        self.assertEqual(bundle.measurements["test_cases"], 240)
        self.assertIn("not approval", " ".join(bundle.warnings))

    def test_any_failed_gate_blocks_bundle_readiness(self):
        names = (
            (0, "publishable_tone_gold"),
            (1, "quality_gate_eligible"),
            (1, "polarity_kappa_passed"),
            (2, "candidate_gate_passed"),
            (3, "admission_ready"),
            (4, "admission_ready"),
            (5, "passed"),
        )
        for index, field in names:
            with self.subTest(index=index, field=field):
                values = list(copy.deepcopy(reports()))
                setattr(values[index], field, False)
                bundle = assess_tone_release_evidence(*values, artifact_sha256=hashes())
                self.assertFalse(bundle.evidence_ready_for_human_review)

    def test_dataset_candidate_and_denominator_mismatches_fail_closed(self):
        values = list(reports())
        values[5].dataset_version = "other"
        with self.assertRaisesRegex(EvaluationDatasetError, "dataset versions differ"):
            assess_tone_release_evidence(*values, artifact_sha256=hashes())
        values = list(reports())
        values[4].prediction_run_id = "other-run"
        with self.assertRaisesRegex(EvaluationDatasetError, "candidate test runs differ"):
            assess_tone_release_evidence(*values, artifact_sha256=hashes())
        values = list(reports())
        values[3].measurements.test_cases = 239
        with self.assertRaisesRegex(EvaluationDatasetError, "denominators differ"):
            assess_tone_release_evidence(*values, artifact_sha256=hashes())

    def test_bundle_id_is_deterministic_and_hash_bound(self):
        first = assess_tone_release_evidence(*reports(), artifact_sha256=hashes())
        second = assess_tone_release_evidence(*reports(), artifact_sha256=hashes())
        self.assertEqual(first.bundle_id, second.bundle_id)
        changed = hashes()
        changed["candidate_test_operations"] = "f" * 64
        third = assess_tone_release_evidence(*reports(), artifact_sha256=changed)
        self.assertNotEqual(first.bundle_id, third.bundle_id)
        invalid = hashes()
        invalid.pop("dataset_cases")
        with self.assertRaisesRegex(EvaluationDatasetError, "exact artifact hashes"):
            assess_tone_release_evidence(*reports(), artifact_sha256=invalid)
        nonhex = hashes()
        nonhex["dataset_cases"] = "z" * 64
        with self.assertRaisesRegex(EvaluationDatasetError, "exact artifact hashes"):
            assess_tone_release_evidence(*reports(), artifact_sha256=nonhex)


if __name__ == "__main__":
    unittest.main()
