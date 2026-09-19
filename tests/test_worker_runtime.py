import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import cli
from app import config, database
from app.jobs import claim_job, enqueue_job, get_job
from app.runtime_health import (
    read_worker_heartbeat,
    worker_heartbeat_path,
    write_worker_heartbeat,
)
from app.worker import process_one_job, register_default_schedules


T0 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class WorkerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database_path = self.root / "app.db"
        patches = (
            patch.object(database, "DB_PATH", self.database_path),
            patch.object(config, "DB_PATH", self.database_path),
            patch.object(config, "RUNTIME_PATH", self.root / "runtime"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def test_default_schedules_are_durable_and_restart_preserves_due_time(self):
        register_default_schedules(T0)
        with database.get_db() as db:
            first = {
                row["id"]: row["next_due_at"]
                for row in db.execute("SELECT id,next_due_at FROM schedules")
            }
        self.assertEqual(len(first), 5)
        register_default_schedules(T0 + timedelta(minutes=1))
        with database.get_db() as db:
            second = {
                row["id"]: row["next_due_at"]
                for row in db.execute("SELECT id,next_due_at FROM schedules")
            }
        self.assertEqual(second, first)
        changed_hour = (config.REPORT_HOUR + 1) % 24
        with patch.object(config, "REPORT_HOUR", changed_hour):
            register_default_schedules(T0 + timedelta(minutes=2))
        with database.get_db() as db:
            changed = {
                row["id"]: row["next_due_at"]
                for row in db.execute("SELECT id,next_due_at FROM schedules")
            }
        self.assertNotEqual(changed["report:daily"], first["report:daily"])
        self.assertEqual(changed["reconcile:daily"], first["reconcile:daily"])

    def test_search_refresh_schedule_can_be_enabled_and_disabled(self):
        with patch.object(config, "CURATION_SEARCH_ENABLED", True):
            register_default_schedules(T0)
        with database.get_db() as db:
            row = db.execute("SELECT enabled,interval_seconds FROM schedules WHERE id='curation-search:refresh'").fetchone()
            self.assertEqual(tuple(row), (1, 60))
        with patch.object(config, "CURATION_SEARCH_ENABLED", False):
            register_default_schedules(T0 + timedelta(minutes=1))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT enabled FROM schedules WHERE id='curation-search:refresh'").fetchone()[0], 0)

    def test_hot_refresh_schedule_can_be_enabled_and_disabled(self):
        with patch.object(config, "CURATION_HOT_ENABLED", True):
            register_default_schedules(T0)
        with database.get_db() as db:
            row = db.execute("SELECT enabled,interval_seconds FROM schedules WHERE id='curation-hot:refresh'").fetchone()
            self.assertEqual(tuple(row), (1, 60))
        with patch.object(config, "CURATION_HOT_ENABLED", False):
            register_default_schedules(T0 + timedelta(minutes=1))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT enabled FROM schedules WHERE id='curation-hot:refresh'").fetchone()[0], 0)

    def test_durable_hot_refresh_job_builds_and_skips_when_disabled(self):
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/1','Headline','ai',?,?)""",
                       (T0.isoformat(), T0.isoformat()))
            db.execute("""INSERT INTO stories(id,title,channel,url,first_at,last_at)
                          VALUES('story-one','Headline','ai','https://example.test/1',?,?)""",
                       (T0.isoformat(), T0.isoformat()))
            db.execute("INSERT INTO story_items(item_id,story_id) VALUES(1,'story-one')")
        enqueue_job(kind="curation-hot", idempotency_key="hot-fixture", scheduled_for=T0)
        with patch.object(config, "CURATION_HOT_ENABLED", True):
            result = process_one_job(worker_id="hot-worker", now=T0)
        self.assertEqual(result.state, "succeeded")
        with database.get_db() as db:
            state = db.execute("SELECT status,indexed_count FROM curation_story_metrics_state").fetchone()
            self.assertEqual(tuple(state), ("ready", 1))
        enqueue_job(kind="curation-hot", idempotency_key="hot-disabled", scheduled_for=T0)
        with patch.object(config, "CURATION_HOT_ENABLED", False):
            skipped = process_one_job(worker_id="hot-worker", now=T0)
        self.assertEqual(skipped.state, "succeeded")
        self.assertIn('"status": "disabled"', skipped.result_ref)

    def test_durable_search_refresh_job_builds_without_models(self):
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            for item_id in range(1, 4):
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                              VALUES(?,1,?,'Searchable item','ai',?,?)""",
                           (item_id, f"https://example.test/{item_id}",
                            "2026-09-10T09:00:00+00:00", "2026-09-10T09:01:00+00:00"))
        enqueue_job(kind="curation-search", idempotency_key="refresh-fixture", scheduled_for=T0)
        with patch.object(config, "CURATION_SEARCH_ENABLED", True):
            result = process_one_job(worker_id="search-worker", now=T0)
        self.assertEqual(result.state, "succeeded")
        with database.get_db() as db:
            state = db.execute("SELECT status,indexed_count FROM curation_search_state").fetchone()
            self.assertEqual(tuple(state), ("ready", 3))
        enqueue_job(kind="curation-search", idempotency_key="disabled-fixture", scheduled_for=T0)
        with patch.object(config, "CURATION_SEARCH_ENABLED", False):
            skipped = process_one_job(worker_id="search-worker", now=T0)
        self.assertEqual(skipped.state, "succeeded")
        self.assertIn('"status": "disabled"', skipped.result_ref)

    def test_new_worker_reclaims_expired_job_after_restart(self):
        job = enqueue_job(
            kind="crawl", idempotency_key="restart-fixture", scheduled_for=T0,
            max_attempts=3,
        )
        dead = claim_job(worker_id="dead-worker", lease_seconds=1, now=T0)
        self.assertEqual(dead.id, job.id)
        result = process_one_job(
            worker_id="replacement-worker",
            handlers={"crawl": lambda: {"ran": 0, "results": []}},
            now=T0 + timedelta(seconds=2),
            lease_seconds=30,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(get_job(job.id).attempt_count, 2)
        with database.get_db() as db:
            attempts = [
                row[0]
                for row in db.execute(
                    "SELECT status FROM job_attempts ORDER BY attempt_number"
                )
            ]
        self.assertEqual(attempts, ["lease_expired", "succeeded"])

    def test_worker_failure_is_retried_without_persisting_secret_text(self):
        enqueue_job(
            kind="crawl", idempotency_key="secret-fixture", scheduled_for=T0,
            max_attempts=2,
        )

        def fail():
            raise RuntimeError("upstream token=secret-value")

        with self.assertLogs("app.worker", level="ERROR") as captured:
            result = process_one_job(
                worker_id="worker-a", handlers={"crawl": fail}, now=T0,
                lease_seconds=30,
            )
        self.assertEqual(result.state, "retry_wait")
        self.assertNotIn("secret-value", result.error_detail)
        self.assertIn("[redacted]", result.error_detail)
        self.assertNotIn("secret-value", " ".join(captured.output))

    def test_worker_heartbeat_is_atomic_versioned_and_expires(self):
        write_worker_heartbeat(
            worker_id="worker-a", started_at=T0.isoformat(), heartbeat_at=T0
        )
        current = read_worker_heartbeat(
            expected_version=config.APP_VERSION,
            max_age_seconds=10,
            now=T0 + timedelta(seconds=5),
        )
        self.assertTrue(current.healthy)
        self.assertEqual(current.status, "healthy")
        wrong = read_worker_heartbeat(
            expected_version="different-release", max_age_seconds=10,
            now=T0 + timedelta(seconds=5),
        )
        self.assertEqual(wrong.status, "wrong_version")
        stale = read_worker_heartbeat(
            expected_version=config.APP_VERSION, max_age_seconds=10,
            now=T0 + timedelta(seconds=11),
        )
        self.assertEqual(stale.status, "stale")
        write_worker_heartbeat(
            worker_id="worker-a", started_at=T0.isoformat(),
            heartbeat_at=T0 + timedelta(
                seconds=config.WORKER_HEARTBEAT_FUTURE_TOLERANCE_SECONDS + 1
            ),
        )
        future = read_worker_heartbeat(
            expected_version=config.APP_VERSION, max_age_seconds=10, now=T0
        )
        self.assertEqual(future.status, "future")

    def test_web_start_never_initializes_or_runs_background_tasks(self):
        with patch.object(config, "PROCESS_ROLE", "web"), patch(
            "app.db_admin.verify_database"
        ) as verify, patch("uvicorn.run") as run, patch.object(
            cli, "cmd_init_db"
        ) as initialize:
            cli.cmd_serve()
        verify.assert_called_once_with(config.DB_PATH, require_current=True)
        run.assert_called_once()
        initialize.assert_not_called()

    def test_release_preparation_clears_previous_worker_heartbeat(self):
        write_worker_heartbeat(
            worker_id="old-worker", started_at=T0.isoformat(), heartbeat_at=T0
        )
        self.assertTrue(worker_heartbeat_path().exists())
        with patch.object(config, "PROCESS_ROLE", "maintenance"), patch.object(
            cli, "cmd_init_db"
        ) as initialize:
            cli.cmd_prepare_release()
        initialize.assert_called_once_with()
        self.assertFalse(worker_heartbeat_path().exists())


if __name__ == "__main__":
    unittest.main()
