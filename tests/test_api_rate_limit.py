import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, db_admin
from app.api_auth import authenticate_api_key, create_consumer, issue_api_key
from app.api_rate_limit import release_api_request, reserve_api_request


NOW = datetime(2026, 9, 22, 12, 0, 10, tzinfo=timezone.utc)


class ApiRateLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        db_admin.migrate_database(self.path)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "rate-client", actor="test", now=NOW)
            first = issue_api_key(db, consumer, {"read:items"}, actor="test",
                                  expires_at=NOW + timedelta(days=1), now=NOW)
            second = issue_api_key(db, consumer, {"read:items"}, actor="test",
                                   expires_at=NOW + timedelta(days=1), now=NOW)
            self.first = authenticate_api_key(db, first.token, now=NOW)
            self.second = authenticate_api_key(db, second.token, now=NOW)

    def reserve(self, principal, *, now=NOW, rate=60, concurrency=5, lease_seconds=30):
        with database.get_db(self.path) as db:
            return reserve_api_request(
                db, principal, rate_per_minute=rate,
                consumer_concurrency=concurrency,
                lease_seconds=lease_seconds, now=now,
            )

    def release(self, lease_id):
        with database.get_db(self.path) as db:
            release_api_request(db, lease_id)

    def test_per_key_fixed_minute_limit_and_release_does_not_refund(self):
        first = self.reserve(self.first, rate=2)
        self.assertTrue(first.allowed)
        self.release(first.lease_id)
        second = self.reserve(self.first, rate=2)
        self.assertTrue(second.allowed)
        self.release(second.lease_id)
        denied = self.reserve(self.first, rate=2)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "rate")
        self.assertEqual(denied.retry_after, 50)
        self.assertTrue(self.reserve(self.second, rate=2).allowed)
        self.assertTrue(self.reserve(self.first, rate=2,
                                     now=NOW + timedelta(seconds=50)).allowed)

    def test_concurrency_is_shared_across_keys_and_expired_leases_recover(self):
        first = self.reserve(self.first, concurrency=1)
        denied = self.reserve(self.second, concurrency=1)
        self.assertTrue(first.allowed)
        self.assertEqual((denied.allowed, denied.reason, denied.retry_after),
                         (False, "concurrency", 1))
        self.release(first.lease_id)
        self.assertTrue(self.reserve(self.second, concurrency=1).allowed)
        recovered = self.reserve(self.first, concurrency=1,
                                 now=NOW + timedelta(seconds=31))
        self.assertTrue(recovered.allowed)
        with database.get_db(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_request_leases").fetchone()[0], 1)

    def test_separate_connections_admit_at_most_five_concurrent_requests(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.reserve(self.first), range(8)))
        self.assertEqual(sum(result.allowed for result in results), 5)
        self.assertEqual(sum(result.reason == "concurrency" for result in results), 3)
        with database.get_db(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_request_leases").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT used_count FROM api_rate_buckets").fetchone()[0], 5)

    def test_upgrade_from_v23_keeps_existing_key_and_audit(self):
        predecessor = self.path.with_name("predecessor.db")
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:23])
            consumer = create_consumer(db, "old", actor="test", now=NOW)
            key = issue_api_key(db, consumer, {"read:items"}, actor="test",
                                expires_at=NOW + timedelta(days=1), now=NOW)
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (24,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 23)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_key_audit").fetchone()[0], 2)
            self.assertIsNotNone(authenticate_api_key(db, key.token, now=NOW))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_rate_buckets").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
