import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import company_match, config, database
from app.crawler import runner
from app.web import routes

STATIC = Path(routes.__file__).resolve().parent / "static"


class PortalAppTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for target, name, value in ((database, "DB_PATH", self.root / "app.db"),
                                    (config, "DB_PATH", self.root / "app.db"),
                                    (config, "BLOB_PATH", self.root / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        routes._overview_cache.clear()
        self.addCleanup(routes._overview_cache.clear)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('wire','Wire','ai','rss','info')")
        self.number = 0
        self.client = TestClient(routes.app)

    def add(self, companies, tmt=None):
        self.number += 1
        self.assertTrue(runner.insert_item("wire", dict(
            url=f"https://example.com/{self.number}", title=f"Story number {self.number} about AI",
            companies=companies)))
        if tmt is not None:
            with database.get_db() as db:
                db.execute("UPDATE items SET tmt=? WHERE id=(SELECT MAX(id) FROM items)", (tmt,))

    def test_the_side_column_counts_the_last_day(self):
        self.add(["openai"])
        self.add(["openai", "anthropic"])
        self.add([])
        self.add(["openai"], tmt=0)  # hidden as not relevant: counted as collected only
        overview = routes._home_overview()
        self.assertEqual((overview["total"], overview["relevant"]), (4, 3))
        self.assertEqual([(c["slug"], c["count"]) for c in overview["companies"]],
                         [("openai", 2), ("anthropic", 1)])
        page = self.client.get("/?mode=all").text
        self.assertIn("24 小时概览", page)
        self.assertIn('href="/topics/openai"', page)

    def test_the_overview_is_cached_per_database(self):
        self.add(["openai"])
        self.assertEqual(routes._home_overview()["total"], 1)
        self.add(["openai"])
        self.assertEqual(routes._home_overview()["total"], 1)  # within the minute: cached
        with patch.object(routes, "OVERVIEW_TTL_SECONDS", 0):
            self.assertEqual(routes._home_overview()["total"], 2)
        other = self.root / "other.db"
        with patch.object(database, "DB_PATH", other), patch.object(config, "DB_PATH", other):
            database.init_schema()
            self.assertEqual(routes._home_overview()["total"], 0)

    def test_pages_carry_the_new_name_and_the_installable_app_tags(self):
        page = self.client.get("/").text
        self.assertIn("<title>精选 — Infohub</title>", page)
        self.assertIn('class="brand-name">Infohub<', page)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', page)
        self.assertIn('href="/static/icons/apple-touch-icon.png"', page)
        self.assertIn(f'href="/static/style.css?v={config.APP_VERSION}"', page)
        self.assertIn('class="tabbar"', page)
        self.assertNotIn("行业情报站", page)

    def test_the_manifest_points_at_icons_that_exist(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/manifest+json")
        manifest = json.loads(response.text)
        self.assertEqual((manifest["name"], manifest["display"], manifest["scope"]), ("Infohub", "standalone", "/"))
        self.assertTrue(any(icon.get("purpose") == "maskable" for icon in manifest["icons"]))
        for icon in manifest["icons"]:
            with self.subTest(icon=icon["src"]):
                self.assertTrue((STATIC / icon["src"].removeprefix("/static/")).is_file())
                self.assertEqual(self.client.get(icon["src"]).status_code, 200)

    def test_the_service_worker_only_adds_an_offline_page(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(response.headers["cache-control"], "no-cache")
        script = response.text
        self.assertIn(f"infohub-shell-{config.APP_VERSION}", script)  # a new release replaces the cache
        self.assertIn("event.request.mode !== 'navigate'", script)  # news and API calls are never cached
        self.assertEqual(self.client.get("/static/offline.html").status_code, 200)


if __name__ == "__main__":
    unittest.main()
