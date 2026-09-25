import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database
from app.curation_search import advance_search_index
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import enqueue_legacy_curation_item, process_one_legacy_curation_import
from app.web import routes


class CurationSearchQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        for target, name, value in (
            (database, "DB_PATH", self.path),
            (config, "BLOB_PATH", self.path.parent / "blobs"),
            (routes, "CURATION_READ_ENABLED", True),
            (routes, "CURATION_SEARCH_ENABLED", True),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
        self.client = TestClient(routes.app)

    def _item(self, item_id: int, title: str, translated: str = "", summary: str = "", tmt: int = 1):
        with database.get_db() as db:
            db.execute("""INSERT INTO items(id,source_id,url,title,title_zh,summary,channel,tmt,
                          published_at,fetched_at) VALUES(?,?,?,?,?,?,'ai',?,?,?)""",
                       (item_id, 1, f"https://example.test/{item_id}", title, translated,
                        summary, tmt, "2026-09-10T09:00:00+00:00", "2026-09-10T09:01:00+00:00"))

    def _ready(self):
        for _ in range(50):
            if advance_search_index(10).status == "ready":
                return
        self.fail("search index did not become ready")

    def test_uses_current_publication_and_falls_back_when_dirty(self):
        self._item(1, "Original product launch", "正式译文甲乙", "正式摘要甲乙")
        for _ in range(5):
            if backfill_legacy_batch(10).status == "completed":
                break
        enqueue_legacy_curation_item(1)
        for _ in range(4):
            process_one_legacy_curation_import(worker_id="search-query-test")
        self._ready()
        result = self.client.get("/search", params={"q": "正式译文"})
        self.assertEqual([item["id"] for item in result.context["items"]], [1])
        self.assertEqual(result.context["notice"], "")
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='误导译文戊己' WHERE id=1")
        stale = self.client.get("/search", params={"q": "误导译文"})
        self.assertIn("正在更新", stale.context["notice"])
        self._ready()
        self.assertEqual(self.client.get("/search", params={"q": "误导译文"}).context["items"], [])
        self.assertEqual(len(self.client.get("/search", params={"q": "正式译文"}).context["items"]), 1)

    def test_pages_more_than_one_hundred_without_repeating_ids(self):
        for item_id in range(1, 106):
            self._item(item_id, f"Searchtoken item {item_id}")
        self._ready()
        found = []
        for page in range(1, 5):
            response = self.client.get("/search", params={"q": "Searchtoken", "page": page})
            self.assertEqual(response.status_code, 200)
            found.extend(item["id"] for item in response.context["items"])
            self.assertEqual(response.context["has_next"], page < 4)
        self.assertEqual(len(found), 105)
        self.assertEqual(len(set(found)), 105)
        self.assertEqual(self.client.get("/search", params={"q": "Searchtoken", "page": 5}).context["items"], [])

    def test_visibility_short_terms_and_query_bounds(self):
        self._item(1, "Great apple", tmt=1)
        self._item(2, "Other apple", tmt=0)
        self._ready()
        self.assertEqual([item["id"] for item in self.client.get("/search", params={"q": "app"}).context["items"]], [1])
        self.assertEqual([item["id"] for item in self.client.get("/search", params={"q": "ap"}).context["items"]], [1])
        self.assertEqual(self.client.get("/search", params={"q": "%"}).context["items"], [])
        self.assertEqual(self.client.get("/search", params={"q": "a" * 121}).status_code, 400)
        self.assertEqual(self.client.get("/search", params={"q": "apple", "page": 201}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
