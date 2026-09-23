import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.db_admin import verify_database
from app.topic_assignment_reviews import (
    TopicAssignmentReviewError,
    record_topic_assignment_review,
    review_preview,
)


NOW = "2026-09-23T12:00:00.000000Z"


class TopicAssignmentReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start(); self.addCleanup(mocked.stop)
        database.init_schema()
        with database.get_db() as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.com','T','ai',?,?)""", (NOW, NOW))
            db.execute("""INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at)
                          VALUES('d',?,1,'article',?)""", (dataset, NOW))
            db.execute("""INSERT INTO document_versions(
                id,document_id,version,normalizer_version,normalized_at,title_original,
                language,text,content_sha256,version_sha256,canonical_url,source_id,
                published_precision,time_status,time_rule_version,tzdb_version,
                content_origin,content_extent,truncated,extraction_status,correction_kind,
                available_at,availability_basis,point_in_time_eligible)
                VALUES('dv','d',1,'v',?,'T','en','',?,?, 'https://example.com',1,
                'unknown','legacy_unverified','legacy','unknown','legacy_unknown','none',0,
                'not_attempted','initial',?,'legacy_unknown',0)""", (NOW, 'a'*64, 'b'*64, NOW))
            db.execute("UPDATE documents SET current_version_id='dv' WHERE id='d'")
            db.execute("""INSERT INTO topic_catalog(id,dataset_id,status,created_at)
                          VALUES('t',?,'active',?)""", (dataset, NOW))
            db.execute("""INSERT INTO topic_versions(id,topic_id,version,slug,name,group_key,
                description,rules_json,rules_hash,version_sha256,status,available_at)
                VALUES('tv','t',1,'topic','Topic','technology','','{}',?,?,'active',?)""",
                       ('c'*64, 'd'*64, NOW))
            db.execute("UPDATE topic_catalog SET current_version_id='tv' WHERE id='t'")
            db.execute("""INSERT INTO document_topic_assignments(
                id,document_version_id,topic_version_id,method,method_version,status,available_at)
                VALUES('a','dv','tv','legacy_projection','legacy-item-topics-v1','candidate',?)""", (NOW,))

    def test_decisions_append_and_latest_decision_is_effective(self):
        with database.get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            first = record_topic_assignment_review(
                db, assignment_id='a', decision='accepted',
                expected_previous_review_id=None, reviewer_id='reviewer-a',
                reason='Evidence checked.', now=NOW,
            )
            second = record_topic_assignment_review(
                db, assignment_id='a', decision='rejected',
                expected_previous_review_id=first.current_review_id,
                reviewer_id='reviewer-b', reason='Correction received.', now=NOW,
            )
        self.assertEqual((first.effective_status, first.current_review_version), ('accepted', 1))
        self.assertEqual((second.effective_status, second.current_review_version), ('rejected', 2))
        with database.get_db() as db:
            self.assertEqual(review_preview(db, 'a').original_status, 'candidate')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM topic_assignment_reviews").fetchone()[0], 2)
        verify_database(self.path, require_current=True)

    def test_stale_reviewer_cannot_overwrite_current_decision(self):
        with database.get_db() as db:
            first = record_topic_assignment_review(
                db, assignment_id='a', decision='accepted', expected_previous_review_id=None,
                reviewer_id='reviewer', reason='Checked.', now=NOW,
            )
            with self.assertRaisesRegex(TopicAssignmentReviewError, 'refresh'):
                record_topic_assignment_review(
                    db, assignment_id='a', decision='rejected',
                    expected_previous_review_id=None, reviewer_id='stale', reason='Stale.', now=NOW,
                )
            self.assertIsNotNone(first.current_review_id)

    def test_unknown_evidence_and_history_mutation_are_rejected(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(TopicAssignmentReviewError, 'unknown evidence'):
                record_topic_assignment_review(
                    db, assignment_id='a', decision='accepted',
                    expected_previous_review_id=None, reviewer_id='reviewer',
                    reason='Checked.', evidence_ids=('missing',), now=NOW,
                )
            recorded = record_topic_assignment_review(
                db, assignment_id='a', decision='accepted', expected_previous_review_id=None,
                reviewer_id='reviewer', reason='Checked.', now=NOW,
            )
            with self.assertRaisesRegex(Exception, 'immutable'):
                db.execute("UPDATE topic_assignment_reviews SET reason='rewrite' WHERE id=?",
                           (recorded.current_review_id,))
            with self.assertRaisesRegex(Exception, 'immutable'):
                db.execute("DELETE FROM topic_assignment_reviews")


if __name__ == '__main__':
    unittest.main()
