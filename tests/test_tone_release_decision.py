import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_release_admission import assess_tone_release_evidence
from app.tone_release_decision import record_tone_release_decision
from tests.test_tone_release_admission import hashes, reports


class ToneReleaseDecisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle_path = self.write_bundle()
        self.registry_path = self.write_registry()
        self.decision_path = self.write_decision()

    def write_bundle(self, *, ready=True):
        values = list(reports())
        if not ready:
            values[0].publishable_tone_gold = False
        bundle = assess_tone_release_evidence(*values, artifact_sha256=hashes())
        path = self.root / "bundle.json"
        path.write_text(json.dumps(bundle.to_dict(), sort_keys=True))
        return path

    def write_registry(self, **approver_changes):
        approver = {
            "approver_id": "release-owner",
            "status": "active",
            "scopes": ["tone:release:approve"],
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": None,
        }
        approver.update(approver_changes)
        registry = {
            "schema_version": "tone-release-approver-registry-v1",
            "registry_version": "tone-approvers-v1",
            "generated_at": "2026-09-28T23:00:00Z",
            "approvers": [approver],
        }
        path = self.root / "registry.json"
        path.write_text(json.dumps(registry, sort_keys=True))
        return path

    def write_decision(self, **changes):
        bundle = json.loads(self.bundle_path.read_text())
        registry = json.loads(self.registry_path.read_text())
        decision = {
            "schema_version": "tone-release-decision-v1",
            "decision_id": "tone-decision-v1",
            "bundle_id": bundle["bundle_id"],
            "bundle_sha256": hashlib.sha256(self.bundle_path.read_bytes()).hexdigest(),
            "registry_version": registry["registry_version"],
            "registry_sha256": hashlib.sha256(self.registry_path.read_bytes()).hexdigest(),
            "approver_id": "release-owner",
            "decision": "approve",
            "reason": "All frozen evidence and rollback steps were independently reviewed.",
            "recorded_at": "2026-09-29T00:00:00Z",
            "source": "human",
            "model_assistance": False,
            "blind_holdout_exclusion_acknowledged": True,
            "budget_policy_id": bundle["measurements"]["policy_id"],
            "rollback_plan_acknowledged": True,
        }
        decision.update(changes)
        path = self.root / "decision.json"
        path.write_text(json.dumps(decision, sort_keys=True))
        return path

    def test_authorized_independent_approval_becomes_import_candidate(self):
        record = record_tone_release_decision(
            self.bundle_path, self.registry_path, self.decision_path
        )
        self.assertTrue(record.approval_candidate_for_controlled_import)
        self.assertEqual(record.approver_id, "release-owner")
        self.assertEqual(record.policy_id, "approved-budget-v1")
        repeated = record_tone_release_decision(
            self.bundle_path, self.registry_path, self.decision_path
        )
        self.assertEqual(record.record_id, repeated.record_id)

    def test_rejection_can_record_an_incomplete_bundle_without_approving_it(self):
        self.bundle_path = self.write_bundle(ready=False)
        self.decision_path = self.write_decision(
            decision="reject",
            reason="Evidence gates remain incomplete.",
            blind_holdout_exclusion_acknowledged=False,
            rollback_plan_acknowledged=False,
        )
        record = record_tone_release_decision(
            self.bundle_path, self.registry_path, self.decision_path
        )
        self.assertEqual(record.decision, "reject")
        self.assertFalse(record.approval_candidate_for_controlled_import)

    def test_approval_requires_ready_bundle_and_acknowledgements(self):
        self.bundle_path = self.write_bundle(ready=False)
        self.decision_path = self.write_decision()
        with self.assertRaisesRegex(EvaluationDatasetError, "cannot approve incomplete"):
            record_tone_release_decision(self.bundle_path, self.registry_path, self.decision_path)
        self.bundle_path = self.write_bundle(ready=True)
        self.decision_path = self.write_decision(rollback_plan_acknowledged=False)
        with self.assertRaisesRegex(EvaluationDatasetError, "requires holdout and rollback"):
            record_tone_release_decision(self.bundle_path, self.registry_path, self.decision_path)

    def test_registry_scope_status_time_and_independence_are_enforced(self):
        cases = (
            ({"status": "revoked"}, "not authorized"),
            ({"scopes": ["tone:release:read"]}, "not authorized"),
            ({"valid_from": "2026-10-01T00:00:00Z"}, "not authorized"),
            ({"valid_until": "2026-09-28T23:30:00Z"}, "not authorized"),
            ({"approver_id": "reviewer-a"}, "independent"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                self.registry_path = self.write_registry(**changes)
                self.decision_path = self.write_decision(approver_id=changes.get("approver_id", "release-owner"))
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    record_tone_release_decision(
                        self.bundle_path, self.registry_path, self.decision_path
                    )

    def test_bundle_registry_policy_and_content_tampering_fail_closed(self):
        cases = (
            ({"bundle_sha256": "0" * 64}, "bundle binding differs"),
            ({"registry_sha256": "0" * 64}, "registry binding differs"),
            ({"budget_policy_id": "other-policy"}, "budget policy differs"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                path = self.write_decision(**changes)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    record_tone_release_decision(self.bundle_path, self.registry_path, path)
        bundle = json.loads(self.bundle_path.read_text())
        bundle["measurements"]["candidate_macro_f1"] = 1.0
        self.bundle_path.write_text(json.dumps(bundle, sort_keys=True))
        self.decision_path = self.write_decision()
        with self.assertRaisesRegex(EvaluationDatasetError, "bundle_id mismatch"):
            record_tone_release_decision(self.bundle_path, self.registry_path, self.decision_path)

    def test_rehashed_but_invalid_measurements_are_rejected(self):
        bundle = json.loads(self.bundle_path.read_text())
        bundle["measurements"]["calibrated_test_ece"] = "excellent"
        self.bundle_path.write_text(json.dumps(bundle, sort_keys=True))
        self.decision_path = self.write_decision()
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid tone release evidence measurements"):
            record_tone_release_decision(self.bundle_path, self.registry_path, self.decision_path)


if __name__ == "__main__":
    unittest.main()
