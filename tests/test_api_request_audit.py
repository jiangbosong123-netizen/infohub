import sqlite3
import tempfile
import unittest
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, db_admin
from app.api_auth import authenticate_api_key, create_consumer, issue_api_key
from app.api_request_audit import record_request_event
from app.api_key_admin import main as admin_main


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class ApiRequestAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        db_admin.migrate_database(self.path)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "audit-client", actor="test", now=NOW)
            key = issue_api_key(db, consumer, {"read:items"}, actor="test",
                                expires_at=NOW + timedelta(days=1), now=NOW)
            self.principal = authenticate_api_key(db, key.token, now=NOW)

    def test_allowlist_and_append_only_guards(self):
        with database.get_db(self.path) as db:
            record_request_event(
                db, request_id="request-one", event="completed",
                principal=self.principal, method="GET", resource="items",
                required_scope="read:items", status_code=200, duration_ms=12, now=NOW,
            )
            for changes in (
                {"resource": "/api/v1/items/private-id"},
                {"required_scope": "admin:all"},
                {"event": "token=secret"},
            ):
                values = dict(request_id="bad", event="completed", principal=self.principal,
                              method="GET", resource="items", required_scope="read:items")
                values.update(changes)
                with self.assertRaises(ValueError):
                    record_request_event(db, **values)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE api_request_audit SET duration_ms=1 WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM api_request_audit WHERE id=1")

    def test_operator_listing_contains_only_allowlisted_fields(self):
        with database.get_db(self.path) as db:
            record_request_event(
                db, request_id="safe-request", event="completed",
                principal=self.principal, method="GET", resource="items",
                required_scope="read:items", status_code=200, duration_ms=7, now=NOW,
            )
        output = io.StringIO()
        status = admin_main(
            ["request-audit-list", "--key-id", self.principal.key_id],
            db_path=self.path, output=output, actor="test",
        )
        self.assertEqual(status, 0)
        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resource"], "items")
        self.assertEqual(set(rows[0]), {
            "id", "request_id", "event", "consumer_id", "key_id", "method",
            "resource", "required_scope", "status_code", "error_code",
            "duration_ms", "occurred_at",
        })

    def test_upgrade_from_v24_preserves_transient_limits_and_adds_empty_audit(self):
        predecessor = self.path.with_name("predecessor.db")
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:24])
            consumer = create_consumer(db, "old", actor="test", now=NOW)
            key = issue_api_key(db, consumer, {"read:items"}, actor="test",
                                expires_at=NOW + timedelta(days=1), now=NOW)
            db.execute("INSERT INTO api_rate_buckets VALUES(?,?,?)",
                       (key.key_id, int(NOW.timestamp()), 2))
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(
            report.applied_versions,
            tuple(range(25, db_admin.CURRENT_SCHEMA_VERSION + 1)),
        )
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 24)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT used_count FROM api_rate_buckets").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_request_audit").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
