import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.topic_statistics import advance_topic_statistics
from app.topic_statistics_admission import (
    TopicStatisticsAdmissionError,
    admission_preview,
    record_admission_review,
)


NOW = "2026-09-23T18:00:00.000000Z"


class TopicStatisticsAdmissionTests(unittest.TestCase):
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
                """INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                   VALUES(1,1,'https://example.test/1','One','ai',?,?)""",
                (NOW, NOW),
            )
            db.execute(
                "INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at) VALUES('d',?,1,'article',?)",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO document_versions(
                       id,document_id,version,normalizer_version,normalized_at,title_original,
                       language,text,content_sha256,version_sha256,canonical_url,source_id,
                       published_precision,time_status,time_rule_version,tzdb_version,
                       content_origin,content_extent,truncated,extraction_status,correction_kind,
                       available_at,availability_basis,point_in_time_eligible)
                   VALUES('dv','d',1,'v1',?,'One','en','',?,?, 'https://example.test/1',1,
                          'unknown','legacy_unverified','legacy','unknown','legacy_unknown','none',0,
                          'not_attempted','initial',?,'legacy_unknown',0)""",
                (NOW, "a" * 64, "b" * 64, NOW),
            )
            db.execute("UPDATE documents SET current_version_id='dv' WHERE id='d'")
            db.execute(
                "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES('t',?,'active',?)",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO topic_versions(
                       id,topic_id,version,slug,name,group_key,description,rules_json,
                       rules_hash,version_sha256,status,available_at)
                   VALUES('tv','t',1,'topic','Topic','technology','','{}',?,?,'active',?)""",
                ("c" * 64, "d" * 64, NOW),
            )
            db.execute("UPDATE topic_catalog SET current_version_id='tv' WHERE id='t'")
            db.execute(
                """INSERT INTO document_topic_assignments(
                       id,document_version_id,topic_version_id,method,method_version,status,available_at)
                   VALUES('a','dv','tv','fixture','fixture-v1','candidate',?)""",
                (NOW,),
            )
        advance_topic_statistics(25)
        self.publication = advance_topic_statistics(25).publication_id

    def test_preview_exposes_candidate_backlog_and_policy_zero(self):
        with database.get_db() as db:
            preview = admission_preview(db, self.publication)
        self.assertEqual(preview.metrics["assignment_total"], 1)
        self.assertEqual(preview.metrics["effective_candidate"], 1)
        self.assertEqual(preview.metrics["decided_assignment_bps"], 0)
        self.assertEqual(preview.metrics["published_document_members"], 0)
        self.assertIsNone(preview.current_decision)

    def test_approval_enforces_coverage_and_explicit_zero_exception(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(TopicStatisticsAdmissionError, "coverage"):
                record_admission_review(
                    db, publication_id=self.publication, decision="approved",
                    expected_previous_review_id=None, minimum_decided_assignment_bps=1,
                    allow_zero_members=True, reviewer_id="reviewer", reason="Not enough.", now=NOW,
                )
            with self.assertRaisesRegex(TopicStatisticsAdmissionError, "zero-member"):
                record_admission_review(
                    db, publication_id=self.publication, decision="approved",
                    expected_previous_review_id=None, minimum_decided_assignment_bps=0,
                    allow_zero_members=False, reviewer_id="reviewer", reason="No zero.", now=NOW,
                )
            approved = record_admission_review(
                db, publication_id=self.publication, decision="approved",
                expected_previous_review_id=None, minimum_decided_assignment_bps=0,
                allow_zero_members=True, reviewer_id="reviewer", reason="Synthetic exception.", now=NOW,
            )
        self.assertEqual((approved.current_decision, approved.current_review_version),
                         ("approved", 1))

    def test_reviews_are_append_only_and_stale_writers_fail(self):
        with database.get_db() as db:
            rejected = record_admission_review(
                db, publication_id=self.publication, decision="rejected",
                expected_previous_review_id=None, minimum_decided_assignment_bps=9000,
                allow_zero_members=False, reviewer_id="reviewer", reason="Backlog too large.", now=NOW,
            )
            with self.assertRaisesRegex(TopicStatisticsAdmissionError, "refresh"):
                record_admission_review(
                    db, publication_id=self.publication, decision="approved",
                    expected_previous_review_id=None, minimum_decided_assignment_bps=0,
                    allow_zero_members=True, reviewer_id="stale", reason="Stale.", now=NOW,
                )
            approved = record_admission_review(
                db, publication_id=self.publication, decision="approved",
                expected_previous_review_id=rejected.current_review_id,
                minimum_decided_assignment_bps=0, allow_zero_members=True,
                reviewer_id="reviewer", reason="Explicit test exception.", now=NOW,
            )
            self.assertEqual(approved.current_review_version, 2)
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE topic_statistics_admission_reviews SET reason='rewrite'")
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM topic_statistics_admission_reviews")

    def test_migration_29_preserves_schema_28_database(self):
        predecessor = self.path.parent / "predecessor.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:28])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (29,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 28)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "x")


if __name__ == "__main__":
    unittest.main()
