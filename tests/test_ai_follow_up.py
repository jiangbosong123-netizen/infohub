import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, worker
from app.ai import pipeline


class AiFollowUpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "DB_PATH", Path(folder.name) / "app.db")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def ai_jobs(self):
        with database.get_db() as db:
            return [tuple(row) for row in db.execute(
                "SELECT priority, state FROM jobs WHERE kind='ai' ORDER BY id")]

    def run_ai(self, batches):
        with patch.object(pipeline, "process_pending", side_effect=batches):
            return worker._ai()

    def test_a_run_that_used_its_whole_budget_queues_the_next_one(self):
        result = self.run_ai([worker.AI_BATCH] * worker.AI_BATCHES_PER_JOB)
        self.assertEqual(result["processed"], worker.AI_BATCH * worker.AI_BATCHES_PER_JOB)
        self.assertTrue(result["follow_up"])
        self.assertEqual(self.ai_jobs(), [(worker.AI_FOLLOW_UP_PRIORITY, "pending")])

    def test_a_drained_backlog_or_a_failing_model_does_not_chain(self):
        for batches in ([worker.AI_BATCH, 7, 0], [0], [worker.AI_BATCH] * 3 + [12, 0]):
            with self.subTest(batches=batches):
                self.assertFalse(self.run_ai(batches)["follow_up"])
        self.assertEqual(self.ai_jobs(), [])

    def test_a_short_batch_from_a_failed_half_does_not_end_the_run(self):
        # One half of a batch failed: 15 judged, the queue is not empty, keep going and chain.
        result = self.run_ai([15] + [worker.AI_BATCH] * (worker.AI_BATCHES_PER_JOB - 1))
        self.assertEqual(result["processed"], 15 + worker.AI_BATCH * (worker.AI_BATCHES_PER_JOB - 1))
        self.assertTrue(result["follow_up"])

    def test_daily_jobs_go_before_the_ai_tick_and_follow_ups(self):
        worker.register_default_schedules()
        with database.get_db() as db:
            stored = {row["kind"]: row["priority"] for row in db.execute("SELECT kind, priority FROM schedules")}
        for kind in ("reconcile", "report", "backup", "prune"):
            self.assertGreater(stored[kind], stored["ai"])
        self.assertGreater(stored["ai"], worker.AI_FOLLOW_UP_PRIORITY)

    def test_a_daily_job_queued_under_the_old_order_is_lifted(self):
        from app.jobs import enqueue_job
        old = enqueue_job(kind="reconcile", idempotency_key="reconcile:old", priority=20)
        worker.register_default_schedules()
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT priority FROM jobs WHERE id=?", (old.id,)).fetchone()[0], 40)


if __name__ == "__main__":
    unittest.main()
