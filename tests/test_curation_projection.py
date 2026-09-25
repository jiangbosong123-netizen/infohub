import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database
from app.curation_projection import display_curation, published_curation
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import enqueue_legacy_curation_item, process_one_legacy_curation_import
from app.web import routes


class CurationProjectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for target, name, value in (
            (database, "DB_PATH", root / "app.db"),
            (config, "DB_PATH", root / "app.db"),
            (config, "BLOB_PATH", root / "blobs"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,title_zh,summary,raw_summary,
                       channel,score,tmt,reason,ai_cat,companies,published_at,fetched_at)
                       VALUES(1,1,'https://example.test/one','Original','旧译文','旧摘要',
                       'Original summary','ai',80,1,'旧理由','model','[]',
                       '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
        for _ in range(5):
            if backfill_legacy_batch(10).status == "completed":
                break

    def _import(self):
        enqueue_legacy_curation_item(1)
        for _ in range(4):
            self.assertIsNotNone(process_one_legacy_curation_import(worker_id="read-fixture"))

    def test_flag_off_and_missing_publications_keep_legacy(self):
        with database.get_db() as db:
            row = db.execute("SELECT i.*,s.name AS source_name FROM items i JOIN sources s ON s.id=i.source_id").fetchone()
            self.assertEqual(published_curation(db, [1]), {})
        with patch.object(routes, "CURATION_READ_ENABLED", False):
            item = routes._decorate([row])[0]
        self.assertEqual((item["title"], item["summary"], item["score"]), ("旧译文", "旧摘要", 80))
        self.assertFalse(item["curation_needs_review"])

    def test_current_publications_override_legacy_on_list_and_story_projection(self):
        self._import()
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='改过的旧译文',summary='改过的旧摘要',score=5,reason='改过的旧理由' WHERE id=1")
            row = db.execute("SELECT i.*,s.name AS source_name FROM items i JOIN sources s ON s.id=i.source_id").fetchone()
            tasks = published_curation(db, [1])[1]
        self.assertEqual(set(tasks), {"translation", "relevance", "summarization", "importance"})
        with patch.object(routes, "CURATION_READ_ENABLED", True):
            item = routes._decorate([row])[0]
        self.assertEqual((item["title"], item["summary"], item["score"], item["reason"]),
                         ("旧译文", "旧摘要", 80, "旧理由"))
        self.assertTrue(item["curation_needs_review"])
        story = display_curation(dict(row), tasks)
        self.assertEqual((story["title_zh"], story["summary"], story["score"]), ("旧译文", "旧摘要", 80))

    def test_rejected_pointer_suppresses_stale_legacy_field(self):
        self._import()
        with database.get_db() as db:
            row = db.execute("SELECT * FROM items WHERE id=1").fetchone()
            tasks = published_curation(db, [1])[1]
        tasks["summarization"] = {"status": "unavailable"}
        display = display_curation(dict(row), tasks)
        self.assertEqual(display["summary"], "")
        self.assertEqual(display["title_zh"], "旧译文")

    def test_only_current_document_version_is_used(self):
        self._import()
        with database.get_db() as db:
            db.execute("UPDATE documents SET status='withdrawn' WHERE legacy_item_id=1")
            self.assertEqual(published_curation(db, [1]), {})

    def test_portal_flag_controls_visible_cards(self):
        self._import()
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='当前旧值' WHERE id=1")
        client = TestClient(routes.app)
        with patch.object(routes, "CURATION_READ_ENABLED", False):
            legacy = client.get("/?mode=all")
        with patch.object(routes, "CURATION_READ_ENABLED", True):
            current = client.get("/?mode=all")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(current.status_code, 200)
        self.assertIn("当前旧值", legacy.text)
        self.assertIn("旧译文", current.text)
        self.assertIn("AI 待复核", current.text)

    def test_feed_filter_score_and_topic_use_published_values_before_pagination(self):
        self._import()
        with database.get_db() as db:
            db.execute("UPDATE items SET tmt=0,score=5,ai_cat='product' WHERE id=1")
            db.execute("""INSERT INTO topics(slug,name,group_key,description,rules,position)
                          VALUES('test-topic','测试主题','ai','','{}',1)""")
            db.execute("INSERT INTO item_topics(item_id,topic_slug,evidence) VALUES(1,'test-topic','fixture')")
        client = TestClient(routes.app)
        with patch.object(routes, "CURATION_READ_ENABLED", False), patch.object(routes, "CURATED_FEED_ENABLED", True):
            self.assertEqual(routes._query_items(mode="selected"), [])
            self.assertEqual(client.get("/topics/test-topic").context["topic"]["total"], 0)
        with patch.object(routes, "CURATION_READ_ENABLED", True), patch.object(routes, "CURATED_FEED_ENABLED", True):
            self.assertEqual([r["id"] for r in routes._query_items(mode="selected", cat="model", limit=1)], [1])
            self.assertEqual(routes._query_items(mode="all", cat="product"), [])
            topic = client.get("/topics/test-topic")
            self.assertEqual(topic.status_code, 200)
            self.assertEqual((topic.context["topic"]["total"], topic.context["topic"]["selected"]), (1, 1))
            self.assertEqual(topic.context["days"][0]["rows"][0]["score"], 80)

    def test_saved_and_story_visibility_use_current_publication(self):
        self._import()
        now = datetime.now(timezone.utc).isoformat()
        with database.get_db() as db:
            db.execute("UPDATE items SET tmt=0 WHERE id=1")
            db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,source_count,item_count,first_at,last_at)
                          VALUES('story-one',1,'Story','ai','https://example.test/one',1,1,?,?)""", (now, now))
            db.execute("""INSERT INTO story_items(item_id,story_id,match_reason,match_score)
                          VALUES(1,'story-one','fixture',1.0)""")
        client = TestClient(routes.app)
        with patch.object(routes, "CURATION_READ_ENABLED", False):
            self.assertEqual(client.get("/saved?ids=1").context["days"], [])
            self.assertEqual(client.get("/story/story-one").status_code, 404)
            self.assertEqual(routes._top_clusters(channel="ai"), [])
        with patch.object(routes, "CURATION_READ_ENABLED", True):
            saved = client.get("/saved?ids=1")
            story = client.get("/story/story-one")
            hot = routes._top_clusters(channel="ai")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.context["days"][0]["rows"][0]["id"], 1)
        self.assertEqual(story.status_code, 200)
        self.assertEqual(story.context["story"]["item_count"], 1)
        self.assertEqual(story.context["reports"][0]["title_zh"], "旧译文")
        self.assertEqual([row["id"] for row in hot], ["story-one"])


if __name__ == "__main__":
    unittest.main()
