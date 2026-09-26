import unittest

from app import database, db_admin
from app.event_admission import record_admission_review
from app.event_dataset_release import (
    EventDatasetReleaseError,
    approved_event_release,
    record_release_review,
)
from app.event_match_reviews import record_match_review
from app.event_review_sample_gate import record_sample_evaluation
from app.event_review_sampling import create_sample_batch, sample_queue
from tests import test_event_review_sampling as sampling_fixture


NOW = sampling_fixture.NOW


class EventDatasetReleaseTests(unittest.TestCase):
    def setUp(self):
        sampling_fixture.EventReviewSamplingTests.setUp(self)
        with database.get_db() as db:
            self.batch = create_sample_batch(
                db, seed="release-v1", per_stratum_limit=2,
                created_by="fixture", now=NOW,
            )

    def review_sample(self, db):
        for item in sample_queue(db, self.batch.batch_id, limit=250):
            record_match_review(
                db, decision_id=item.decision_id, decision="accepted",
                expected_previous_review_id=None,
                evidence_ids=(self.raw_record_id,), reviewer_id="human",
                reason="Release sample evidence checked.", now=NOW,
            )

    def approve_sample(self, db, *, weak=False):
        value = 1 if weak else 9_000
        result = record_sample_evaluation(
            db, batch_id=self.batch.batch_id, decision="approved",
            expected_previous_evaluation_id=None,
            minimum_decided_bps=10_000,
            minimum_stratum_decided_bps=10_000,
            minimum_acceptance_bps=value,
            minimum_stratum_acceptance_bps=value,
            evaluator_id="quality-lead", reason="Sample quality reviewed.", now=NOW,
        )
        return result.current_evaluation_id

    def admit_event(self, db, previous=None):
        return record_admission_review(
            db, event_version_id=self.event_version_id, decision="reported",
            expected_previous_review_id=previous, reviewer_id="event-editor",
            reason="Current evidence and accepted match reviewed.", now=NOW,
        )

    def test_release_binds_current_sample_and_exact_admitted_event_manifest(self):
        with database.get_db() as db:
            self.review_sample(db)
            evaluation_id = self.approve_sample(db)
            self.admit_event(db)
            release = record_release_review(
                db, sample_evaluation_id=evaluation_id, decision="approved",
                expected_previous_review_id=None, reviewer_id="release-lead",
                reason="Sample policy and admitted event manifest passed.", now=NOW,
            )
            self.assertEqual(release.metrics["admitted_event_count"], 1)
            proof = approved_event_release(db)
            self.assertEqual(len(proof["event_manifest"]), 1)
            self.assertEqual(proof["event_manifest"][0]["event_id"], self.event_id)
        db_admin.verify_database(self.path, require_current=True)

    def test_weak_sample_thresholds_and_empty_manifest_fail_closed(self):
        with database.get_db() as db:
            self.review_sample(db)
            weak_evaluation = self.approve_sample(db, weak=True)
            with self.assertRaisesRegex(EventDatasetReleaseError, "thresholds"):
                record_release_review(
                    db, sample_evaluation_id=weak_evaluation, decision="approved",
                    expected_previous_review_id=None, reviewer_id="release-lead",
                    reason="Must not pass weak policy.", now=NOW,
                )

    def test_later_event_admission_invalidates_release_and_history_is_immutable(self):
        with database.get_db() as db:
            self.review_sample(db)
            evaluation_id = self.approve_sample(db)
            admission = self.admit_event(db)
            release = record_release_review(
                db, sample_evaluation_id=evaluation_id, decision="approved",
                expected_previous_review_id=None, reviewer_id="release-lead",
                reason="Initial release.", now=NOW,
            )
            self.admit_event(db, admission.current_review_id)
            with self.assertRaisesRegex(EventDatasetReleaseError, "stale"):
                approved_event_release(db)
            with self.assertRaisesRegex(EventDatasetReleaseError, "refresh"):
                record_release_review(
                    db, sample_evaluation_id=evaluation_id, decision="rejected",
                    expected_previous_review_id=None, reviewer_id="stale-writer",
                    reason="Stale release decision.", now=NOW,
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE event_dataset_release_reviews SET reason='rewrite' WHERE id=?",
                    (release.current_review_id,),
                )

    def test_new_match_decision_invalidates_release_sample_coverage(self):
        with database.get_db() as db:
            self.review_sample(db)
            evaluation_id = self.approve_sample(db)
            self.admit_event(db)
            record_release_review(
                db, sample_evaluation_id=evaluation_id, decision="approved",
                expected_previous_review_id=None, reviewer_id="release-lead",
                reason="Current decision population sampled.", now=NOW,
            )
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,
                       score,decision,reason,review_status,available_at)
                   VALUES(?,?,?,?,?,?,?, ?,?,?,'pending',?)""",
                (
                    "post-release-decision", dataset_id, "post-release-key",
                    sampling_fixture.canonical([self.document_version_id]), "[]",
                    "matcher-v3", "{}", 0.5, "new_candidate",
                    "Arrived after the sample was frozen.", NOW,
                ),
            )
            with self.assertRaisesRegex(EventDatasetReleaseError, "stale"):
                approved_event_release(db)

    def test_migration_38_preserves_schema_37_database(self):
        predecessor = self.path.parent / "schema37.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:37])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (38,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 37)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM event_dataset_release_reviews"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
