import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.data_coverage import audit_data_coverage
from app.legacy_backfill import backfill_legacy_batch


class DataCoverageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "app.db"
        for module, key, value in (
            (database, "DB_PATH", self.path),
            (config, "DB_PATH", self.path),
            (config, "BLOB_PATH", self.root / "blobs"),
        ):
            scope = patch.object(module, key, value)
            scope.start()
            self.addCleanup(scope.stop)

    def test_legacy_database_reports_schema_gap_without_migration(self):
        with sqlite3.connect(self.path) as db:
            db.executescript(database.SCHEMA)
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                          VALUES(1,'one','One','stock','media','rss')""")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/one','One','stock',
                                 '2026-09-18T00:00:00Z','2026-09-18T01:00:00Z')""")
        result = audit_data_coverage(self.path)
        self.assertEqual(result["schema_state"], "legacy_unversioned")
        self.assertEqual(result["legacy"]["items_by_channel"], {"stock": 1})
        self.assertEqual(result["blocked_reasons"], ["current_schema_missing"])
        with sqlite3.connect(self.path) as db:
            self.assertFalse(db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='documents'").fetchone())

    def test_current_database_distinguishes_schema_from_backfilled_rows(self):
        database.init_schema()
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                          VALUES(1,'one','One','stock','media','rss')""")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/one','One','stock',
                                 '2026-09-18T00:00:00Z','2026-09-18T01:00:00Z')""")
            db.execute("""INSERT INTO item_discoveries(item_id,source_id,first_seen_at,last_seen_at)
                          VALUES(1,1,'2026-09-18T01:00:00Z','2026-09-18T01:00:00Z')""")
            db.execute("""INSERT INTO daily_reports(id,date,content,created_at)
                          VALUES(1,'2026-09-18','# Daily','2026-09-19T01:00:00Z')""")
        before = audit_data_coverage(self.path)
        self.assertEqual(before["new_model"]["documents_with_current_version"],
                         {"covered": 0, "total": 1, "percent": 0.0})
        self.assertIn("legacy_backfill_incomplete", before["blocked_reasons"])
        self.assertEqual(before["new_model"]["search"]["state"], "empty")

        for _ in range(4):
            result = backfill_legacy_batch(10)
            if result.status == "completed":
                break
        self.assertEqual(result.status, "completed")
        after = audit_data_coverage(self.path)
        self.assertEqual(after["new_model"]["documents_with_current_version"]["covered"], 1)
        self.assertEqual(after["new_model"]["documents_with_raw_input"]["covered"], 1)
        self.assertEqual(after["new_model"]["documents_with_legacy_excerpt_input"], 1)
        self.assertEqual(after["new_model"]["legacy_discoveries_mapped"]["covered"], 1)
        self.assertEqual(after["new_model"]["legacy_report_identities_mapped"]["covered"], 1)
        self.assertNotIn("legacy_backfill_incomplete", after["blocked_reasons"])
        self.assertIn("document_analysis_not_fully_published", after["blocked_reasons"])


if __name__ == "__main__":
    unittest.main()
