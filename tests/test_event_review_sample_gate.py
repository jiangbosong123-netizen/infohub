import unittest

from app import database, db_admin
from app.event_match_reviews import record_match_review
from app.event_review_sample_gate import (
    EventReviewSampleGateError,
    approved_sample_evaluation,
    record_sample_evaluation,
)
from app.event_review_sampling import create_sample_batch, sample_queue
from tests import test_event_review_sampling as sampling_fixture


NOW = sampling_fixture.NOW


class EventReviewSampleGateTests(unittest.TestCase):
    def setUp(self):
        sampling_fixture.EventReviewSamplingTests.setUp(self)
        with database.get_db() as db:
            self.batch = create_sample_batch(
                db, seed="event-gate-v1", per_stratum_limit=2,
                created_by="fixture", now=NOW,
            )

    def evaluate(self, db, decision, previous=None):
        return record_sample_evaluation(
            db, batch_id=self.batch.batch_id, decision=decision,
            expected_previous_evaluation_id=previous,
            minimum_decided_bps=10_000,
            minimum_stratum_decided_bps=10_000,
            minimum_acceptance_bps=9_000,
            minimum_stratum_acceptance_bps=9_000,
            evaluator_id="quality-lead", reason="Fixed event quality policy.", now=NOW,
        )

    def review_all(self, db):
        for item in sample_queue(db, self.batch.batch_id, limit=250):
            record_match_review(
                db, decision_id=item.decision_id, decision="accepted",
                expected_previous_review_id=None,
                evidence_ids=(self.raw_record_id,), reviewer_id="human",
                reason="Sample evidence checked.", now=NOW,
            )

    def test_incomplete_sample_is_rejected_then_completed_sample_is_approved(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(EventReviewSampleGateError, "completion"):
                self.evaluate(db, "approved")
            rejected = self.evaluate(db, "rejected")
            self.review_all(db)
            approved = self.evaluate(db, "approved", rejected.current_evaluation_id)
            self.assertEqual((approved.current_decision, approved.current_version), ("approved", 2))
            proof = approved_sample_evaluation(db, self.batch.batch_id)
            self.assertEqual(proof["evaluation_id"], approved.current_evaluation_id)
        db_admin.verify_database(self.path, require_current=True)

    def test_later_correction_invalidates_approval_and_history_is_immutable(self):
        with database.get_db() as db:
            self.review_all(db)
            approved = self.evaluate(db, "approved")
            first = sample_queue(
                db, self.batch.batch_id, limit=1, pending_only=False,
            )[0]
            record_match_review(
                db, decision_id=first.decision_id, decision="rejected",
                expected_previous_review_id=first.current_review_id,
                evidence_ids=(self.raw_record_id,), reviewer_id="human",
                reason="Correction after secondary review.", now=NOW,
            )
            with self.assertRaisesRegex(EventReviewSampleGateError, "metrics changed"):
                approved_sample_evaluation(db, self.batch.batch_id)
            with self.assertRaisesRegex(EventReviewSampleGateError, "refresh"):
                self.evaluate(db, "rejected", None)
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE event_review_sample_evaluations SET reason='rewrite' WHERE id=?",
                    (approved.current_evaluation_id,),
                )
        db_admin.verify_database(self.path, require_current=True)

    def test_weak_stratum_blocks_overall_approval(self):
        with database.get_db() as db:
            items = sample_queue(db, self.batch.batch_id, limit=250)
            weak = items[0].stratum_key
            for item in items:
                record_match_review(
                    db, decision_id=item.decision_id,
                    decision="rejected" if item.stratum_key == weak else "accepted",
                    expected_previous_review_id=None,
                    evidence_ids=(self.raw_record_id,), reviewer_id="human",
                    reason="Stratified sample decision.", now=NOW,
                )
            with self.assertRaisesRegex(EventReviewSampleGateError, "acceptance threshold"):
                self.evaluate(db, "approved")

    def test_migration_37_preserves_schema_36_database(self):
        predecessor = self.path.parent / "schema36.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:36])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (37,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 36)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM event_review_sample_evaluations"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
