import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app import db_admin


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "app.db"

    def _legacy_database(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.executescript(database.SCHEMA)
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','ai','rss')"""
            )
            db.execute(
                """INSERT INTO items(source_id,url,title,summary,channel,published_at,fetched_at)
                   VALUES(1,'https://example.com/one','legacy title','legacy summary','ai',
                          '2026-09-14T09:00:00+00:00','2026-09-14T09:01:00+00:00')"""
            )

    def test_new_database_is_versioned_and_idempotent(self):
        first = db_admin.migrate_database(self.path)
        second = db_admin.migrate_database(self.path)
        self.assertEqual(first.previous_state, "empty")
        self.assertEqual(first.applied_versions, (1, 2))
        self.assertIsNone(first.backup_path)
        self.assertEqual(second.applied_versions, ())
        self.assertEqual(second.verification.state, "current")
        self.assertEqual(second.current_version, db_admin.CURRENT_SCHEMA_VERSION)

    def test_legacy_upgrade_creates_verified_backup_and_preserves_rows(self):
        self._legacy_database()
        with patch.object(db_admin.config, "APP_VERSION", "release-test-sha"):
            report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_state, "legacy_unversioned")
        self.assertTrue(report.backup_path)
        backup = Path(report.backup_path)
        self.assertTrue(backup.exists())
        self.assertEqual(db_admin.verify_database(backup).state, "legacy_unversioned")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT title,summary FROM items").fetchone(),
                             ("legacy title", "legacy summary"))
            migrations = db.execute(
                "SELECT version,name,checksum,release_id FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(
            migrations,
            [
                (migration.version, migration.name, migration.checksum, "release-test-sha")
                for migration in db_admin.MIGRATIONS
            ],
        )
        with sqlite3.connect(self.path) as db:
            self.assertTrue(db.execute(
                "SELECT applied_at FROM schema_migrations"
            ).fetchone()[0].endswith("Z"))

    def test_failed_migration_rolls_back_schema_and_history(self):
        def fail_after_ddl(db: sqlite3.Connection) -> None:
            db.execute("CREATE TABLE should_rollback(id INTEGER PRIMARY KEY)")
            raise RuntimeError("injected failure")

        failing = db_admin.Migration(1, "failure fixture", "v1", fail_after_ddl)
        with database.get_db(self.path) as db:
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                db_admin.apply_migrations(db, (failing,))
        with sqlite3.connect(self.path) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("should_rollback", tables)
        self.assertNotIn("schema_migrations", tables)

    def test_tampered_history_is_rejected_before_change(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE schema_migrations SET checksum='wrong'")
        with self.assertRaises(db_admin.UnsupportedSchemaError):
            db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT checksum FROM schema_migrations"
            ).fetchone()[0], "wrong")

    def test_newer_unknown_version_is_rejected_before_change(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO schema_migrations(
                       version,name,checksum,applied_at,release_id
                   ) VALUES(3,'future','unknown','2026-09-15T00:00:00.000000Z','future')"""
            )
        with self.assertRaisesRegex(db_admin.UnsupportedSchemaError, "newer or unknown"):
            db_admin.migrate_database(self.path)
        self.assertFalse((self.root / "backups").exists())

    def test_old_migration_table_layout_is_rejected(self):
        self._legacy_database()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
            db.execute("""INSERT INTO schema_migrations(version,name,applied_at)
                          VALUES(1,'unrecognized','2026-09-15T00:00:00Z')""")
        with self.assertRaisesRegex(db_admin.UnsupportedSchemaError, "unsupported layout"):
            db_admin.migrate_database(self.path)

    def test_unrecognized_nonempty_database_is_not_modified(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE unrelated(id INTEGER)")
        with self.assertRaises(db_admin.UnsupportedSchemaError):
            db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertEqual(tables, {"unrelated"})

    def test_backup_includes_wal_data_and_never_overwrites(self):
        db_admin.migrate_database(self.path)
        with database.get_db(self.path) as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('wal','WAL','ai','rss')")
        destination = self.root / "manual.db"
        report = db_admin.backup_database(self.path, destination)
        self.assertEqual(report.integrity, "ok")
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "wal")
        with self.assertRaises(FileExistsError):
            db_admin.backup_database(self.path, destination)

    def test_init_schema_uses_patched_database_path(self):
        with patch.object(database, "DB_PATH", self.path):
            database.init_schema()
        self.assertEqual(
            db_admin.verify_database(self.path, require_current=True).schema_version,
            db_admin.CURRENT_SCHEMA_VERSION,
        )

    def test_version_one_database_is_backed_up_then_extended(self):
        with database.get_db(self.path) as db:
            applied = db_admin.apply_migrations(
                db, db_admin.MIGRATIONS[:1], release_id="old-release"
            )
        self.assertEqual(applied, (1,))
        with sqlite3.connect(self.path) as db:
            self.assertNotIn(
                "jobs",
                {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )},
            )

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_state, "versioned")
        self.assertEqual(report.previous_version, 1)
        self.assertEqual(report.applied_versions, (2,))
        self.assertTrue(report.backup_path)
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 1)
        self.assertEqual(report.verification.schema_version, 2)


if __name__ == "__main__":
    unittest.main()
