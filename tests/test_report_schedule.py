import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import config, database
from app.ai.daily import generate_daily
from app.report_inputs import freeze_calendar_daily
from app.report_schedule import generate_legacy_scheduled_report, generate_scheduled_report


class ScheduledReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for target, name, value in (
            (database, "DB_PATH", path),
            (config, "DB_PATH", path),
            (config, "APP_TZ", ZoneInfo("Europe/London")),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/one','Evidence','ai',80,1,
                                 '2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""")

    def test_first_run_publishes_once_and_retry_creates_no_snapshot(self):
        first = generate_scheduled_report("2026-09-18")
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["citation_count"], 1)
        self.assertEqual(generate_scheduled_report("2026-09-18")["status"], "already_published")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_input_snapshots").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 0)

    def test_legacy_row_is_preserved_without_creating_snapshot(self):
        with database.get_db() as db:
            db.execute("INSERT INTO daily_reports(date,content,created_at) VALUES('2026-09-18','old text','2026-09-19T07:00:00Z')")
        self.assertEqual(generate_scheduled_report("2026-09-18")["status"], "legacy_preserved")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "old text")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_input_snapshots").fetchone()[0], 0)

    def test_late_legacy_row_is_rechecked_before_publish(self):
        def freeze_then_legacy(day):
            snapshot = freeze_calendar_daily(day)
            with database.get_db() as db:
                db.execute("INSERT INTO daily_reports(date,content,created_at) VALUES(?,?,?)",
                           (day, "late legacy", "2026-09-19T07:00:00Z"))
            return snapshot

        with patch("app.report_schedule.freeze_calendar_daily", side_effect=freeze_then_legacy):
            self.assertEqual(generate_scheduled_report("2026-09-18")["status"], "legacy_preserved")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "late legacy")

    def test_legacy_worker_writes_once_and_retry_preserves_content(self):
        first = generate_legacy_scheduled_report("2026-09-18")
        self.assertEqual(first["status"], "legacy_written")
        with database.get_db() as db:
            before = tuple(db.execute("SELECT content,created_at FROM daily_reports").fetchone())
            db.execute("UPDATE items SET title='Changed later' WHERE id=1")
        self.assertEqual(generate_legacy_scheduled_report("2026-09-18")["status"], "legacy_preserved")
        with database.get_db() as db:
            self.assertEqual(tuple(db.execute("SELECT content,created_at FROM daily_reports").fetchone()), before)

    def test_explicit_legacy_regeneration_remains_available(self):
        self.assertEqual(generate_legacy_scheduled_report("2026-09-18")["status"], "legacy_written")
        with database.get_db() as db:
            db.execute("UPDATE items SET title='Explicitly revised' WHERE id=1")
        self.assertEqual(generate_daily("2026-09-18"), "2026-09-18")
        with database.get_db() as db:
            self.assertIn("Explicitly revised", db.execute("SELECT content FROM daily_reports").fetchone()[0])

    def test_legacy_worker_cannot_replace_published_version(self):
        self.assertEqual(generate_scheduled_report("2026-09-18")["status"], "published")
        self.assertEqual(generate_legacy_scheduled_report("2026-09-18")["status"], "already_published")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 0)

    def test_legacy_final_insert_rechecks_versioned_pointer(self):
        from app.ai import daily

        original = daily._collect

        def collect_then_publish(day):
            result = original(day)
            self.assertEqual(generate_scheduled_report(day)["status"], "published")
            return result

        with patch.object(daily, "_collect", side_effect=collect_then_publish):
            self.assertEqual(generate_legacy_scheduled_report("2026-09-18")["status"], "already_published")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 0)
