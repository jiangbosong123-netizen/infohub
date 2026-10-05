import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.jobs import claim_job, complete_job, enqueue_job, prune_succeeded_jobs
from app.worker import JOB_RETENTION_DAYS, _prune

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=JOB_RETENTION_DAYS + 1)
RECENT = NOW - timedelta(days=JOB_RETENTION_DAYS - 1)


class JobRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for target, name in ((database, "DB_PATH"), (config, "DB_PATH")):
            item = patch.object(target, name, self.path)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        self.counter = 0

    def job(self, kind: str, finished: datetime, *, succeed: bool = True) -> str:
        self.counter += 1
        enqueue_job(kind=kind, idempotency_key=f"{kind}:{self.counter}", scheduled_for=finished)
        job = claim_job(worker_id="retention", kinds=(kind,), lease_seconds=60, now=finished)
        if succeed:
            complete_job(job.id, job.lease_token, now=finished)
        return job.id

    def ids(self) -> set[str]:
        with database.get_db() as db:
            return {row[0] for row in db.execute("SELECT id FROM jobs")}

    def test_only_old_unreferenced_succeeded_routine_jobs_are_deleted(self):
        old_crawl = self.job("crawl", OLD)
        old_hot = self.job("curation-hot", OLD)
        recent_crawl = self.job("crawl", RECENT)
        running_crawl = self.job("crawl", OLD, succeed=False)
        old_analysis = self.job("analysis", OLD)
        referenced = self.job("report", OLD)
        with database.get_db() as db:
            dataset, epoch = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1").fetchone()
            db.execute(
                """INSERT INTO change_log(dataset_id,epoch,idempotency_key,resource_type,resource_id,
                       version_id,operation,available_at,payload_json,payload_sha256,hash_algorithm,
                       job_id,lease_token)
                   VALUES(?,?,'k','report','r','v','create',?,'{}',?,'jcs-sha256-v1',?,
                          (SELECT lease_token FROM job_attempts WHERE job_id=?))""",
                (dataset, epoch, OLD.isoformat(), "0" * 64, referenced, referenced))
            attempts = db.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0]
            self.assertEqual(attempts, 6)
            deleted = prune_succeeded_jobs(db, finished_before=NOW - timedelta(days=JOB_RETENTION_DAYS))
        self.assertEqual(deleted, 2)
        self.assertEqual(self.ids(), {recent_crawl, running_crawl, old_analysis, referenced})
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0], attempts - 2)
            self.assertFalse(db.execute(
                "SELECT 1 FROM job_attempts WHERE job_id IN (?,?)", (old_crawl, old_hot)).fetchone())
        db_admin.verify_database(self.path, require_current=True)

    def test_daily_prune_reports_deleted_routine_jobs(self):
        self.job("topic-statistics", datetime.now(timezone.utc) - timedelta(days=JOB_RETENTION_DAYS + 2))
        self.job("topic-statistics", datetime.now(timezone.utc) - timedelta(days=1))
        result = _prune()
        self.assertEqual(result["routine_jobs_deleted"], 1)
        self.assertEqual(len(self.ids()), 1)

    def test_every_key_that_refers_to_jobs_or_attempts_is_indexed(self):
        with database.get_db() as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            unindexed = []
            for table in tables:
                leading = {db.execute(f"PRAGMA index_info('{index[1]}')").fetchone()[2]
                           for index in db.execute(f"PRAGMA index_list('{table}')")}
                for key in db.execute(f"PRAGMA foreign_key_list('{table}')"):
                    if key[2] in ("jobs", "job_attempts") and key[3] not in leading:
                        unindexed.append(f"{table}.{key[3]}")
        self.assertEqual(unindexed, [])


if __name__ == "__main__":
    unittest.main()
