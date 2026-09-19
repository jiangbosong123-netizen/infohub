import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.curation_hot_metrics import advance_hot_metrics
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import enqueue_legacy_curation_item, process_one_legacy_curation_import


class CurationHotMetricsBuilderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        for target, name, value in (
            (database, "DB_PATH", self.path),
            (config, "BLOB_PATH", self.path.parent / "blobs"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        database.init_schema()
        current = datetime.now(timezone.utc).isoformat()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            for item_id in (1, 2):
                db.execute("""INSERT INTO items(id,source_id,url,title,title_zh,summary,channel,
                              score,tmt,published_at,fetched_at) VALUES(?,?,?,?,?,?,'ai',?,?,?,?)""",
                           (item_id, 1, f"https://openai.com/{item_id}", f"Original {item_id}",
                            f"译题甲乙{item_id}", f"摘要甲乙{item_id}", 80 if item_id == 1 else 60, 1,
                            current, current))
            db.execute("""INSERT INTO stories(id,title,channel,url,first_at,last_at)
                          VALUES('story-one','Legacy','ai','https://openai.com/1',?,?)""",
                       (current, current))
            db.execute("INSERT INTO story_items(item_id,story_id) VALUES(1,'story-one')")
            db.execute("INSERT INTO story_items(item_id,story_id) VALUES(2,'story-one')")

    def _ready(self):
        for _ in range(10):
            result = advance_hot_metrics(1)
            if result.status == "ready":
                return result
        self.fail("hot metrics did not become ready")

    def test_visible_counts_publishers_and_dirty_refresh(self):
        first = advance_hot_metrics(1)
        self.assertEqual((first.status, first.scanned, first.last_story_id), ("building", 1, "story-one"))
        self.assertEqual(self._ready().indexed_count, 1)
        with database.get_db() as db:
            row = db.execute("SELECT * FROM curation_story_metrics WHERE story_id='story-one'").fetchone()
            self.assertEqual((row["visible_item_count"], row["publisher_count"]), (2, 1))
            self.assertEqual(row["title_display"], "译题甲乙1")
            initial_heat = row["heat"]
            self.assertGreater(initial_heat, 0)
            db.execute("UPDATE items SET tmt=0 WHERE id=1")
        refreshed = advance_hot_metrics(1)
        self.assertEqual((refreshed.status, refreshed.refreshed, refreshed.dirty_remaining), ("ready", 1, 0))
        with database.get_db() as db:
            row = db.execute("SELECT * FROM curation_story_metrics WHERE story_id='story-one'").fetchone()
            self.assertEqual((row["visible_item_count"], row["publisher_count"]), (1, 1))
            self.assertEqual(row["representative_item_id"], 2)
            self.assertLess(row["heat"], initial_heat)
            db.execute("DELETE FROM story_items WHERE item_id=2")
        advance_hot_metrics(1)
        with database.get_db() as db:
            row = db.execute("SELECT visible_item_count,heat,last_visible_at FROM curation_story_metrics").fetchone()
            self.assertEqual(tuple(row), (0, 0.0, None))
            db.execute("DELETE FROM items WHERE id=1")
            self.assertEqual(db.execute("SELECT reason FROM curation_story_metrics_dirty").fetchone()[0], "member_delete")
        self.assertEqual(advance_hot_metrics(1).dirty_remaining, 0)

    def test_current_imported_score_and_title_survive_legacy_field_changes(self):
        for _ in range(5):
            if backfill_legacy_batch(10).status == "completed":
                break
        enqueue_legacy_curation_item(1)
        for _ in range(4):
            process_one_legacy_curation_import(worker_id="hot-test")
        self._ready()
        with database.get_db() as db:
            original = db.execute("SELECT heat,title_display FROM curation_story_metrics").fetchone()
            db.execute("UPDATE items SET score=10,title_zh='错误旧译文' WHERE id=1")
        advance_hot_metrics(1)
        with database.get_db() as db:
            after = db.execute("SELECT heat,title_display FROM curation_story_metrics").fetchone()
            self.assertEqual(after["title_display"], original["title_display"])
            self.assertEqual(after["heat"], original["heat"])

    def test_failed_batch_rolls_back_state_and_dirty_acknowledgement(self):
        with patch("app.curation_hot_metrics.published_curation", side_effect=RuntimeError("projection failed")):
            with self.assertRaisesRegex(RuntimeError, "projection failed"):
                advance_hot_metrics(1)
        with database.get_db() as db:
            self.assertEqual(tuple(db.execute("SELECT status,generation,last_story_id FROM curation_story_metrics_state").fetchone()), ("empty", 0, ""))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_story_metrics").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM curation_story_metrics_dirty").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
