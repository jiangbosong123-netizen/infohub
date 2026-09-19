import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.curation_search import advance_search_index
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import enqueue_legacy_curation_item, process_one_legacy_curation_import


class CurationSearchBuilderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        self.db_patch = patch.object(database, "DB_PATH", self.path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.blob_patch = patch.object(config, "BLOB_PATH", self.path.parent / "blobs")
        self.blob_patch.start()
        self.addCleanup(self.blob_patch.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            for item_id in (1, 2):
                db.execute("""INSERT INTO items(id,source_id,url,title,title_zh,summary,channel,
                              published_at,fetched_at) VALUES(?,?,?,?,?,?, 'ai',?,?)""",
                           (item_id, 1, f"https://example.test/{item_id}", f"Original {item_id}",
                            f"旧标题甲{item_id}", f"旧摘要乙{item_id}",
                            "2026-09-10T09:00:00+00:00", "2026-09-10T09:01:00+00:00"))

    def test_resumes_scan_then_replays_changed_item_without_duplicate_fts(self):
        first = advance_search_index(1)
        self.assertEqual((first.status, first.last_item_id, first.scanned), ("building", 1, 1))
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='新标题丙丁' WHERE id=1")
        second = advance_search_index(1)
        self.assertEqual((second.last_item_id, second.scanned), (2, 1))
        replay = advance_search_index(1)
        self.assertEqual((replay.refreshed, replay.status), (1, "ready"))
        self.assertEqual((replay.indexed_count, replay.dirty_remaining), (2, 0))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT title_display FROM curation_search_documents WHERE item_id=1").fetchone()[0], "新标题丙丁")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_fts WHERE curation_search_fts MATCH '新标题'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_fts WHERE curation_search_fts MATCH '旧标题'").fetchone()[0], 1)
            db.execute("INSERT INTO curation_search_fts(curation_search_fts,rank) VALUES('integrity-check',1)")
            indexed_at = db.execute("SELECT indexed_at FROM curation_search_documents WHERE item_id=1").fetchone()[0]
        again = advance_search_index(1)
        self.assertEqual((again.status, again.scanned, again.refreshed), ("ready", 0, 0))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT indexed_at FROM curation_search_documents WHERE item_id=1").fetchone()[0], indexed_at)

    def test_current_publications_replace_legacy_text_and_track_pointers(self):
        while advance_search_index(10).status != "ready":
            pass
        for _ in range(5):
            if backfill_legacy_batch(10).status == "completed":
                break
        enqueue_legacy_curation_item(1)
        for _ in range(4):
            self.assertIsNotNone(process_one_legacy_curation_import(worker_id="search-builder-test"))
        result = advance_search_index(10)
        self.assertEqual((result.status, result.refreshed), ("ready", 2))
        with database.get_db() as db:
            row = db.execute("""SELECT document_version_id,translation_publication_id,
                                     summary_publication_id,title_display,summary_display
                              FROM curation_search_documents WHERE item_id=1""").fetchone()
            self.assertIsNotNone(row["document_version_id"])
            self.assertIsNotNone(row["translation_publication_id"])
            self.assertIsNotNone(row["summary_publication_id"])
            self.assertEqual(row["title_display"], "旧标题甲1")
            self.assertEqual(row["summary_display"], "旧摘要乙1")

    def test_failure_rolls_back_cursor_and_dirty_acknowledgement(self):
        with patch("app.curation_search.published_curation", side_effect=RuntimeError("projection failed")):
            with self.assertRaisesRegex(RuntimeError, "projection failed"):
                advance_search_index(1)
        with database.get_db() as db:
            state = db.execute("SELECT status,generation,last_item_id FROM curation_search_state").fetchone()
            self.assertEqual(tuple(state), ("empty", 0, 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_documents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_search_dirty").fetchone()[0], 2)

    def test_new_item_after_ready_enters_index_through_dirty_queue(self):
        while advance_search_index(10).status != "ready":
            pass
        with database.get_db() as db:
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(3,1,'https://example.test/3','Late arrival','ai',
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
        report = advance_search_index(10)
        self.assertEqual((report.status, report.refreshed, report.indexed_count), ("ready", 1, 3))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT title_original FROM curation_search_documents WHERE item_id=3").fetchone()[0], "Late arrival")


if __name__ == "__main__":
    unittest.main()
