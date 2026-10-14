import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import company_match, database, stories
from app.crawler.runner import insert_item
from app.stories import refresh_derived

T0 = datetime.now(timezone.utc) - timedelta(hours=2)


class RefreshChunkTests(unittest.TestCase):
    def setUp(self):
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        pause = patch.object(stories, "REFRESH_PAUSE_SECONDS", 0)
        pause.start()
        self.addCleanup(pause.stop)

    def database(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(database, "DB_PATH", Path(folder.name) / "app.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('feed','Feed','ai','rss')")

    def insert(self):
        # Four events of three articles each, interleaved in time, so every chunk of three holds
        # articles of different events and each event spans several chunks.
        ids = {}
        for minute, (event, wording) in enumerate((e, w) for w in range(3) for e in range(4)):
            title = f"OpenAI launches coding agent {event} for enterprise developers" + (
                "", " today", " worldwide")[wording]
            insert_item("feed", dict(title=title, url=f"https://example.com/{event}/{wording}",
                                     companies=["openai"],
                                     published_at=(T0 + timedelta(minutes=minute)).isoformat()))
            with database.get_db() as db:
                ids[db.execute("SELECT max(id) FROM items").fetchone()[0]] = event
        return ids

    def partition(self, ids):
        with database.get_db() as db:
            rows = db.execute("SELECT item_id, story_id FROM story_items").fetchall()
        events = {}
        for item_id, story_id in rows:
            events.setdefault(story_id, set()).add(ids[item_id])
        return sorted(sorted(group) for group in events.values())

    def test_chunks_reach_the_same_events_as_one_transaction(self):
        results = []
        for chunk in (None, 3):
            with self.subTest(chunk=chunk):
                self.database()
                ids = self.insert()
                result = refresh_derived(chunk=chunk)
                results.append(self.partition(ids))
                self.assertEqual((result["processed"], result["chunks"]), (12, 1 if chunk is None else 4))
        self.assertEqual(results[0], [[0], [1], [2], [3]])
        self.assertEqual(results[1], results[0])

    def test_the_crawl_refresh_indexes_one_chunk_and_leaves_a_backlog(self):
        from app import ranking
        self.database()
        self.insert()
        with patch.object(stories.refresh_derived, "__defaults__", (3, None)):  # chunks of 3
            ranking.rebuild_clusters()
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM derived_dirty").fetchone()[0], 9)
        result = refresh_derived(chunk=3)
        self.assertEqual((result["processed"], result["chunks"], result["remaining"]), (9, 3, 0))

    def test_a_chunk_past_its_time_commits_what_it_placed(self):
        self.database()
        ids = self.insert()
        with patch.object(stories, "REFRESH_CHUNK_SECONDS", 0):  # one item, then out of time
            result = refresh_derived(chunk=5)
        self.assertEqual((result["processed"], result["chunks"], result["remaining"]), (12, 12, 0))
        self.assertEqual(self.partition(ids), [[0], [1], [2], [3]])

    def test_the_refresh_pauses_between_chunks_for_other_writers(self):
        self.database()
        self.insert()
        with patch.object(stories, "REFRESH_PAUSE_SECONDS", 0.25), patch.object(stories.time, "sleep") as sleep:
            result = refresh_derived(chunk=5)
        self.assertEqual(result["chunks"], 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.25,), (0.25,)])

    def test_items_queued_during_the_last_chunk_wait_for_the_next_refresh(self):
        self.database()
        self.insert()
        original = stories.assign_topics
        added = []

        def another_writer(db, row, definitions):  # a crawl inserting while the chunk runs
            if not added:
                db.execute("""INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
                              SELECT id,'https://example.com/late','OpenAI ships a late item','ai',?,?
                              FROM sources""", (T0.isoformat(), T0.isoformat()))
                added.append(1)
            return original(db, row, definitions)

        with patch("app.stories.assign_topics", side_effect=another_writer):
            result = refresh_derived(chunk=20)
        self.assertEqual((result["processed"], result["chunks"], result["remaining"]), (12, 1, 1))
        self.assertEqual(refresh_derived()["processed"], 1)

    def test_the_crawl_refresh_leaves_a_backlog_to_the_ai_job(self):
        from app import ranking
        self.database()
        self.insert()
        with patch.object(stories, "REFRESH_CHUNK", 3):  # 12 queued: more than one chunk
            ranking.rebuild_clusters()
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM derived_dirty").fetchone()[0], 12)

    def test_a_failed_chunk_keeps_the_chunks_before_it(self):
        self.database()
        ids = sorted(self.insert())
        original = stories.assign_topics

        def fail_on_fifth(db, row, definitions):
            if row["id"] == ids[4]:
                raise ValueError("test failure")
            return original(db, row, definitions)

        with patch("app.stories.assign_topics", side_effect=fail_on_fifth), self.assertRaises(ValueError):
            refresh_derived(chunk=2)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM story_items").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM derived_dirty").fetchone()[0], 8)
        self.assertEqual(refresh_derived(chunk=2)["processed"], 8)


if __name__ == "__main__":
    unittest.main()
