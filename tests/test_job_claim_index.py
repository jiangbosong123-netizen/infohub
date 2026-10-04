import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.jobs import claim_job, enqueue_job

CLAIM_SQL = """SELECT id FROM jobs WHERE state IN ('pending','retry_wait') AND scheduled_for<=?
               AND next_attempt_at<=? AND dataset_epoch=? AND kind IN (?)
               ORDER BY priority DESC,next_attempt_at,scheduled_for,created_at,id LIMIT 1"""


class JobClaimIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for item in (patch.object(database, "DB_PATH", self.path), patch.object(config, "DB_PATH", self.path)):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def test_claim_query_reads_the_ordered_partial_index_without_sorting(self):
        with database.get_db() as db:
            epoch = db.execute("SELECT current_epoch FROM dataset_state").fetchone()[0]
            plan = [row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN " + CLAIM_SQL, ("9999", "9999", epoch, "k"))]
        self.assertTrue(any("idx_jobs_claim_order" in step for step in plan), plan)
        self.assertFalse(any("TEMP B-TREE" in step for step in plan), plan)

    def test_claims_follow_priority_then_due_time_order(self):
        now = datetime(2026, 10, 4, tzinfo=timezone.utc)
        specs = [("low-early", 0, -30), ("high-late", 5, -1), ("high-early", 5, -20), ("mid", 3, -40)]
        for key, priority, minutes in specs:
            enqueue_job(kind="k", idempotency_key=key, payload={"key": key}, priority=priority,
                        scheduled_for=(now + timedelta(minutes=minutes)).isoformat())
        claimed = []
        while (job := claim_job(worker_id="w", kinds=("k",), now=now.isoformat())) is not None:
            claimed.append(job.payload["key"])
        self.assertEqual(claimed, ["high-early", "high-late", "mid", "low-early"])

    def test_migration_45_adds_only_the_index_to_a_schema_44_database(self):
        predecessor = self.path.with_name("schema44.db")
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:44])
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (45, 46))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 44)
        with sqlite3.connect(predecessor) as db:
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_jobs_claim_order'").fetchone()[0]
        self.assertIn("WHERE state IN ('pending','retry_wait')", sql)


if __name__ == "__main__":
    unittest.main()
