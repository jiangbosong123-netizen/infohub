import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, db_admin
from app.api_auth import authenticate_api_key
from app.api_key_admin import main


class ApiKeyAdminTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        db_admin.migrate_database(self.path)

    def command(self, *args):
        output = io.StringIO()
        status = main(list(args), db_path=self.path, output=output, actor="test-operator")
        return status, json.loads(output.getvalue()) if output.getvalue() else None

    def test_issue_list_revoke_and_audit_never_repeat_or_store_token(self):
        status, created = self.command("consumer-create", "research-client")
        self.assertEqual(status, 0)
        consumer_id = created["consumer_id"]
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        status, issued = self.command(
            "key-issue", consumer_id, "--scope", "read:items",
            "--scope", "read:reports", "--expires-at", expires,
        )
        self.assertEqual(status, 0)
        token = issued["token"]
        self.assertTrue(token.startswith("ih1."))
        with database.get_db(self.path) as db:
            self.assertEqual(authenticate_api_key(db, token).consumer_id, consumer_id)
            audit = [dict(row) for row in db.execute(
                "SELECT action,actor,key_id,details_json FROM api_key_audit ORDER BY id")]
            self.assertEqual([row["action"] for row in audit],
                             ["consumer_created", "key_issued"])
            self.assertTrue(all(row["actor"] == "test-operator" for row in audit))
            self.assertNotIn(token, str(audit))
            self.assertNotIn(token, str(dict(db.execute("SELECT * FROM api_keys").fetchone())))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE api_key_audit SET actor='changed' WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM api_key_audit WHERE id=1")
        status, keys = self.command("key-list", consumer_id)
        self.assertEqual(status, 0)
        self.assertEqual(len(keys), 1)
        self.assertNotIn(token, json.dumps(keys))
        self.assertNotIn("token_sha256", json.dumps(keys))
        status, audit_list = self.command("audit-list", "--consumer-id", consumer_id)
        self.assertEqual(status, 0)
        self.assertNotIn(token, json.dumps(audit_list))
        self.assertEqual(self.command("key-revoke", issued["key_id"])[1], {"revoked": True})
        self.assertEqual(self.command("key-revoke", issued["key_id"])[1], {"revoked": False})
        with database.get_db(self.path) as db:
            self.assertIsNone(authenticate_api_key(db, token))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_key_audit").fetchone()[0], 3)
        self.assertEqual(self.command("consumer-revoke", consumer_id)[1], {"revoked": True})
        self.assertEqual(self.command("consumer-revoke", consumer_id)[1], {"revoked": False})
        with database.get_db(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_key_audit").fetchone()[0], 4)

    def test_invalid_issue_does_not_create_key_or_audit(self):
        consumer_id = self.command("consumer-create", "client")[1]["consumer_id"]
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status, result = self.command(
                "key-issue", consumer_id, "--scope", "admin:all", "--expires-at", expiry)
        self.assertEqual(status, 2)
        self.assertIsNone(result)
        self.assertIn("operation rejected", stderr.getvalue())
        with database.get_db(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_key_audit").fetchone()[0], 1)

    def test_migration_22_to_23_preserves_keys_and_adds_empty_audit(self):
        predecessor = self.path.with_name("predecessor.db")
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:22])
            db.execute("""INSERT INTO api_consumers(id,name,status,created_at)
                          VALUES('old','Old','active','2026-09-20T00:00:00Z')""")
            db.execute("""INSERT INTO api_keys(
                          key_id,consumer_id,token_sha256,scopes_json,issued_at,expires_at)
                          VALUES('old-key','old',?,'["read:items"]',
                                 '2026-09-20T00:00:00Z','2027-09-20T00:00:00Z')""",
                       ("a" * 64,))
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions,
                         tuple(range(23, db_admin.CURRENT_SCHEMA_VERSION + 1)))
        self.assertTrue(report.backup_path)
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 22)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT name FROM api_consumers WHERE id='old'").fetchone()[0], "Old")
            self.assertEqual(db.execute("SELECT token_sha256 FROM api_keys WHERE key_id='old-key'").fetchone()[0], "a" * 64)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_key_audit").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
