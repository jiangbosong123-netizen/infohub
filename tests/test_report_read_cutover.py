import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import config, database
from app.report_inputs import freeze_calendar_daily
from app.report_versions import publish_structured_report
from app.web import routes


class ReportReadCutoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        for target, name, value in (
            (database, "DB_PATH", self.path),
            (config, "DB_PATH", self.path),
            (config, "APP_TZ", ZoneInfo("Europe/London")),
        ):
            control = patch.object(target, name, value)
            control.start()
            self.addCleanup(control.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,summary,channel,score,tmt,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/report','New source','Evidence','ai',80,1,
                                 '2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""")
            db.execute("""INSERT INTO daily_reports(date,content,created_at)
                          VALUES('2026-09-18','legacy report','2026-09-19T07:00:00Z')""")
        snapshot = freeze_calendar_daily("2026-09-18", now=datetime(2026, 9, 19, 8, tzinfo=timezone.utc))
        self.publication = publish_structured_report(snapshot["snapshot_id"])
        self.client = TestClient(routes.app)

    def test_default_off_keeps_legacy_daily_pages(self):
        with patch.object(routes, "REPORT_READ_ENABLED", False):
            response = self.client.get("/daily/2026-09-18")
            listing = self.client.get("/daily")
        self.assertEqual(response.status_code, 200)
        self.assertIn("legacy report", response.text)
        self.assertNotIn("New source", response.text)
        self.assertIn("旧版日报", listing.text)

    def test_enabled_reads_version_with_citation_and_legacy_fallback(self):
        with database.get_db() as db:
            db.execute("INSERT INTO daily_reports(date,content,created_at) VALUES('2026-09-17','older legacy','2026-09-18T07:00:00Z')")
        with patch.object(routes, "REPORT_READ_ENABLED", True):
            listing = self.client.get("/daily")
            versioned = self.client.get("/daily/2026-09-18")
            older = self.client.get("/daily/2026-09-17")
        self.assertIn("版本 1", listing.text)
        self.assertIn("旧版日报", listing.text)
        self.assertIn("New source", versioned.text)
        self.assertIn("https://example.test/report", versioned.text)
        self.assertIn("历史可见时点未核实", versioned.text)
        self.assertNotIn("legacy report", versioned.text)
        self.assertIn("older legacy", older.text)

    def test_bad_version_falls_back_with_visible_notice(self):
        with database.get_db() as db:
            db.execute("DROP TRIGGER report_versions_no_update")
            db.execute("UPDATE report_versions SET content='corrupt' WHERE id=?",
                       (self.publication["version_id"],))
        with patch.object(routes, "REPORT_READ_ENABLED", True):
            response = self.client.get("/daily/2026-09-18")
        self.assertEqual(response.status_code, 200)
        self.assertIn("legacy report", response.text)
        self.assertIn("新版日报校验失败", response.text)
        self.assertNotIn("corrupt", response.text)


if __name__ == "__main__":
    unittest.main()
