import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin


class SqliteWalResetTests(unittest.TestCase):
    def test_versions_with_and_without_the_fix(self):
        safe = [(3, 51, 3), (3, 53, 4), (4, 0, 0), (3, 50, 7), (3, 50, 9), (3, 44, 6)]
        unsafe = [(3, 51, 2), (3, 51, 0), (3, 50, 6), (3, 46, 1), (3, 45, 1), (3, 44, 5),
                  (3, 40, 1), (3, 7, 0)]
        for version in safe:
            self.assertTrue(database.sqlite_wal_reset_safe(version), version)
        for version in unsafe:
            self.assertFalse(database.sqlite_wal_reset_safe(version), version)

    def unsafe_library(self):
        return patch.multiple(sqlite3, sqlite_version_info=(3, 46, 1), sqlite_version="3.46.1")

    def test_production_refuses_to_verify_on_an_unsafe_library(self):
        with self.unsafe_library(), patch.object(config, "ENVIRONMENT", "production"):
            with self.assertRaisesRegex(db_admin.DatabaseSafetyError, "3.46.1.*walresetbug"):
                db_admin.verify_database(Path("does-not-matter.db"))

    def test_other_environments_warn_once_and_continue(self):
        with self.unsafe_library(), patch.object(config, "ENVIRONMENT", "development"), \
                patch.object(db_admin, "_UNSAFE_SQLITE_WARNED", False):
            with self.assertLogs("app.db_admin", "WARNING") as logs:
                db_admin.require_safe_sqlite()
                db_admin.require_safe_sqlite()
        self.assertEqual(len(logs.records), 1)

    def test_the_image_builds_a_fixed_checksummed_library(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        version = int(re.search(r"ARG SQLITE_VERSION=(\d{7})", dockerfile).group(1))
        major, minor, patch_level = version // 1_000_000, version // 10_000 % 100, version // 100 % 100
        self.assertTrue(database.sqlite_wal_reset_safe((major, minor, patch_level)))
        self.assertRegex(dockerfile, r"ARG SQLITE_SHA3_256=[0-9a-f]{64}")
        self.assertIn("hashlib.sha3_256", dockerfile)
        self.assertIn("assert v >= (3, 51, 3)", dockerfile)
        self.assertIn("COPY --from=sqlite /usr/local/lib/libsqlite3.so*", dockerfile)



class PlannerStatisticsTests(unittest.TestCase):
    """Pages are tuned for plans chosen without sqlite_stat1; ANALYZE made /topics ~6x slower."""

    def test_migration_and_verification_leave_no_statistics(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        db_admin.migrate_database(path)
        db_admin.verify_database(path, require_current=True)
        with sqlite3.connect(path) as db:
            stats = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'").fetchall()
        self.assertEqual(stats, [])

    def test_the_application_never_collects_statistics(self):
        root = Path(__file__).resolve().parents[1]
        statement = re.compile(r"(?i:PRAGMA\s+(\w+\.)?optimize)|\bANALYZE\b")
        offenders = [str(source.relative_to(root)) for source in [root / "cli.py", *(root / "app").rglob("*.py")]
                     if statement.search(source.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

if __name__ == "__main__":
    unittest.main()
