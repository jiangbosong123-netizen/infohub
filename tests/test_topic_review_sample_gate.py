import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.topic_assignment_reviews import record_topic_assignment_review
from app.topic_review_sample_gate import (
    TopicReviewSampleGateError,
    approved_sample_evaluation,
    record_sample_evaluation,
    sample_gate_preview,
)
from app.topic_review_sampling import create_sample_batch, sample_queue


NOW = "2026-09-24T12:00:00.000000Z"


class TopicReviewSampleGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        with database.get_db() as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
            db.execute(
                "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES('t',?,'active',?)",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO topic_versions(id,topic_id,version,slug,name,group_key,
                       description,rules_json,rules_hash,version_sha256,status,available_at)
                   VALUES('tv','t',1,'topic','Topic','technology','','{}',?,?,'active',?)""",
                ("a" * 64, "b" * 64, NOW),
            )
            db.execute("UPDATE topic_catalog SET current_version_id='tv' WHERE id='t'")
            for index in (1, 2):
                db.execute(
                    """INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                       VALUES(?,1,?,?,'ai',?,?)""",
                    (index, f"https://example.test/{index}", f"Title {index}", NOW, NOW),
                )
                db.execute(
                    "INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at) VALUES(?,?,?,'article',?)",
                    (f"d{index}", dataset, index, NOW),
                )
                db.execute(
                    """INSERT INTO document_versions(
                           id,document_id,version,normalizer_version,normalized_at,title_original,
                           language,text,content_sha256,version_sha256,canonical_url,source_id,
                           published_precision,time_status,time_rule_version,tzdb_version,
                           content_origin,content_extent,truncated,extraction_status,correction_kind,
                           available_at,availability_basis,point_in_time_eligible)
                       VALUES(?,?,1,'v1',?,?,'en','',?,?,?,1,'unknown','legacy_unverified',
                              'legacy','unknown','legacy_unknown','none',0,'not_attempted','initial',
                              ?,'legacy_unknown',0)""",
                    (f"dv{index}", f"d{index}", NOW, f"Title {index}",
                     f"{index:064x}", f"{index + 10:064x}",
                     f"https://example.test/{index}", NOW),
                )
                db.execute(
                    "UPDATE documents SET current_version_id=? WHERE id=?",
                    (f"dv{index}", f"d{index}"),
                )
                db.execute(
                    """INSERT INTO document_topic_assignments(
                           id,document_version_id,topic_version_id,method,method_version,status,available_at)
                       VALUES(?,?,'tv','fixture','fixture-v1','candidate',?)""",
                    (f"a{index}", f"dv{index}", NOW),
                )
            self.batch = create_sample_batch(
                db, seed="gate-v1", per_topic_limit=2, created_by="fixture", now=NOW
            )

    def _evaluate(self, db, decision, previous=None):
        return record_sample_evaluation(
            db, batch_id=self.batch.batch_id, decision=decision,
            expected_previous_evaluation_id=previous,
            minimum_decided_bps=10_000, minimum_topic_decided_bps=10_000,
            minimum_acceptance_bps=9_000, minimum_topic_acceptance_bps=9_000,
            evaluator_id="quality-lead", reason="Fixed sample quality policy.", now=NOW,
        )

    def test_gate_rejects_incomplete_sample_then_approves_completed_sample(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(TopicReviewSampleGateError, "completion"):
                self._evaluate(db, "approved")
            rejected = self._evaluate(db, "rejected")
            for item in sample_queue(db, self.batch.batch_id, limit=10):
                record_topic_assignment_review(
                    db, assignment_id=item.assignment_id, decision="accepted",
                    expected_previous_review_id=None, reviewer_id="human",
                    reason="Evidence checked.", now=NOW,
                )
            approved = self._evaluate(db, "approved", rejected.current_evaluation_id)
            self.assertEqual((approved.current_decision, approved.current_version), ("approved", 2))
            proof = approved_sample_evaluation(db, self.batch.batch_id)
            self.assertEqual(proof["evaluation_id"], approved.current_evaluation_id)
        db_admin.verify_database(self.path, require_current=True)

    def test_later_review_correction_makes_current_approval_stale(self):
        with database.get_db() as db:
            for item in sample_queue(db, self.batch.batch_id, limit=10):
                record_topic_assignment_review(
                    db, assignment_id=item.assignment_id, decision="accepted",
                    expected_previous_review_id=None, reviewer_id="human",
                    reason="Evidence checked.", now=NOW,
                )
            approved = self._evaluate(db, "approved")
            first = sample_queue(
                db, self.batch.batch_id, limit=10, pending_only=False
            )[0]
            record_topic_assignment_review(
                db, assignment_id=first.assignment_id, decision="rejected",
                expected_previous_review_id=first.current_review_id,
                reviewer_id="human", reason="Correction received.", now=NOW,
            )
            with self.assertRaisesRegex(TopicReviewSampleGateError, "metrics changed"):
                approved_sample_evaluation(db, self.batch.batch_id)
            with self.assertRaisesRegex(TopicReviewSampleGateError, "refresh"):
                self._evaluate(db, "rejected", None)
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE topic_review_sample_evaluations SET reason='rewrite' WHERE id=?",
                    (approved.current_evaluation_id,),
                )
        db_admin.verify_database(self.path, require_current=True)

    def test_migration_32_preserves_schema_31_database(self):
        predecessor = self.path.parent / "predecessor.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:31])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (32, 33, 34))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 31)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "x")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM topic_review_sample_evaluations"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
