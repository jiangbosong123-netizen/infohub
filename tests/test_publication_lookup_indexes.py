import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin

LOOKUPS = {
    "idx_change_log_version": (
        "SELECT idempotency_key FROM change_log WHERE version_id=?", ("v",)),
    "idx_analysis_authorizations_budget": (
        """SELECT COALESCE(SUM(reserved_cost_microusd),0) FROM analysis_attempt_authorizations
           WHERE provider=? AND budget_day=? AND decision='allowed'""", ("p", "2026-10-04")),
}


class PublicationLookupIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for item in (patch.object(database, "DB_PATH", self.path), patch.object(config, "DB_PATH", self.path)):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def test_hot_lookups_search_an_index_instead_of_scanning_history(self):
        with database.get_db() as db:
            for index, (sql, args) in LOOKUPS.items():
                plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))
                with self.subTest(index):
                    self.assertIn(index, plan)
                    self.assertNotIn("SCAN", plan)

    def test_migration_46_adds_only_the_indexes_to_a_schema_45_database(self):
        predecessor = self.path.with_name("schema45.db")
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:45])
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, tuple(range(46, db_admin.CURRENT_SCHEMA_VERSION + 1)))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 45)
        with sqlite3.connect(predecessor) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue(set(LOOKUPS) <= names)


if __name__ == "__main__":
    unittest.main()
