import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import company_match, database, db_admin, stories
from app.crawler.runner import insert_item
from app.stories import refresh_derived

T0 = datetime.now(timezone.utc) - timedelta(hours=2)


class TopicQueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(database, "DB_PATH", Path(folder.name) / "app.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('feed','Feed','robot','rss')")
        titles = ["Acme Robotics unveils a humanoid arm for warehouses",
                  "Acme Robotics unveils a humanoid arm for warehouses today",
                  "Nvidia ships a new robotics computer for factories"]
        for minute, title in enumerate(titles):
            insert_item("feed", dict(title=title, url=f"https://example.com/{minute}",
                                     published_at=(T0 + timedelta(minutes=minute)).isoformat()))
        refresh_derived()

    def state(self):
        with database.get_db() as db:
            return {
                "events": db.execute("SELECT item_id,story_id,match_reason,match_score FROM story_items ORDER BY item_id").fetchall(),
                "acme": [row[0] for row in db.execute("SELECT item_id FROM item_topics WHERE topic_slug='acme' ORDER BY item_id")],
                "queues": (db.execute("SELECT COUNT(*) FROM derived_dirty").fetchone()[0],
                           db.execute("SELECT COUNT(*) FROM topic_dirty").fetchone()[0]),
            }

    def follow(self):
        with database.get_db() as db:
            db.execute("""INSERT INTO companies(slug,name,market,aliases)
                          VALUES('acme','Acme Robotics','US','["Acme Robotics"]')""")

    def test_a_watchlist_change_reassigns_topics_without_matching_events_again(self):
        before = self.state()
        self.follow()
        result = refresh_derived()
        after = self.state()
        self.assertEqual((result["processed"], result["topics"], result["remaining"]), (0, 3, 0))
        self.assertEqual([tuple(row) for row in after["events"]], [tuple(row) for row in before["events"]])
        self.assertEqual(len(after["acme"]), 2)
        self.assertEqual(after["queues"], (0, 0))

    def test_topics_are_reassigned_in_chunks(self):
        self.follow()
        with patch.object(stories, "TOPIC_CHUNK", 2):
            result = refresh_derived(max_chunks=1)
            self.assertEqual((result["topics"], result["remaining"]), (2, 1))
            result = refresh_derived()
        self.assertEqual((result["topics"], result["chunks"], result["remaining"]), (1, 1, 0))
        self.assertEqual(len(self.state()["acme"]), 2)

    def test_an_item_queued_for_both_is_placed_once(self):
        self.follow()
        with database.get_db() as db:
            first = db.execute("SELECT MIN(id) FROM items").fetchone()[0]
            db.execute("INSERT INTO derived_dirty(item_id) VALUES(?)", (first,))
        result = refresh_derived()
        # The first item anchors an event, so both of its articles are placed (topics included);
        # only the third is left to the topic pass.
        self.assertEqual((result["processed"], result["topics"]), (2, 1))
        self.assertEqual(self.state()["queues"], (0, 0))


class TopicQueueMigrationTests(unittest.TestCase):
    def test_migration_56_adds_only_the_topic_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            with database.get_db(Path(folder) / "schema55.db") as db:
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:55])
                before = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
                self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:56]), (56,))
                after = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        self.assertEqual(after - before, {"topic_dirty"})


if __name__ == "__main__":
    unittest.main()
