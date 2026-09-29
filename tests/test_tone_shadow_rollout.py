import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.tone_release_admission import assess_tone_release_evidence
from app.tone_release_decision import record_tone_release_decision
from app.tone_release_import import import_tone_release_admission
from app.tone_shadow_rollout import (
    ToneShadowRolloutError,
    create_tone_shadow_rollout,
    transition_tone_shadow_rollout,
)
from tests.test_tone_release_admission import hashes, reports


T0 = "2026-09-29T02:00:00.000000Z"


class ToneShadowRolloutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        bundle = assess_tone_release_evidence(*reports(), artifact_sha256=hashes())
        self.bundle = self.root / "bundle.json"
        self.bundle.write_text(json.dumps(bundle.to_dict(), sort_keys=True))
        registry = {
            "schema_version": "tone-release-approver-registry-v1",
            "registry_version": "tone-approvers-v1",
            "generated_at": "2026-09-28T23:00:00Z",
            "approvers": [{
                "approver_id": "release-owner", "status": "active",
                "scopes": ["tone:release:approve"],
                "valid_from": "2026-09-01T00:00:00Z", "valid_until": None,
            }],
        }
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps(registry, sort_keys=True))
        decision = {
            "schema_version": "tone-release-decision-v1",
            "decision_id": "tone-shadow-decision-v1",
            "bundle_id": bundle.bundle_id,
            "bundle_sha256": hashlib.sha256(self.bundle.read_bytes()).hexdigest(),
            "registry_version": registry["registry_version"],
            "registry_sha256": hashlib.sha256(self.registry.read_bytes()).hexdigest(),
            "approver_id": "release-owner", "decision": "approve",
            "reason": "Approved for a bounded shadow-only rollout.",
            "recorded_at": "2026-09-29T00:00:00Z", "source": "human",
            "model_assistance": False,
            "blind_holdout_exclusion_acknowledged": True,
            "budget_policy_id": bundle.measurements["policy_id"],
            "rollback_plan_acknowledged": True,
        }
        self.decision = self.root / "decision.json"
        self.decision.write_text(json.dumps(decision, sort_keys=True))
        record = record_tone_release_decision(
            self.bundle, self.registry, self.decision
        )
        self.record = self.root / "record.json"
        self.record.write_text(json.dumps(record.to_dict(), sort_keys=True))
        with database.get_db() as db:
            admission = import_tone_release_admission(
                db, self.bundle, self.registry, self.decision, self.record,
                imported_by="release-operator", now=T0,
            )
        self.admission_id = admission.admission_id
        self.rollout_config = {
            "mode": "shadow_only",
            "minimum_observations": 200,
            "observation_window_hours": 168,
            "maximum_error_bps": 100,
            "maximum_disagreement_bps": 500,
        }

    def create(self):
        with database.get_db() as db:
            return create_tone_shadow_rollout(
                db, admission_id=self.admission_id, sample_bps=500,
                config=self.rollout_config, created_by="rollout-owner",
                reason="Start with a bounded five percent shadow sample.", now=T0,
            )

    def test_approved_admission_creates_idempotent_planned_rollout(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first, second)
        self.assertEqual((first.state, first.transition_version), ("planned", 1))
        with database.get_db() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tone_shadow_rollouts"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM analysis_publications"
            ).fetchone()[0], 0)

    def test_config_is_bounded_and_cannot_enable_serving(self):
        cases = (
            ({**self.rollout_config, "mode": "serve"}, "shadow_only"),
            ({**self.rollout_config, "minimum_observations": 0}, "minimum_observations"),
            ({**self.rollout_config, "maximum_error_bps": 10001}, "maximum_error_bps"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with database.get_db() as db, self.assertRaisesRegex(
                    ToneShadowRolloutError, message
                ):
                    create_tone_shadow_rollout(
                        db, admission_id=self.admission_id, sample_bps=500,
                        config=value, created_by="owner", reason="Invalid fixture.", now=T0,
                    )

    def test_state_machine_is_append_only_and_stale_safe(self):
        planned = self.create()
        with database.get_db() as db:
            running = transition_tone_shadow_rollout(
                db, rollout_id=planned.rollout_id, to_state="running",
                expected_previous_transition_id=planned.transition_id,
                actor="operator", reason="Begin observation only.", now=T0,
            )
            with self.assertRaisesRegex(ToneShadowRolloutError, "refresh"):
                transition_tone_shadow_rollout(
                    db, rollout_id=planned.rollout_id, to_state="aborted",
                    expected_previous_transition_id=planned.transition_id,
                    actor="stale-operator", reason="Stale action.", now=T0,
                )
            paused = transition_tone_shadow_rollout(
                db, rollout_id=running.rollout_id, to_state="paused",
                expected_previous_transition_id=running.transition_id,
                actor="operator", reason="Pause for inspection.", now=T0,
            )
            aborted = transition_tone_shadow_rollout(
                db, rollout_id=paused.rollout_id, to_state="aborted",
                expected_previous_transition_id=paused.transition_id,
                actor="operator", reason="Stop without serving.", now=T0,
            )
            self.assertEqual((aborted.state, aborted.transition_version), ("aborted", 4))
            with self.assertRaisesRegex(ToneShadowRolloutError, "invalid"):
                transition_tone_shadow_rollout(
                    db, rollout_id=aborted.rollout_id, to_state="running",
                    expected_previous_transition_id=aborted.transition_id,
                    actor="operator", reason="Cannot reopen terminal state.", now=T0,
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE tone_shadow_rollouts SET sample_bps=10000")
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM tone_shadow_rollout_transitions")

    def test_unapproved_or_missing_admission_cannot_be_used(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(ToneShadowRolloutError, "approved admission"):
                create_tone_shadow_rollout(
                    db, admission_id="not-approved", sample_bps=500,
                    config=self.rollout_config, created_by="owner",
                    reason="Rejected admissions stay closed.", now=T0,
                )

    def test_migration_41_preserves_schema_40_database(self):
        predecessor = self.root / "schema40.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:40])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (41,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 40)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tone_shadow_rollouts"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
