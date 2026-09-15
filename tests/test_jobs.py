from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import database
from app.jobs import (
    IdempotencyConflictError,
    InputVersionChangedError,
    JobError,
    LeaseLostError,
    block_job,
    cancel_job,
    claim_job,
    complete_job,
    enqueue_due_schedules,
    enqueue_job,
    fail_job,
    get_job,
    job_counts,
    renew_lease,
    upsert_interval_schedule,
)
from app.timeutil import format_utc, parse_utc


UTC = timezone.utc
T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "jobs.db"
        patcher = patch.object(database, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()

    def enqueue(self, key: str = "job:one", **overrides):
        values = {
            "kind": "normalize",
            "idempotency_key": key,
            "subject_id": "item:1",
            "input_version": "v1",
            "payload": {"item_id": 1},
            "scheduled_for": T0,
            "max_attempts": 3,
        }
        values.update(overrides)
        return enqueue_job(**values)

    def test_enqueue_is_idempotent_and_rejects_key_reuse(self):
        first = self.enqueue(payload={"b": 2, "a": 1})
        repeated = self.enqueue(payload={"a": 1, "b": 2})
        self.assertEqual(repeated.id, first.id)
        self.assertEqual(job_counts()["pending"], 1)
        with self.assertRaises(IdempotencyConflictError):
            self.enqueue(priority=10, payload={"a": 1, "b": 2})

        immediate = enqueue_job(kind="cleanup", idempotency_key="immediate")
        immediate_retry = enqueue_job(kind="cleanup", idempotency_key="immediate")
        self.assertEqual(immediate_retry.id, immediate.id)
        self.assertEqual(immediate_retry.scheduled_for, immediate.scheduled_for)

    def test_claim_selects_only_due_kind_then_highest_priority(self):
        self.enqueue("low", priority=1)
        high = self.enqueue("high", priority=10)
        self.enqueue("future", priority=100, scheduled_for=T0 + timedelta(minutes=1))
        self.enqueue("other-kind", kind="analyse", priority=20)

        claimed = claim_job(worker_id="worker-a", kinds=("normalize",), now=T0)
        self.assertEqual(claimed.id, high.id)
        self.assertEqual(claimed.state, "running")
        self.assertEqual(claimed.attempt_count, 1)
        self.assertEqual(claimed.heartbeat_at, format_utc(T0))
        self.assertIsNone(claim_job(worker_id="worker-b", kinds=("missing",), now=T0))

    def test_expired_lease_is_reclaimed_and_stale_worker_cannot_write(self):
        job = self.enqueue(max_attempts=2)
        first = claim_job(worker_id="worker-a", lease_seconds=10, now=T0)
        second = claim_job(
            worker_id="worker-b", lease_seconds=10, now=T0 + timedelta(seconds=11)
        )
        self.assertEqual(second.id, job.id)
        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertEqual(second.lease_generation, 2)

        with self.assertRaises(LeaseLostError):
            complete_job(
                job.id, first.lease_token, expected_input_version="v1",
                now=T0 + timedelta(seconds=12),
            )
        completed = complete_job(
            job.id, second.lease_token, expected_input_version="v1",
            result_ref="normalized:1", now=T0 + timedelta(seconds=12),
        )
        repeated = complete_job(
            job.id, second.lease_token, expected_input_version="v1",
            result_ref="normalized:1", now=T0 + timedelta(seconds=13),
        )
        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(repeated.to_dict(), completed.to_dict())
        with self.assertRaises(IdempotencyConflictError):
            complete_job(
                job.id, second.lease_token, expected_input_version="v1",
                result_ref="different", now=T0 + timedelta(seconds=13),
            )
        with sqlite3.connect(self.path) as db:
            statuses = [row[0] for row in db.execute(
                "SELECT status FROM job_attempts ORDER BY attempt_number"
            )]
        self.assertEqual(statuses, ["lease_expired", "succeeded"])

    def test_expired_lease_cannot_complete_before_reclaim(self):
        job = self.enqueue()
        claimed = claim_job(worker_id="worker-a", lease_seconds=10, now=T0)
        with self.assertRaisesRegex(LeaseLostError, "expired"):
            complete_job(
                job.id, claimed.lease_token, now=T0 + timedelta(seconds=10)
            )
        self.assertEqual(get_job(job.id).state, "running")

    def test_renewal_extends_only_the_current_live_lease(self):
        job = self.enqueue()
        claimed = claim_job(worker_id="worker-a", lease_seconds=10, now=T0)
        renewed = renew_lease(
            job.id, claimed.lease_token, lease_seconds=20,
            now=T0 + timedelta(seconds=5),
        )
        self.assertEqual(
            renewed.lease_expires_at, format_utc(T0 + timedelta(seconds=25))
        )
        self.assertEqual(renewed.heartbeat_at, format_utc(T0 + timedelta(seconds=5)))
        complete_job(
            job.id, claimed.lease_token, expected_input_version="v1",
            now=T0 + timedelta(seconds=15),
        )
        with self.assertRaises(LeaseLostError):
            renew_lease(job.id, claimed.lease_token, now=T0 + timedelta(seconds=16))

    def test_failures_retry_then_move_to_dead_letter(self):
        job = self.enqueue(max_attempts=2)
        first = claim_job(worker_id="worker-a", now=T0)
        retry = fail_job(
            job.id, first.lease_token, error_code="upstream_timeout",
            error_detail="temporary", now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(retry.state, "retry_wait")
        self.assertEqual(retry.next_attempt_at, format_utc(T0 + timedelta(seconds=61)))
        self.assertIsNone(claim_job(worker_id="early", now=T0 + timedelta(seconds=60)))

        second = claim_job(worker_id="worker-b", now=T0 + timedelta(seconds=62))
        dead = fail_job(
            job.id, second.lease_token, error_code="upstream_timeout",
            now=T0 + timedelta(seconds=63),
        )
        self.assertEqual(dead.state, "dead_letter")
        self.assertEqual(dead.finished_at, format_utc(T0 + timedelta(seconds=63)))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM job_attempts WHERE status='failed'").fetchone()[0],
                2,
            )

    def test_input_version_is_a_completion_fence(self):
        job = self.enqueue(input_version="source-v7")
        claimed = claim_job(worker_id="worker-a", now=T0)
        with self.assertRaises(InputVersionChangedError):
            complete_job(job.id, claimed.lease_token, now=T0)
        with self.assertRaises(InputVersionChangedError):
            complete_job(
                job.id, claimed.lease_token, expected_input_version="source-v8", now=T0
            )
        self.assertEqual(get_job(job.id).state, "running")
        self.assertEqual(
            complete_job(
                job.id, claimed.lease_token, expected_input_version="source-v7", now=T0
            ).state,
            "succeeded",
        )

    def test_block_and_cancel_transitions_are_auditable(self):
        blocked_job = self.enqueue("blocked")
        claimed = claim_job(worker_id="worker-a", now=T0)
        blocked = block_job(
            blocked_job.id, claimed.lease_token, error_code="operator_required", now=T0
        )
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(cancel_job(blocked.id, now=T0).state, "cancelled")

        running_job = self.enqueue("running")
        running = claim_job(worker_id="worker-b", now=T0)
        with self.assertRaises(LeaseLostError):
            cancel_job(running_job.id, now=T0)
        cancelled = cancel_job(
            running_job.id, lease_token=running.lease_token, now=T0
        )
        self.assertEqual(cancelled.state, "cancelled")

        terminal_job = self.enqueue("terminal")
        terminal_claim = claim_job(worker_id="worker-c", now=T0)
        complete_job(
            terminal_job.id, terminal_claim.lease_token,
            expected_input_version="v1", now=T0,
        )
        with self.assertRaises(JobError):
            cancel_job(terminal_job.id, now=T0)

    def test_overdue_schedule_coalesces_and_records_success(self):
        upsert_interval_schedule(
            schedule_id="crawl:feeds", kind="crawl", next_due_at=T0,
            interval_seconds=60, payload={"source_group": "feeds"}, max_attempts=2,
        )
        jobs = enqueue_due_schedules(now=T0 + timedelta(seconds=185))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].scheduled_for, format_utc(T0))
        self.assertEqual(enqueue_due_schedules(now=T0 + timedelta(seconds=185)), ())
        with sqlite3.connect(self.path) as db:
            next_due = db.execute(
                "SELECT next_due_at FROM schedules WHERE id='crawl:feeds'"
            ).fetchone()[0]
        self.assertEqual(next_due, format_utc(T0 + timedelta(seconds=240)))

        claimed = claim_job(worker_id="scheduler-worker", kinds=("crawl",),
                            now=T0 + timedelta(seconds=185))
        complete_job(
            claimed.id, claimed.lease_token,
            expected_input_version=claimed.input_version,
            now=T0 + timedelta(seconds=186),
        )
        with sqlite3.connect(self.path) as db:
            last_success = db.execute(
                "SELECT last_success_at FROM schedules WHERE id='crawl:feeds'"
            ).fetchone()[0]
        self.assertEqual(last_success, format_utc(T0 + timedelta(seconds=186)))

        upsert_interval_schedule(
            schedule_id="crawl:feeds", kind="crawl",
            next_due_at=T0 + timedelta(seconds=999), interval_seconds=60,
            payload={"source_group": "feeds"}, max_attempts=2,
        )
        with sqlite3.connect(self.path) as db:
            preserved = db.execute(
                "SELECT next_due_at FROM schedules WHERE id='crawl:feeds'"
            ).fetchone()[0]
        self.assertEqual(preserved, format_utc(T0 + timedelta(seconds=240)))

    def test_changed_schedule_config_rejects_old_result(self):
        upsert_interval_schedule(
            schedule_id="crawl:feeds", kind="crawl", next_due_at=T0,
            interval_seconds=60, payload={"revision": 1},
        )
        old_job = enqueue_due_schedules(now=T0)[0]
        claimed = claim_job(worker_id="worker-a", now=T0)
        upsert_interval_schedule(
            schedule_id="crawl:feeds", kind="crawl",
            next_due_at=T0 + timedelta(seconds=60), interval_seconds=60,
            payload={"revision": 2},
        )
        with self.assertRaisesRegex(InputVersionChangedError, "schedule configuration"):
            complete_job(
                old_job.id, claimed.lease_token,
                expected_input_version=claimed.input_version, now=T0,
            )
        self.assertEqual(get_job(old_job.id).state, "running")

    def test_concurrent_workers_cannot_claim_the_same_job(self):
        job = self.enqueue()

        def claim(worker: str):
            return claim_job(worker_id=worker, now=T0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("worker-a", "worker-b")))
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].id, job.id)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0], 1)

    def test_timestamps_are_canonical_utc_and_naive_values_fail(self):
        self.assertEqual(format_utc(T0), "2026-09-15T12:00:00.000000Z")
        self.assertEqual(parse_utc(format_utc(T0)), T0)
        with self.assertRaises(ValueError):
            format_utc(datetime(2026, 9, 15, 12, 0))
        with self.assertRaises(ValueError):
            self.enqueue(scheduled_for="2026-09-15T12:00:00")


if __name__ == "__main__":
    unittest.main()
