import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, db_admin
from app.source_activity import source_activity
from app.web import routes

NOW = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)  # a Sunday evening


def hourly(end: datetime, days: int = 14) -> list[datetime]:
    return [end - timedelta(hours=n) for n in range(days * 24)]


def weekdays(end: datetime, days: int = 14) -> list[datetime]:
    """Every two hours on weekdays only, ending at ``end`` (a Friday evening)."""
    return [t for t in (end - timedelta(hours=2 * n) for n in range(days * 12)) if t.weekday() < 5]


class SourceActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for target, name in ((database, "DB_PATH"), (config, "DB_PATH")):
            item = patch.object(target, name, self.path)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        friday = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)
        self.patterns = {
            "stopped": hourly(NOW - timedelta(hours=20)),        # quiet for 20 h, normally hourly
            "alive": hourly(NOW - timedelta(hours=2)),
            "weekdays": weekdays(friday),                        # quiet since Friday, as every weekend
            "sparse": [NOW - timedelta(days=n * 3) for n in range(5)],
            "failing": hourly(NOW - timedelta(hours=20)),
        }
        with database.get_db() as db:
            for n, (key, times) in enumerate(self.patterns.items(), start=1):
                db.execute("""INSERT INTO sources(id,key,name,channel,type,interval_minutes,fail_count,
                              last_success_at,last_error) VALUES(?,?,?,'ai','rss',30,?,?,?)""",
                           (n, key, key, 1 if key == "failing" else 0,
                            (NOW - timedelta(minutes=10)).isoformat(),
                            "timeout" if key == "failing" else None))
                for m, seen in enumerate(times):
                    item_id = n * 10_000 + m
                    db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                                  VALUES(?,?,?,?,'ai',?,?)""",
                               (item_id, n, f"https://e.test/{key}/{m}", "T", seen.isoformat(), seen.isoformat()))
                    db.execute("""INSERT INTO item_discoveries(item_id,source_id,first_seen_at,last_seen_at)
                                  VALUES(?,?,?,?)""", (item_id, n, seen.isoformat(), NOW.isoformat()))

    def statuses(self):
        with database.get_db() as db:
            rows = db.execute("""SELECT id,key,fail_count,last_success_at,last_error,interval_minutes
                                 FROM sources""").fetchall()
            activity = source_activity(db, [r["id"] for r in rows])
        return {r["key"]: routes._source_status(r, NOW, activity.get(r["id"])) for r in rows}, activity

    def test_only_a_source_quiet_far_beyond_its_own_rhythm_is_silent(self):
        statuses, activity = self.statuses()
        self.assertEqual(statuses, {"stopped": "silent", "alive": "ok", "weekdays": "ok",
                                    "sparse": "ok", "failing": "bad"})
        by_key = dict(zip(self.patterns, (activity[n] for n in range(1, 6))))
        self.assertEqual(by_key["stopped"].quiet_limit, timedelta(hours=12))
        self.assertGreater(by_key["weekdays"].quiet_limit, timedelta(hours=70))
        self.assertIsNone(by_key["sparse"].quiet_limit)

    def test_a_weekday_source_is_silent_when_it_misses_monday(self):
        statuses, _ = self.statuses()
        with database.get_db() as db:
            rows = db.execute("SELECT id,key,fail_count,last_success_at,last_error,interval_minutes "
                              "FROM sources WHERE key='weekdays'").fetchall()
            activity = source_activity(db, [rows[0]["id"]])
        tuesday = NOW + timedelta(days=2)
        row = dict(rows[0], last_success_at=(tuesday - timedelta(minutes=10)).isoformat())
        self.assertEqual(routes._source_status(row, tuesday, activity[row["id"]]), "silent")

    def test_snapshot_and_page_report_silent_sources(self):
        with patch.object(routes, "datetime", type("Clock", (datetime,), {
                "now": classmethod(lambda cls, tz=None: NOW if tz else NOW.replace(tzinfo=None))})):
            snapshot = routes._system_snapshot()
            page = TestClient(routes.app).get("/health")
        self.assertEqual(snapshot["sources"]["silent"], 1)
        self.assertIn("sources_silent", snapshot["pipeline"]["issues"])
        self.assertIn("source_failures_or_staleness", snapshot["pipeline"]["issues"])  # "failing"
        self.assertEqual(page.status_code, 200)
        self.assertIn("· 静默", page.text)

    def test_reads_use_the_source_index(self):
        with database.get_db() as db:
            plans = [" ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))
                     for sql, args in (
                         ("SELECT MAX(first_seen_at) FROM item_discoveries WHERE source_id=?", (1,)),
                         ("SELECT first_seen_at FROM item_discoveries WHERE source_id=? AND first_seen_at>=?",
                          (1, "2026-10-01")))]
        for plan in plans:
            self.assertIn("idx_item_discoveries_source_seen", plan)
            self.assertNotIn("SCAN item_discoveries", plan)


class SourceDiscoveryIndexMigrationTests(unittest.TestCase):
    def test_migration_52_adds_only_the_index(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        with database.get_db(path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:51])
            before = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        report = db_admin.migrate_database(path)
        self.assertEqual(report.applied_versions, tuple(range(52, db_admin.CURRENT_SCHEMA_VERSION + 1)))
        with sqlite3.connect(path) as db:
            after = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        self.assertEqual(after - before, {"idx_item_discoveries_source_seen"})


if __name__ == "__main__":
    unittest.main()
