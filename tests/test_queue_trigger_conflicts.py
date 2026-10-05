import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import config, database, db_admin
from app.analysis_results import publish_analysis_result
from tests import test_analysis_runs as analysis_run_fixtures

# A trigger statement's own OR <policy> is replaced by the firing statement's policy.
CONFLICT_CLAUSE = re.compile(r"\b(INSERT|UPDATE)\s+OR\s+(REPLACE|IGNORE|ABORT|FAIL|ROLLBACK)\b"
                             r"|\bREPLACE\s+INTO\b", re.IGNORECASE)
QUEUE_TRIGGERS = {
    "items_derived_insert", "items_derived_update",
    "curation_search_item_ai", "curation_search_item_au",
    "curation_search_document_ai", "curation_search_document_au",
    "curation_search_publication_ai", "curation_search_publication_au",
    "topic_statistics_topic_insert", "topic_statistics_topic_update",
    "topic_statistics_assignment_insert", "topic_statistics_review_insert",
    "topic_statistics_event_update",
}


def triggers(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as db:
        return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))


class QueueTriggerConflictTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"

    def test_no_trigger_depends_on_the_firing_statements_conflict_policy(self):
        db_admin.migrate_database(self.path)
        found = triggers(self.path)
        self.assertTrue(QUEUE_TRIGGERS <= set(found))
        self.assertEqual(
            sorted(name for name, sql in found.items() if CONFLICT_CLAUSE.search(sql)), []
        )

    def test_migration_47_rewrites_only_the_queue_triggers(self):
        with database.get_db(self.path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:46])
        before = triggers(self.path)
        # Compare against exactly schema 47; later migrations may redefine other triggers.
        with database.get_db(self.path) as db:
            self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:47]), (47,))
        after = triggers(self.path)
        self.assertEqual(set(after), set(before))
        self.assertEqual({name for name in after if after[name] != before[name]}, QUEUE_TRIGGERS)
        self.assertTrue(all(CONFLICT_CLAUSE.search(before[name]) for name in QUEUE_TRIGGERS))

    def test_update_or_ignore_still_refreshes_the_queue_reason(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'t','T','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/a','Original','ai',
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
            db.execute("UPDATE OR IGNORE items SET title='Changed' WHERE id=1")
            self.assertEqual(
                db.execute("SELECT reason FROM curation_search_dirty WHERE item_id=1").fetchone()[0],
                "item_update",
            )


class SupersededPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = analysis_run_fixtures.AnalysisRunTests(
            "test_manifest_is_version_pinned_immutable_and_idempotent"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def publish(self, key: str, summary: str) -> None:
        fixture = self.fixture
        job, run, attempt = fixture.completed_attempt(key)
        publish_analysis_result(
            job_id=job.id, lease_token=job.lease_token, expected_input_version=fixture.doc,
            run_id=run.id, attempt_id=attempt.id, validated_output={
                "schema_version": "summary/1.0",
                "subject": {"type": "document", "version_id": fixture.doc},
                "status": "valid", "evidence_ids": [fixture.raw],
                "data": {"summary": summary, "raw_confidence": 0.8},
            },
            review_status="unreviewed", evidence_status="supported",
            idempotency_key=f"result:{key}", now=analysis_run_fixtures.T0,
        )

    def test_superseding_a_publication_while_the_item_is_still_queued(self):
        self.publish("first", "First summary")
        with database.get_db() as db:
            self.assertEqual(
                [row[0] for row in db.execute("SELECT reason FROM curation_search_dirty")],
                ["publication_insert"],
            )
        self.publish("second", "Second summary")
        with database.get_db() as db:
            self.assertEqual(
                [row[0] for row in db.execute("SELECT reason FROM curation_search_dirty")],
                ["publication_update"],
            )
            current = db.execute(
                """SELECT version.version FROM analysis_publications AS pointer
                   JOIN analysis_publication_versions AS version
                     ON version.id=pointer.current_publication_id"""
            ).fetchone()[0]
        self.assertEqual(current, 2)
        db_admin.verify_database(config.DB_PATH, require_current=True)


if __name__ == "__main__":
    unittest.main()
