import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import company_match, config, database
from app.crawler import googlenews, rss_source, runner

NOW = datetime.now(timezone.utc)
TITLE = "Major updates for Apple's software platforms are now available"


def candidate(link, publisher="Apple", title=TITLE, hours=0):
    return dict(url=link, title=title, summary="", companies=["apple"], event_type="", official=0,
                published_at=(NOW - timedelta(hours=10 - hours)).isoformat(), extra=dict(publisher=publisher),
                observed_at=NOW.isoformat())


class GoogleNewsRepeatTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        for target, name, value in ((database, "DB_PATH", root / "app.db"), (config, "DB_PATH", root / "app.db"),
                                    (config, "BLOB_PATH", root / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        runner.upsert_sources()
        self.sources = {s["key"]: s for s in runner.all_sources()}

    def run_source(self, key, candidates):
        kind = self.sources[key]["type"]
        with patch.dict(runner.FETCHERS, {kind: lambda _source: candidates}), \
                patch("app.stories.refresh_derived", lambda: None):
            return runner.run_source(self.sources[key])

    def items(self):
        with database.get_db() as db:
            return [dict(row) for row in db.execute("SELECT id,url,title,published_at FROM items ORDER BY id")]

    def test_a_resurfaced_copy_is_the_same_item(self):
        self.run_source("google-news", [candidate("https://news.google.com/rss/articles/A")])
        first = self.items()
        self.run_source("google-news", [candidate("https://news.google.com/rss/articles/B", hours=8),
                                        candidate("https://news.google.com/rss/articles/C", hours=9)])
        self.assertEqual(self.items(), first)  # no new card, and the time did not move forward
        with database.get_db() as db:
            locators = db.execute("SELECT COUNT(*) FROM document_locators").fetchone()[0]
            documents = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.assertEqual((documents, locators), (1, 3))  # every sighting is kept as evidence

    def test_other_publishers_older_copies_and_other_feeds_stay_separate(self):
        self.run_source("google-news", [candidate("https://news.google.com/rss/articles/A")])
        self.run_source("google-news", [candidate("https://news.google.com/rss/articles/B", publisher="9to5Mac")])
        self.assertEqual(len(self.items()), 2)
        with database.get_db() as db:
            db.execute("UPDATE items SET fetched_at=?", ((NOW - timedelta(days=8)).isoformat(),))
        self.run_source("google-news", [candidate("https://news.google.com/rss/articles/C")])
        self.assertEqual(len(self.items()), 3)  # a week later it is news again
        self.run_source("techcrunch", [dict(url="https://techcrunch.com/apple-updates", title=TITLE, summary="s",
                                            published_at=NOW.isoformat())])
        self.assertEqual(len(self.items()), 4)  # only Google News re-surfaces copies under new links

    def test_a_trailing_slash_is_the_same_page(self):
        self.run_source("techcrunch", [dict(url="https://techcrunch.com/2026/10/13/story/", title="Story", summary="s",
                                            published_at=NOW.isoformat())])
        self.run_source("techcrunch", [dict(url="https://techcrunch.com/2026/10/13/story", title="Story", summary="s",
                                            published_at=NOW.isoformat())])
        self.assertEqual(len(self.items()), 1)

    def test_titles_lose_publisher_suffixes_and_stray_entities(self):
        self.assertEqual(googlenews._clean_title("Apple sets event date - KELO-AM", "KELO-AM"), "Apple sets event date")
        self.assertEqual(googlenews._clean_title("Nvidia beats - tech-insider.org", "tech-insider.org"), "Nvidia beats")
        self.assertEqual(googlenews._clean_title("Tesla recalls cars - Reuters"), "Tesla recalls cars")
        self.assertEqual(googlenews._clean_title("AT&amp;T and Amazon - CNBC", "CNBC"), "AT&T and Amazon")
        feed = ("<?xml version='1.0'?><rss version='2.0'><channel><title>V</title><item>"
                "<title>&amp;#8216;Gears of War&amp;#8217; launches</title><link>https://www.theverge.com/x</link>"
                "<description>d</description></item></channel></rss>")
        response = SimpleNamespace(content=feed.encode(), status_code=200, headers={})
        with patch.object(rss_source.http, "fetch", return_value=response):
            rows = rss_source.fetch_rss(dict(key="verge", url="https://www.theverge.com/rss", channel="ai"))
        self.assertEqual(rows[0]["title"], "‘Gears of War’ launches")


class TitleIndexMigrationTests(unittest.TestCase):
    def test_migration_54_adds_only_the_title_index(self):
        from app import db_admin
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schema53.db"
            with database.get_db(path) as db:
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:53])
                before = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
                self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:54]), (54,))
                after = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
                plan = " ".join(row[3] for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM items WHERE title=?", ("x",)))
            self.assertEqual(after - before, {"idx_items_title"})
            self.assertIn("idx_items_title", plan)


if __name__ == "__main__":
    unittest.main()
