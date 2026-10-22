import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import collection_gaps, config, database, db_admin, worker

T = datetime(2026, 10, 22, 9, 50, tzinfo=timezone.utc)


class CollectionGapTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "app.db"
        for target, name in ((database, "DB_PATH"), (config, "DB_PATH")):
            item = patch.object(target, name, self.path)
            item.start()
            self.addCleanup(item.stop)

    def runs(self, db, *minutes):
        db.execute("INSERT OR IGNORE INTO sources(id,key,name,channel,type) VALUES(1,'wire','Wire','ai','rss')")
        for minute in minutes:
            db.execute("INSERT INTO fetch_log(source_id,ran_at,ok) VALUES(1,?,1)",
                       ((T + timedelta(minutes=minute)).isoformat(),))

    def gaps(self, db):
        return [tuple(row) for row in db.execute("SELECT started_at, ended_at FROM collection_gaps ORDER BY 1")]

    def test_migration_65_records_the_gaps_the_fetch_log_shows(self):
        with database.get_db() as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:64])
            self.runs(db, 0, 5, 10, 568, 573, 600)  # the host slept from 09:55Z to 19:18Z
            db.execute("INSERT INTO collection_gaps VALUES('2026-10-22T19:30:00Z','2026-10-22T20:00:00Z','by hand')")
        with database.get_db() as db:
            self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:65]), (65,))
            self.assertEqual(self.gaps(db), [("2026-10-22T10:00:00Z", "2026-10-22T19:18:00Z"),
                                             ("2026-10-22T19:30:00Z", "2026-10-22T20:00:00Z")])  # 27 min apart is no gap
            reason = db.execute("SELECT reason FROM collection_gaps WHERE started_at='2026-10-22T10:00:00Z'").fetchone()[0]
        self.assertIn("558 分钟", reason)

    def test_a_crawl_cycle_after_a_sleep_records_the_gap_once(self):
        database.init_schema()
        with database.get_db() as db:
            self.runs(db, 0)
            self.assertIsNone(collection_gaps.record_open_gap(db, T + timedelta(minutes=20)))  # a normal pause
            gap = collection_gaps.record_open_gap(db, T + timedelta(hours=9))
            self.assertEqual((gap["from"], gap["to"], gap["minutes"]),
                             ("2026-10-22T09:50:00Z", "2026-10-22T18:50:00Z", 540))
            self.assertIsNone(collection_gaps.record_open_gap(db, T + timedelta(hours=9, minutes=2)))  # not twice
            self.assertEqual(len(self.gaps(db)), 1)

    def test_the_crawl_job_checks_before_it_runs(self):
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT OR IGNORE INTO sources(id,key,name,channel,type) VALUES(1,'wire','Wire','ai','rss')")
            db.execute("INSERT INTO fetch_log(source_id,ran_at,ok) VALUES(1,?,1)",
                       ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),))
        with patch("app.crawler.runner.run_due_sources", return_value={}), \
                patch("app.external_heartbeat.report_cycle"), self.assertLogs("app.worker", "WARNING"):
            worker._crawl()
        with database.get_db() as db:
            self.assertEqual(len(self.gaps(db)), 1)


if __name__ == "__main__":
    unittest.main()
