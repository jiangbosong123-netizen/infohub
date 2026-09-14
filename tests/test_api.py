import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import company_match, database
from app.ai.audit import CURATION_VERSION, save_result
from app.crawler import runner
from app.stories import refresh_derived
from app.web.routes import app


class IntegrationApiTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(database, "DB_PATH", Path(folder.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(key,name,channel,type,tier)
                VALUES('test','Test Publisher','ai','rss','media')""")
            db.execute("""INSERT INTO companies(slug,name,market,aliases)
                VALUES('openai','OpenAI','PRIVATE','["OpenAI"]')""")
        self.client = TestClient(app)

    def item(self, number: int, **fields) -> int:
        raw = dict(url=f"https://example.com/{number}", title=f"OpenAI item {number}",
                   channel="ai", score=80, tmt=1,
                   published_at="2026-09-14T12:00:00+00:00")
        raw.update(fields)
        self.assertTrue(runner.insert_item("test", raw))
        with database.get_db() as db:
            return db.execute("SELECT MAX(id) FROM items").fetchone()[0]

    def test_item_cursor_filters_hidden_and_does_not_repeat(self):
        ids = [self.item(number) for number in range(3)]
        hidden = self.item(9, tmt=0)
        with database.get_db() as db:
            db.execute("UPDATE items SET tmt=0 WHERE id=?", (hidden,))
        first = self.client.get("/api/v1/items", params={"limit": 2}).json()
        self.assertEqual(len(first["data"]), 2)
        self.assertIsNotNone(first["pagination"]["next_cursor"])
        second = self.client.get("/api/v1/items", params={
            "limit": 2, "cursor": first["pagination"]["next_cursor"]}).json()
        returned = [row["id"] for row in first["data"] + second["data"]]
        self.assertEqual(set(returned), set(ids))
        self.assertNotIn(hidden, returned)
        self.assertEqual(len(returned), len(set(returned)))

    def test_item_detail_exposes_versioned_analysis(self):
        item_id = self.item(1)
        with database.get_db() as db:
            save_result(db, item_id=item_id, analysis_type="curation",
                        pipeline_version=CURATION_VERSION, model="test-model",
                        input_data={"title": "evidence"}, output_data={"score": 80})
            migration = db.execute("SELECT name FROM schema_migrations WHERE version=1").fetchone()
        self.assertIsNotNone(migration)
        response = self.client.get(f"/api/v1/items/{item_id}")
        self.assertEqual(response.status_code, 200)
        analysis = response.json()["data"]["analyses"][0]
        self.assertEqual(analysis["pipeline_version"], CURATION_VERSION)
        self.assertEqual(analysis["input"]["title"], "evidence")

    def test_story_topic_and_validation_endpoints(self):
        self.item(1)
        refresh_derived()
        stories = self.client.get("/api/v1/stories").json()["data"]
        topics = self.client.get("/api/v1/topics").json()["data"]
        self.assertEqual(len(stories), 1)
        self.assertTrue(any(topic["slug"] == "openai" for topic in topics))
        self.assertEqual(self.client.get("/api/v1/items", params={"channel":"invalid"}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/items", params={"cursor":"bad"}).status_code, 400)
