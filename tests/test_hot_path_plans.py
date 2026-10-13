"""The worker's queue polls and the health snapshot read small indexes, not every item (schema 55)."""

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import company_match, config, database
from app.ai import pipeline
from app.crawler import article_text, runner
from app.stories import refresh_derived
from app.web import routes
from tests.test_query_plans import traced_statements

# Reading every row of items or stories, directly, along an unrelated index, or by building a
# throwaway automatic index each time. Scanning one of the small queue indexes is the point.
WHOLE_TABLE = re.compile(r"^(SCAN|SEARCH) (items|i|stories|st|story_items|si)\b")
QUEUE_INDEXES = ("idx_items_unscored", "idx_items_tmt_pending", "idx_items_untitled", "idx_items_hidden",
                 "idx_items_fetched")


def reads_every_row(detail: str) -> bool:
    if "AUTOMATIC" in detail and WHOLE_TABLE.search(detail):
        return True
    return detail.startswith("SCAN ") and bool(WHOLE_TABLE.search(detail)) and not any(
        name in detail for name in QUEUE_INDEXES)


class HotPathPlanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "BLOB_PATH", Path(folder.name) / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','info')")
            db.execute("INSERT INTO companies(slug,name,market) VALUES('nvidia','NVIDIA','US')")
        now = datetime.now(timezone.utc).isoformat()
        for n in range(40):
            runner.insert_item("test", dict(url=f"https://e.test/{n}", title=f"Nvidia story number {n} about chips",
                                            published_at=now, companies=["nvidia"]))

    def full_scans(self, statements, marker):
        found = [sql for sql in statements if marker in sql]
        self.assertTrue(found, f"no statement containing {marker!r} ran")
        scans = []
        with database.get_db() as db:
            for sql in found:
                # The total is a count of every item by definition (it reads the narrowest index).
                sql = sql.replace("(SELECT COUNT(*) FROM items) AS total", "0 AS total")
                for row in db.execute("EXPLAIN QUERY PLAN " + sql):
                    if reads_every_row(row[3]) or "CORRELATED" in row[3]:
                        scans.append((row[3], sql[:120]))
        return scans

    def test_work_queues_and_health_counts_use_indexes(self):
        with traced_statements() as statements, \
                patch.object(config, "llm_enabled", return_value=True), \
                patch.object(pipeline, "_call_llm", return_value=[]), \
                patch.object(pipeline, "_call_llm_tmt", return_value=[]):
            pipeline.process_pending(limit=10)
            pipeline.backfill_tmt(max_batches=1)
            pipeline.backfill_titles(max_batches=1)
            routes._system_snapshot()
            routes._query_items(company="nvidia", mode="all")
            with database.get_db() as db:
                article_text.due_items(db, datetime.now(timezone.utc), 10)
        for marker in ("score IS NULL", "tmt IS NULL", "title_zh=''", "WHERE tmt=0",
                       "item_companies ic JOIN companies c", "article_texts a ON a.item_id"):
            with self.subTest(marker=marker):
                self.assertEqual(self.full_scans(statements, marker), [])

    def test_story_refresh_finds_anchors_through_an_index(self):
        with traced_statements() as statements:
            refresh_derived()
        for marker in ("anchor_item_id IN (SELECT item_id FROM derived_dirty)",
                       "COALESCE(i.tmt,1)!=0 AND i.published_at BETWEEN"):
            with self.subTest(marker=marker):
                self.assertEqual(self.full_scans(statements, marker), [])


if __name__ == "__main__":
    unittest.main()
