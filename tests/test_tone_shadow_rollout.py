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
from app.tone_shadow_evaluation import (
    ToneShadowEvaluationError,
    create_tone_shadow_batch,
    evaluate_tone_shadow_batch,
    record_tone_shadow_observation,
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

    def create(self, sample_bps=500):
        with database.get_db() as db:
            return create_tone_shadow_rollout(
                db, admission_id=self.admission_id, sample_bps=sample_bps,
                config=self.rollout_config, created_by="rollout-owner",
                reason="Start with a bounded five percent shadow sample.", now=T0,
            )

    def add_documents(self, count=3):
        identifiers = []
        with database.get_db() as db:
            dataset_id = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'shadow','Shadow','ai','rss')")
            for number in range(1, count + 1):
                item_id = number
                document_id = f"shadow-document-{number}"
                version_id = f"shadow-document-version-{number}"
                db.execute(
                    """INSERT INTO items(
                           id,source_id,url,title,channel,published_at,fetched_at)
                       VALUES(?,1,?,?, 'ai',?,?)""",
                    (item_id, f"https://example.test/{number}", f"Document {number}", T0, T0),
                )
                db.execute(
                    """INSERT INTO documents(
                           id,dataset_id,legacy_item_id,kind,first_seen_at)
                       VALUES(?,?,?,'article',?)""",
                    (document_id, dataset_id, item_id, T0),
                )
                db.execute(
                    """INSERT INTO document_versions(
                           id,document_id,version,normalizer_version,normalized_at,
                           title_original,language,text,content_sha256,version_sha256,
                           canonical_url,source_id,published_precision,time_status,
                           time_rule_version,tzdb_version,content_origin,content_extent,
                           truncated,extraction_status,correction_kind,available_at,
                           availability_basis,point_in_time_eligible)
                       VALUES(?,?,1,'v1',?,?,'en','',?,?,?,1,'unknown',
                              'legacy_unverified','legacy','unknown','legacy_unknown','none',
                              0,'not_attempted','initial',?,'legacy_unknown',0)""",
                    (
                        version_id, document_id, T0, f"Document {number}",
                        f"{number:064x}"[-64:], f"{number + 10:064x}"[-64:],
                        f"https://example.test/{number}", T0,
                    ),
                )
                db.execute(
                    "UPDATE documents SET current_version_id=? WHERE id=?",
                    (version_id, document_id),
                )
                identifiers.append(version_id)
        return identifiers

    def running_batch(self):
        self.rollout_config = {
            **self.rollout_config,
            "minimum_observations": 2,
            "maximum_error_bps": 0,
            "maximum_disagreement_bps": 0,
        }
        documents = self.add_documents()
        planned = self.create(sample_bps=10_000)
        with database.get_db() as db:
            running = transition_tone_shadow_rollout(
                db, rollout_id=planned.rollout_id, to_state="running",
                expected_previous_transition_id=planned.transition_id,
                actor="operator", reason="Begin frozen observation batch.", now=T0,
            )
            batch = create_tone_shadow_batch(
                db, rollout_id=running.rollout_id,
                population_subject_version_ids=documents,
                created_by="operator", now=T0,
            )
        return running, batch, documents

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
        self.assertEqual(
            db_admin.verify_database(self.path, require_current=True).schema_version, 42
        )

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

    def test_migration_42_preserves_schema_41_database(self):
        predecessor = self.root / "schema41.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:41])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (42,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 41)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tone_shadow_rollouts"
            ).fetchone()[0], 0)

    def test_frozen_batch_requires_complete_population_and_is_immutable(self):
        _, batch, documents = self.running_batch()
        self.assertEqual((batch.population_count, batch.selected_count), (3, 3))
        with database.get_db() as db:
            population = db.execute(
                """SELECT subject_version_id,selected FROM tone_shadow_population_members
                   WHERE batch_id=? ORDER BY ordinal""", (batch.batch_id,),
            ).fetchall()
            self.assertEqual(
                [(row[0], row[1]) for row in population],
                [(item, 1) for item in sorted(documents)],
            )
            members = db.execute(
                """SELECT subject_version_id FROM tone_shadow_batch_members
                   WHERE batch_id=? ORDER BY ordinal""", (batch.batch_id,),
            ).fetchall()
            self.assertEqual([row[0] for row in members], sorted(documents))
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM tone_shadow_batch_members")

    def test_complete_passing_observations_are_required_for_completion(self):
        running, batch, documents = self.running_batch()
        with database.get_db() as db:
            for subject in documents:
                record_tone_shadow_observation(
                    db, batch_id=batch.batch_id, subject_version_id=subject,
                    outcome="matched", candidate_result_sha256="a" * 64,
                    reference_result_sha256="b" * 64, error_code=None,
                    recorded_by="worker", observed_at=T0,
                )
            evaluation = evaluate_tone_shadow_batch(
                db, batch_id=batch.batch_id, evaluated_by="quality-owner",
                reason="All frozen observations meet the configured gates.", now=T0,
            )
            self.assertEqual(evaluation.decision, "passed")
            with self.assertRaisesRegex(ToneShadowRolloutError, "requires"):
                transition_tone_shadow_rollout(
                    db, rollout_id=running.rollout_id, to_state="completed",
                    expected_previous_transition_id=running.transition_id,
                    actor="operator", reason="Missing evaluation binding.", now=T0,
                )
            completed = transition_tone_shadow_rollout(
                db, rollout_id=running.rollout_id, to_state="completed",
                expected_previous_transition_id=running.transition_id,
                actor="operator", reason="Complete after passing shadow gate.",
                shadow_evaluation_id=evaluation.evaluation_id, now=T0,
            )
            self.assertEqual(completed.state, "completed")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM analysis_publications"
            ).fetchone()[0], 0)
        db_admin.verify_database(self.path, require_current=True)

    def test_incomplete_duplicate_and_unselected_observations_fail_closed(self):
        _, batch, documents = self.running_batch()
        with database.get_db() as db:
            with self.assertRaisesRegex(ToneShadowEvaluationError, "lowercase SHA-256"):
                record_tone_shadow_observation(
                    db, batch_id=batch.batch_id, subject_version_id=documents[0],
                    outcome="matched", candidate_result_sha256="A" * 64,
                    reference_result_sha256="b" * 64, error_code=None,
                    recorded_by="worker", observed_at=T0,
                )
            record_tone_shadow_observation(
                db, batch_id=batch.batch_id, subject_version_id=documents[0],
                outcome="matched", candidate_result_sha256="a" * 64,
                reference_result_sha256="b" * 64, error_code=None,
                recorded_by="worker", observed_at=T0,
            )
            with self.assertRaisesRegex(ToneShadowEvaluationError, "already observed"):
                record_tone_shadow_observation(
                    db, batch_id=batch.batch_id, subject_version_id=documents[0],
                    outcome="matched", candidate_result_sha256="a" * 64,
                    reference_result_sha256="b" * 64, error_code=None,
                    recorded_by="worker", observed_at=T0,
                )
            with self.assertRaisesRegex(ToneShadowEvaluationError, "incomplete"):
                evaluate_tone_shadow_batch(
                    db, batch_id=batch.batch_id, evaluated_by="owner",
                    reason="This must remain incomplete.", now=T0,
                )

    def test_failed_gate_cannot_complete_rollout(self):
        running, batch, documents = self.running_batch()
        with database.get_db() as db:
            for index, subject in enumerate(documents):
                record_tone_shadow_observation(
                    db, batch_id=batch.batch_id, subject_version_id=subject,
                    outcome="error" if index == 0 else "matched",
                    candidate_result_sha256=None if index == 0 else "a" * 64,
                    reference_result_sha256="b" * 64,
                    error_code="provider_timeout" if index == 0 else None,
                    recorded_by="worker", observed_at=T0,
                )
            evaluation = evaluate_tone_shadow_batch(
                db, batch_id=batch.batch_id, evaluated_by="quality-owner",
                reason="Observed error rate exceeds the zero-error threshold.", now=T0,
            )
            self.assertEqual(evaluation.decision, "failed")
            with self.assertRaisesRegex(ToneShadowRolloutError, "refresh"):
                transition_tone_shadow_rollout(
                    db, rollout_id=running.rollout_id, to_state="completed",
                    expected_previous_transition_id=running.transition_id,
                    actor="operator", reason="A failed gate cannot complete.",
                    shadow_evaluation_id=evaluation.evaluation_id, now=T0,
                )


if __name__ == "__main__":
    unittest.main()
