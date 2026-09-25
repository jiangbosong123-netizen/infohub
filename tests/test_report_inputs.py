import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import config, database
from app.report_inputs import calendar_window, freeze_calendar_daily, load_frozen_manifest


class ReportInputTests(unittest.TestCase):
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
            for item_id, title, published, fetched, score, tmt in (
                (1, "Selected", "2026-09-18T12:00:00Z", "2026-09-18T12:01:00Z", 80, 1),
                (2, "Hidden", "2026-09-18T13:00:00Z", "2026-09-18T13:01:00Z", 90, 0),
                (3, "Late", "2026-09-18T14:00:00Z", "2026-09-20T00:00:00Z", 99, 1),
                (4, "Outside", "2026-09-19T00:00:00Z", "2026-09-19T00:01:00Z", 99, 1),
            ):
                db.execute("""INSERT INTO items(id,source_id,url,title,summary,channel,score,tmt,published_at,fetched_at)
                              VALUES(?,1,?,?,'Evidence','ai',?,?,?,?)""",
                           (item_id, f"https://example.test/{item_id}", title, score, tmt, published, fetched))

    def test_snapshot_freezes_visible_material_and_does_not_touch_legacy_reports(self):
        now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
        first = freeze_calendar_daily("2026-09-18", now=now)
        self.assertTrue(first["created"])
        self.assertEqual(first["input_count"], 1)
        again = freeze_calendar_daily("2026-09-18", now=now)
        self.assertEqual(again["snapshot_id"], first["snapshot_id"])
        self.assertFalse(again["created"])
        self.assertEqual(load_frozen_manifest(first["snapshot_id"])["items"][0]["item_id"], 1)
        with database.get_db() as db:
            row = db.execute("SELECT * FROM report_input_snapshots").fetchone()
            self.assertEqual(hashlib.sha256(row["manifest_json"].encode()).hexdigest(), row["manifest_sha256"])
            manifest = json.loads(row["manifest_json"])
            self.assertEqual([value["item_id"] for value in manifest["items"]], [1])
            self.assertEqual(manifest["point_in_time_status"], "legacy_mutable_unverified")
            self.assertEqual(manifest["items"][0]["url"], "https://example.test/1")
            self.assertEqual(manifest["coverage"]["eligible_by_channel"], {"ai": 1})
            self.assertEqual(manifest["coverage"]["selected_by_channel"], {"ai": 1})
            self.assertEqual(row["input_count"], db.execute("SELECT COUNT(*) FROM report_input_members").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 0)
            db.execute("UPDATE items SET title='Changed later' WHERE id=1")
        with database.get_db() as db:
            frozen = json.loads(db.execute("SELECT manifest_json FROM report_input_snapshots").fetchone()[0])
            self.assertEqual(frozen["items"][0]["title_original"], "Selected")

    def test_read_rejects_corrupt_member_digest(self):
        result = freeze_calendar_daily("2026-09-18", now=datetime(2026, 9, 19, 8, tzinfo=timezone.utc))
        with database.get_db() as db:
            db.execute("DROP TRIGGER report_input_members_no_update")
            db.execute("UPDATE report_input_members SET material_sha256=?", ("f" * 64,))
        with self.assertRaisesRegex(ValueError, "member digest"):
            load_frozen_manifest(result["snapshot_id"])

    def test_empty_day_adds_no_snapshot(self):
        self.assertIsNone(freeze_calendar_daily("2026-09-16", now=datetime(2026, 9, 19, tzinfo=timezone.utc)))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_input_snapshots").fetchone()[0], 0)

    def test_low_volume_channel_keeps_a_place_among_high_scoring_items(self):
        with database.get_db() as db:
            db.execute("""INSERT INTO items(id,source_id,url,title,summary,channel,score,tmt,published_at,fetched_at)
                          VALUES(5,1,'https://example.test/robot','Robot','Evidence','robot',1,1,
                                 '2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""")
            db.executemany("""INSERT INTO items(id,source_id,url,title,summary,channel,score,tmt,published_at,fetched_at)
                              VALUES(?,1,?,'Stock','Evidence','stock',99,1,
                                     '2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""",
                           [(number, f"https://example.test/{number}") for number in range(100, 240)])
        result = freeze_calendar_daily("2026-09-18", now=datetime(2026, 9, 19, 8, tzinfo=timezone.utc))
        manifest = load_frozen_manifest(result["snapshot_id"])
        self.assertEqual(len(manifest["items"]), 120)
        self.assertIn(5, [item["item_id"] for item in manifest["items"]])
        self.assertEqual(manifest["coverage"]["selected_by_channel"]["robot"], 1)
        self.assertTrue(manifest["coverage"]["truncated_by_global_limit"])

    def test_calendar_window_uses_timezone_and_dst(self):
        self.assertEqual(calendar_window("2026-03-29"), ("2026-03-29T00:00:00.000000Z", "2026-03-29T23:00:00.000000Z"))
        self.assertEqual(calendar_window("2026-10-25"), ("2026-10-24T23:00:00.000000Z", "2026-10-26T00:00:00.000000Z"))
        with self.assertRaises(ValueError):
            calendar_window("2026-9-18")


if __name__ == "__main__":
    unittest.main()
