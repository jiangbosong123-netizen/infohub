import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, db_admin
from app.api_auth import (
    ApiAuthError, authenticate_api_key, create_consumer, issue_api_key,
    revoke_api_key, revoke_consumer,
)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class ApiAuthFoundationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "auth.db"
        db_admin.migrate_database(self.path)

    def issue(self, scopes=frozenset({"read:items"}), expires_at=None):
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "fixture-consumer", now=NOW)
            key = issue_api_key(
                db, consumer, scopes, expires_at=expires_at or NOW + timedelta(days=30),
                now=NOW,
            )
        return consumer, key

    def test_migration_is_additive_and_verifies_current_schema(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_consumers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0], 0)
        report = db_admin.verify_database(self.path, require_current=True)
        self.assertEqual(report.schema_version, 22)

    def test_scope_expiry_and_secret_hash(self):
        consumer, key = self.issue()
        self.assertNotIn(key.token, repr(key))
        with database.get_db(self.path) as db:
            principal = authenticate_api_key(
                db, key.token, required_scope="read:items", now=NOW)
            self.assertEqual(principal.consumer_id, consumer)
            self.assertEqual(principal.authz_version, 1)
            self.assertIsNone(authenticate_api_key(
                db, key.token, required_scope="read:reports", now=NOW))
            self.assertIsNone(authenticate_api_key(
                db, key.token, now=NOW + timedelta(days=30)))
            self.assertIsNone(authenticate_api_key(
                db, key.token[:-1] + ("A" if key.token[-1] != "A" else "B"), now=NOW))
            stored = db.execute("SELECT token_sha256,scopes_json FROM api_keys").fetchone()
            self.assertEqual(len(stored["token_sha256"]), 64)
            self.assertNotIn(key.token, str(tuple(stored)))

    def test_revoke_key_and_consumer_invalidates_access_and_bumps_version(self):
        consumer, key = self.issue()
        with database.get_db(self.path) as db:
            self.assertTrue(revoke_api_key(db, key.key_id, now=NOW))
            self.assertFalse(revoke_api_key(db, key.key_id, now=NOW))
            self.assertIsNone(authenticate_api_key(db, key.token, now=NOW))
            self.assertEqual(db.execute(
                "SELECT authz_version FROM api_consumers WHERE id=?", (consumer,)
            ).fetchone()[0], 2)
            second = issue_api_key(db, consumer, {"read:reports"},
                                   expires_at=NOW + timedelta(days=1), now=NOW)
            self.assertIsNotNone(authenticate_api_key(db, second.token, now=NOW))
            self.assertTrue(revoke_consumer(db, consumer, now=NOW))
            self.assertFalse(revoke_consumer(db, consumer, now=NOW))
            self.assertIsNone(authenticate_api_key(db, second.token, now=NOW))
            self.assertEqual(db.execute(
                "SELECT authz_version FROM api_consumers WHERE id=?", (consumer,)
            ).fetchone()[0], 3)

    def test_invalid_scopes_and_naive_or_expired_times_are_rejected(self):
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "fixture-consumer", now=NOW)
            for scopes, expiry in (
                (set(), NOW + timedelta(days=1)),
                ({"admin:all"}, NOW + timedelta(days=1)),
                ({"read:items"}, NOW),
                ({"read:items"}, datetime(2026, 9, 23)),
            ):
                with self.assertRaises(ApiAuthError):
                    issue_api_key(db, consumer, scopes, expires_at=expiry, now=NOW)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0], 0)

    def test_upgrade_from_v21_keeps_existing_data_and_creates_backup(self):
        with database.get_db(self.path) as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('fixture','Fixture','ai','rss')")
        # Build a genuine v21 predecessor instead of manually removing schema objects.
        predecessor = self.path.with_name("predecessor.db")
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:21])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('old','Old','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (22,))
        self.assertTrue(report.backup_path)
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 21)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "old")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_consumers").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
