import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.crawler.runner import insert_item
from app.curation_hot_metrics import advance_hot_metrics
from app.curation_hot_query import hot_metrics_usable
from app.stories import refresh_derived

WATCHED = {
    "anchor_item_id": 2, "title": "Changed", "url": "https://e.test/changed",
    "channel": "stock", "first_at": "2026-01-01T00:00:00+00:00",
    "last_at": "2026-12-31T00:00:00+00:00", "redirect_to": "other",
}


def queued(db) -> list[str]:
    return [row[0] for row in db.execute("SELECT story_id FROM curation_story_metrics_dirty")]


class StoryMetricsQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for target, name, value in ((database, "DB_PATH", self.path), (config, "DB_PATH", self.path),
                                    (config, "BLOB_PATH", self.path.parent / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)

    def test_rewriting_the_same_values_does_not_queue_the_story(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
            for item_id in (1, 2):
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                              VALUES(?,1,?,'T','ai','2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00')""",
                           (item_id, f"https://e.test/{item_id}"))
            for story in ("story", "other"):
                db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,first_at,last_at)
                              VALUES(?,1,'Title','ai','https://e.test/1',
                              '2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00')""", (story,))
            db.execute("DELETE FROM curation_story_metrics_dirty")
            db.execute(f"""UPDATE stories SET {','.join(f'{c}={c}' for c in WATCHED)},heat=heat+1
                           WHERE id='story'""")
            self.assertEqual(queued(db), [])
            for column, value in WATCHED.items():
                with self.subTest(column=column):
                    db.execute(f"UPDATE stories SET {column}=? WHERE id='story'", (value,))
                    self.assertEqual(queued(db), ["story"])
                    db.execute("DELETE FROM curation_story_metrics_dirty")

    def test_derived_refresh_leaves_ready_hot_metrics_usable(self):
        database.init_schema()
        now = datetime.now(timezone.utc).isoformat()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('test','Test feed','ai','rss')")
        for number, title in enumerate(("OpenAI launches a coding agent for developers",
                                        "OpenAI launches a coding agent for developers today",
                                        "Nvidia reports record quarterly data center revenue")):
            self.assertTrue(insert_item("test", dict(
                title=title, url=f"https://e.test/{number}", published_at=now, companies=["openai"])))
        refresh_derived()
        for _ in range(10):
            if advance_hot_metrics(100).status == "ready":
                break
        with database.get_db() as db:
            self.assertEqual(queued(db), [])
            self.assertTrue(hot_metrics_usable(db))
        # Nothing changed: the refresh still rewrites each recent story to decay its heat.
        refresh_derived()
        with database.get_db() as db:
            self.assertEqual(queued(db), [])
            self.assertTrue(hot_metrics_usable(db))

    def test_migration_49_rewrites_only_the_story_update_trigger(self):
        with database.get_db(self.path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:48])
        with sqlite3.connect(self.path) as db:
            before = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        # Compare against exactly schema 49; later migrations may redefine other triggers.
        with database.get_db(self.path) as db:
            self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:49]), (49,))
        with sqlite3.connect(self.path) as db:
            after = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        self.assertEqual(set(after), set(before))
        self.assertEqual({name for name in after if after[name] != before[name]}, {"curation_story_update"})


if __name__ == "__main__":
    unittest.main()
