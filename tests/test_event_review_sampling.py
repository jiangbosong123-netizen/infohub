import unittest

from app import database, db_admin
from app.event_match_reviews import record_match_review
from app.event_review_sampling import (
    create_sample_batch,
    sample_queue,
    sample_report,
)
from tests import test_event_match_reviews as match_fixture


NOW = match_fixture.NOW
canonical = match_fixture.canonical


LATER = "2026-09-26T12:01:00.000000Z"


class EventReviewSamplingTests(unittest.TestCase):
    def setUp(self):
        match_fixture.EventMatchReviewTests.setUp(self)
        with database.get_db() as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            for index, (decision, matcher, score) in enumerate((
                ("new_candidate", "matcher-v1", 0.20),
                ("new_candidate", "matcher-v1", 0.30),
                ("new_candidate", "matcher-v1", 0.40),
                ("needs_review", "matcher-v1", 0.70),
                ("new_candidate", "matcher-v2", None),
            ), 2):
                db.execute(
                    """INSERT INTO match_decisions(
                           id,dataset_id,decision_key,input_versions_json,
                           candidate_event_versions_json,matcher_version,features_json,
                           score,decision,reason,review_status,available_at)
                       VALUES(?,?,?,?,?,?,?, ?,?,?,'pending',?)""",
                    (
                        f"decision-{index}", dataset, f"fixture-{index}",
                        canonical([self.document_version_id]),
                        (canonical([self.event_version_id])
                         if decision != "new_candidate" else "[]"),
                        matcher, "{}",
                        score, decision, "fixture", NOW,
                    ),
                )

    def test_batch_is_deterministic_stratified_and_frozen(self):
        with database.get_db() as db:
            first = create_sample_batch(
                db, seed="event-quality-v1", per_stratum_limit=2,
                created_by="review-lead", now=NOW,
            )
            repeated = create_sample_batch(
                db, seed="event-quality-v1", per_stratum_limit=2,
                created_by="someone-else", now=NOW,
            )
            self.assertEqual(first.batch_id, repeated.batch_id)
            self.assertEqual(first.candidate_count, 6)
            self.assertEqual(first.stratum_count, 4)
            self.assertEqual(first.member_count, 5)
            initial = sample_queue(db, first.batch_id, limit=20)
            self.assertEqual(len(initial), 5)
            selected = initial[0]
            record_match_review(
                db, decision_id=selected.decision_id, decision="accepted",
                expected_previous_review_id=None,
                evidence_ids=(self.raw_record_id,), reviewer_id="human",
                reason="Sample evidence checked.", now=LATER,
            )
            report = sample_report(db, first.batch_id)
            self.assertEqual((report.accepted, report.rejected, report.pending), (1, 0, 4))
            self.assertEqual((report.decided_bps, report.acceptance_bps), (2000, 10000))
            self.assertEqual(len(sample_queue(db, first.batch_id, limit=20)), 4)
        db_admin.verify_database(self.path, require_current=True)

    def test_batch_members_and_review_order_are_immutable(self):
        with database.get_db() as db:
            batch = create_sample_batch(
                db, seed="immutable", per_stratum_limit=1,
                created_by="review-lead", now=NOW,
            )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE event_review_sampling_batches SET seed='rewrite' WHERE id=?",
                    (batch.batch_id,),
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "DELETE FROM event_review_sampling_members WHERE batch_id=?",
                    (batch.batch_id,),
                )
            record_match_review(
                db, decision_id=self.decision_id, decision="accepted",
                expected_previous_review_id=None,
                evidence_ids=(self.raw_record_id,), reviewer_id="human",
                reason="Evidence checked.", now=LATER,
            )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE event_match_review_order SET sequence=99")

    def test_review_cutoff_reproduces_prior_sample_state(self):
        with database.get_db() as db:
            batch = create_sample_batch(
                db, seed="cutoff", per_stratum_limit=2,
                created_by="review-lead", now=NOW,
            )
            item = sample_queue(db, batch.batch_id, limit=1)[0]
            record_match_review(
                db, decision_id=item.decision_id, decision="rejected",
                expected_previous_review_id=None,
                evidence_ids=(self.raw_record_id,), reviewer_id="human",
                reason="Does not match.", now=LATER,
            )
            current = sample_report(db, batch.batch_id)
            frozen = sample_report(
                db, batch.batch_id,
                review_cutoff_sequence=batch.review_cutoff_sequence,
            )
            self.assertEqual(current.rejected, 1)
            self.assertEqual(frozen.rejected, 0)
            self.assertEqual(frozen.pending, batch.member_count)

    def test_migration_36_preserves_schema_35_database(self):
        predecessor = self.path.parent / "schema35.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:35])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (36, 37))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 35)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM event_review_sampling_batches"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
