import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database
from app.curation_hot_metrics import advance_hot_metrics
from app.web import routes


class CurationHotQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        for target, name, value in (
            (database, "DB_PATH", self.path),
            (config, "BLOB_PATH", self.path.parent / "blobs"),
            (routes, "CURATION_READ_ENABLED", True),
            (routes, "CURATION_HOT_ENABLED", True),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        database.init_schema()
        current = datetime.now(timezone.utc).isoformat()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            for item_id, host, score in ((1, "openai.com", 80), (2, "openai.com", 60), (3, "anthropic.com", 70)):
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,published_at,fetched_at)
                              VALUES(?,1,?,'Headline','ai',?,1,?,?)""",
                           (item_id, f"https://{host}/{item_id}", score, current, current))
            for story_id, anchor, old_heat in (("first", 1, 0.01), ("second", 3, 9.0)):
                db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,heat,source_count,
                              item_count,first_at,last_at) VALUES(?,?,?,'ai',?,?,9,9,?,?)""",
                           (story_id, anchor, "Old headline", f"https://example.test/{story_id}", old_heat,
                            current, current))
            for item_id, story_id in ((1, "first"), (2, "first"), (3, "second")):
                db.execute("INSERT INTO story_items(item_id,story_id) VALUES(?,?)", (item_id, story_id))
            db.execute("""INSERT INTO topics(slug,name,group_key,description,rules,position)
                          VALUES('topic-a','Topic A','ai','','{}',1)""")
            db.execute("INSERT INTO item_topics(item_id,topic_slug,evidence) VALUES(1,'topic-a','{}')")
        self.client = TestClient(routes.app)

    def _ready(self):
        for _ in range(10):
            if advance_hot_metrics(100).status == "ready":
                return
        self.fail("hot metrics did not become ready")

    def test_ready_projection_uses_visible_metrics_and_topic_filter(self):
        self._ready()
        rows = routes._top_clusters(10)
        self.assertEqual([row["id"] for row in rows], ["first", "second"])
        self.assertEqual((rows[0]["item_count"], rows[0]["source_count"]), (2, 1))
        self.assertFalse(rows[0]["metrics_pending"])
        self.assertEqual([row["id"] for row in routes._top_clusters(10, channel="ai", topic="topic-a")], ["first"])
        response = self.client.get("/hot")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("旧口径数值", response.text)
        with database.get_db() as db:
            db.execute("UPDATE curation_story_metrics SET computed_at=? WHERE story_id='first'",
                       ((datetime.now(timezone.utc) - timedelta(hours=18)).isoformat(),))
        self.assertEqual(routes._top_clusters(10)[0]["id"], "second")

    def test_dirty_projection_falls_back_with_visible_notice(self):
        self._ready()
        with database.get_db() as db:
            db.execute("UPDATE items SET tmt=0 WHERE id=1")
        fallback = routes._top_clusters(10)
        self.assertTrue(fallback[0]["metrics_pending"])
        self.assertEqual(fallback[0]["item_count"], 9)
        self.assertIn("旧口径数值", self.client.get("/hot").text)
        self._ready()
        current = routes._top_clusters(10)
        self.assertFalse(current[0]["metrics_pending"])
        self.assertEqual(next(row for row in current if row["id"] == "first")["item_count"], 1)

    def test_mixed_channel_story_uses_visible_member_channel(self):
        self._ready()
        current = datetime.now(timezone.utc).isoformat()
        with database.get_db() as db:
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,published_at,fetched_at)
                          VALUES(4,1,'https://openai.com/4','Robot report','robot',75,1,?,?)""",
                       (current, current))
            db.execute("INSERT INTO story_items(item_id,story_id) VALUES(4,'first')")
        self._ready()
        self.assertEqual([row["id"] for row in routes._top_clusters(10, channel="robot")], ["first"])


if __name__ == "__main__":
    unittest.main()
