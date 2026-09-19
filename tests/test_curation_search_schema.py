import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import enqueue_legacy_curation_item, process_one_legacy_curation_import


class CurationSearchSchemaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"

    def test_version_16_upgrade_adds_empty_index_without_scanning_items(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:-1])
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/a','Original','ai',
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.applied_versions, (17,))
        self.assertEqual(db_admin.verify_database(self.path, require_current=True).schema_version, 17)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_documents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status,last_item_id FROM curation_search_state").fetchone(), ("empty", 0))
            self.assertEqual(db.execute("SELECT title FROM items WHERE id=1").fetchone()[0], "Original")

    def test_derived_fts_and_dirty_queue_follow_changes(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/a','Original','ai',
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
            self.assertEqual(db.execute("SELECT reason FROM curation_search_dirty WHERE item_id=1").fetchone()[0], "item_insert")
            db.execute("""INSERT INTO curation_search_documents(
                item_id,index_schema_version,title_original,title_display,summary_display,indexed_at)
                VALUES(1,'curation-search-v1','Original','旧版标题','旧版摘要','2026-09-19T00:00:00Z')""")
            self.assertEqual(db.execute("SELECT rowid FROM curation_search_fts WHERE curation_search_fts MATCH '旧版标题'").fetchone()[0], 1)
            db.execute("UPDATE curation_search_documents SET title_display='新版标题' WHERE item_id=1")
            self.assertIsNone(db.execute("SELECT rowid FROM curation_search_fts WHERE curation_search_fts MATCH '旧版标题'").fetchone())
            self.assertEqual(db.execute("SELECT rowid FROM curation_search_fts WHERE curation_search_fts MATCH '新版标题'").fetchone()[0], 1)
            db.execute("INSERT INTO curation_search_fts(curation_search_fts,rank) VALUES('integrity-check',1)")
            db.execute("DELETE FROM items WHERE id=1")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_documents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_dirty").fetchone()[0], 0)
            db.execute("INSERT INTO curation_search_fts(curation_search_fts,rank) VALUES('integrity-check',1)")

    def test_document_backfill_and_publication_mark_index_dirty(self):
        with patch.object(database, "DB_PATH", self.path), patch.object(config, "BLOB_PATH", self.path.parent / "blobs"):
            database.init_schema()
            with database.get_db() as db:
                db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
                db.execute("""INSERT INTO items(id,source_id,url,title,title_zh,summary,channel,
                              score,tmt,published_at,fetched_at)
                              VALUES(1,1,'https://example.test/a','Original','译文标题','摘要文本',
                              'ai',80,1,'2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
            for _ in range(5):
                if backfill_legacy_batch(10).status == "completed":
                    break
            with database.get_db() as db:
                self.assertEqual(db.execute("SELECT reason FROM curation_search_dirty WHERE item_id=1").fetchone()[0], "document_update")
                db.execute("DELETE FROM curation_search_dirty WHERE item_id=1")
            enqueue_legacy_curation_item(1)
            process_one_legacy_curation_import(worker_id="search-fixture")
            with database.get_db() as db:
                self.assertEqual(db.execute("SELECT reason FROM curation_search_dirty WHERE item_id=1").fetchone()[0], "publication_insert")


if __name__ == "__main__":
    unittest.main()
