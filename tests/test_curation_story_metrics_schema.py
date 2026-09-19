import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import enqueue_legacy_curation_item, process_one_legacy_curation_import


class CurationStoryMetricsSchemaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"

    @staticmethod
    def _seed(db):
        db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
        db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                      VALUES(1,1,'https://example.test/1','Original','ai',
                      '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
        db.execute("""INSERT INTO stories(id,title,channel,url,first_at,last_at)
                      VALUES('story-one','Original','ai','https://example.test/1',
                      '2026-09-10T09:00:00+00:00','2026-09-10T09:00:00+00:00')""")

    def test_upgrade_from_17_is_empty_and_does_not_rewrite_story(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:17])
            self._seed(db)
            db.execute("INSERT INTO story_items(item_id,story_id) VALUES(1,'story-one')")
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.applied_versions, (18, 19))
        self.assertEqual(db_admin.verify_database(self.path, require_current=True).schema_version, 19)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_story_metrics").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_story_metrics_dirty").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status,last_story_id,indexed_count FROM curation_story_metrics_state").fetchone(), ("empty", "", 0))
            self.assertEqual(db.execute("SELECT title FROM stories WHERE id='story-one'").fetchone()[0], "Original")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM story_items").fetchone()[0], 1)

    def test_membership_and_item_change_enqueue_story_then_delete_cascades(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            self._seed(db)
            db.execute("INSERT INTO story_items(item_id,story_id) VALUES(1,'story-one')")
            self.assertEqual(db.execute("SELECT reason FROM curation_story_metrics_dirty WHERE story_id='story-one'").fetchone()[0], "member_insert")
            db.execute("DELETE FROM curation_story_metrics_dirty")
            db.execute("UPDATE items SET title_zh='新标题' WHERE id=1")
            self.assertEqual(db.execute("SELECT reason FROM curation_story_metrics_dirty WHERE story_id='story-one'").fetchone()[0], "item_update")
            db.execute("DELETE FROM story_items WHERE story_id='story-one'")
            db.execute("DELETE FROM stories WHERE id='story-one'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_story_metrics_dirty").fetchone()[0], 0)

    def test_current_publication_change_enqueues_member_story(self):
        with patch.object(database, "DB_PATH", self.path), patch.object(config, "BLOB_PATH", self.path.parent / "blobs"):
            database.init_schema()
            with database.get_db() as db:
                self._seed(db)
                db.execute("INSERT INTO story_items(item_id,story_id) VALUES(1,'story-one')")
                db.execute("DELETE FROM curation_story_metrics_dirty")
            for _ in range(5):
                if backfill_legacy_batch(10).status == "completed":
                    break
            with database.get_db() as db:
                self.assertEqual(db.execute("SELECT reason FROM curation_story_metrics_dirty").fetchone()[0], "document_update")
                db.execute("DELETE FROM curation_story_metrics_dirty")
            enqueue_legacy_curation_item(1)
            process_one_legacy_curation_import(worker_id="story-metrics-test")
            with database.get_db() as db:
                self.assertEqual(db.execute("SELECT reason FROM curation_story_metrics_dirty").fetchone()[0], "publication_insert")


if __name__ == "__main__":
    unittest.main()
