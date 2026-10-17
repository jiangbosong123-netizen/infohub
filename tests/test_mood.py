import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, mood
from app.ai import pipeline
from app.crawler import runner
from app.web import routes

NOW = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)


class MoodTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "DB_PATH", Path(folder.name) / "app.db")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','info')")
        routes._mood_cache.clear()
        self.addCleanup(routes._mood_cache.clear)

    def add(self, n, score=50, mood_value=None, tmt=1, hours_ago=1):
        runner.insert_item("test", dict(url=f"https://e.test/{n}", title=f"Story {n}",
                                        published_at=NOW.isoformat()))
        with database.get_db() as db:
            item_id = db.execute("SELECT MAX(id) FROM items").fetchone()[0]
            db.execute("UPDATE items SET score=?, tmt=?, fetched_at=? WHERE id=?",
                       (score, tmt, (NOW - timedelta(hours=hours_ago)).isoformat(), item_id))
            if mood_value is not None:
                db.execute("INSERT INTO item_moods(item_id,mood,judged_at) VALUES(?,?,?)",
                           (item_id, mood_value, NOW.isoformat()))
        return item_id

    def summary(self):
        with database.get_db() as db:
            return mood.summary(db, NOW)

    def test_scoring_stores_a_valid_mood_and_ignores_anything_else(self):
        self.assertIn("mood", pipeline.SYSTEM_PROMPT)
        ids = [self.add(n, score=None) for n in range(4)]
        with database.get_db() as db:
            db.execute("UPDATE items SET score=NULL, tmt=NULL")
        verdicts = [dict(id=item_id, tmt=True, score=60, summary_zh="s", title_zh="t", reason="r", mood=value)
                    for item_id, value in zip(ids, (2, -1, 5, "1"))]
        with patch.object(config, "llm_enabled", return_value=True), \
                patch.object(pipeline, "_call_llm", return_value=verdicts):
            self.assertEqual(pipeline.process_pending(limit=10), 4)
        with database.get_db() as db:
            stored = dict(db.execute("SELECT item_id,mood FROM item_moods").fetchall())
        self.assertEqual(stored, {ids[0]: 2, ids[1]: -1})  # 5 is out of range, "1" is not an int

    def test_a_reused_judgement_brings_its_mood(self):
        title = "OpenAI ships a new reasoning model for developers"
        for n, value in ((1, 2), (2, None)):
            runner.insert_item("test", dict(url=f"https://e.test/r{n}", title=title, published_at=NOW.isoformat()))
        with database.get_db() as db:
            first, second = [row[0] for row in db.execute("SELECT id FROM items ORDER BY id")]
            db.execute("UPDATE items SET score=80, tmt=1, title_zh='t' WHERE id=?", (first,))
            db.execute("INSERT INTO item_moods(item_id,mood,judged_at) VALUES(?,2,?)", (first, NOW.isoformat()))
        with patch.object(config, "llm_enabled", return_value=True), \
                patch.object(pipeline, "_call_llm", side_effect=AssertionError("no model call for a repeat")):
            self.assertEqual(pipeline.process_pending(limit=10), 1)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT mood FROM item_moods WHERE item_id=?", (second,)).fetchone()[0], 2)

    def test_the_face_sleeps_until_enough_items_are_judged(self):
        for n in range(mood.MIN_ITEMS - 1):
            self.add(n, mood_value=2)
        self.assertEqual(self.summary()["level"], "sleepy")
        self.add(99, mood_value=2)
        self.assertEqual(self.summary()["level"], "great")

    def test_important_news_weighs_more_and_old_or_hidden_news_does_not_count(self):
        for n in range(20):
            self.add(n, score=20, mood_value=1)          # many small good things
        big = self.add(100, score=95, mood_value=-2)     # one big bad thing
        self.add(101, score=99, mood_value=2, tmt=0)     # hidden as not relevant
        self.add(102, score=99, mood_value=2, hours_ago=30)  # older than a day
        result = self.summary()
        self.assertEqual(result["judged"], 21)
        self.assertEqual(result["counts"], {"good": 20, "calm": 0, "bad": 1})
        self.assertEqual(result["value"], round((20 * 20 - 2 * 95) / (20 * 20 + 95), 2))  # 0.42
        self.assertEqual(result["level"], "good")
        self.assertEqual(result["worst"]["id"], big)

    def test_every_page_shows_the_face_and_survives_a_missing_table(self):
        for n in range(25):
            self.add(n, mood_value=-2 if n % 5 else 0)
        with patch.object(mood, "datetime") as clock:
            clock.now.return_value = NOW
            page = TestClient(routes.app).get("/hot").text
        self.assertIn('class="mood mood-sad"', page)
        self.assertIn("利空 20", page)
        self.assertIn("仅供参考", page)
        routes._mood_cache.clear()
        with database.get_db() as db:
            db.execute("DROP TABLE item_moods")
        page = TestClient(routes.app).get("/hot")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('class="mood ', page.text)


if __name__ == "__main__":
    unittest.main()
