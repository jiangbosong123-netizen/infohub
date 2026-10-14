import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.jobs import claim_job, enqueue_job, get_job
from app.runtime_health import worker_heartbeat_path
from app.worker import CRAWL_LANE_KINDS, MAIN_LANE_KINDS, JOB_KINDS, HeartbeatPump, run_lane, run_lanes


class WorkerLaneTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        for target, name, value in ((database, "DB_PATH", root / "app.db"), (config, "DB_PATH", root / "app.db"),
                                    (config, "RUNTIME_PATH", root / "runtime"),
                                    (config, "WORKER_POLL_SECONDS", 0.05)):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        self.pump = HeartbeatPump("worker-1", "2026-10-14T00:00:00.000000Z")

    def heartbeat(self):
        return json.loads(worker_heartbeat_path().read_text(encoding="utf-8"))

    def test_every_job_kind_belongs_to_exactly_one_lane(self):
        self.assertEqual(sorted(CRAWL_LANE_KINDS + MAIN_LANE_KINDS), sorted(JOB_KINDS))
        self.assertFalse(set(CRAWL_LANE_KINDS) & set(MAIN_LANE_KINDS))

    def test_a_crawl_runs_while_the_main_lane_is_busy(self):
        crawled = threading.Event()
        seen = {}

        def ai():  # a long AI tick: it ends only once the crawl has run beside it
            seen["crawl_ran_meanwhile"] = crawled.wait(10)
            return {}

        def crawl():
            seen["lanes_busy"] = self.heartbeat()["active_job_ids"]
            crawled.set()
            return {}

        handlers = {"ai": ai, "crawl": crawl}
        ai_job = enqueue_job(kind="ai", idempotency_key="ai-1", priority=30)
        stopping = threading.Event()
        lanes = [threading.Thread(target=run_lane, kwargs=dict(
                     lane=lane, kinds=kinds, worker_id=f"worker-1:{lane}", pump=self.pump,
                     stopping=stopping, handlers=handlers))
                 for lane, kinds in (("main", MAIN_LANE_KINDS), ("crawl", CRAWL_LANE_KINDS))]
        for lane in lanes:
            lane.start()
        while get_job(ai_job.id).state != "running":
            time.sleep(0.02)
        crawl_job = enqueue_job(kind="crawl", idempotency_key="crawl-1", priority=50)
        deadline = time.monotonic() + 15
        while {get_job(ai_job.id).state, get_job(crawl_job.id).state} != {"succeeded"}:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        stopping.set()
        for lane in lanes:
            lane.join(5)
        self.assertTrue(seen["crawl_ran_meanwhile"])
        self.assertEqual(set(seen["lanes_busy"]), {"main", "crawl"})
        self.assertEqual(self.heartbeat()["active_job_ids"], {})

    def test_the_worker_runs_both_lanes_and_waits_for_the_crawl_in_hand(self):
        stopping = threading.Event()
        crawled, order = threading.Event(), []

        def ai():  # stops the worker once a crawl has started beside it
            crawled.wait(10)
            stopping.set()
            order.append("ai")
            return {}

        def crawl():
            crawled.set()
            time.sleep(0.3)  # still running when the worker is told to stop
            order.append("crawl")
            return {}

        enqueue_job(kind="ai", idempotency_key="ai-1")
        enqueue_job(kind="crawl", idempotency_key="crawl-1")
        run_lanes(worker_id="worker-1", pump=self.pump, stopping=stopping,
                  handlers={"ai": ai, "crawl": crawl})
        self.assertEqual(order, ["ai", "crawl"])  # returned only after the crawl finished

    def test_a_crawl_that_falls_due_while_the_main_lane_is_busy_runs_beside_it(self):
        from datetime import datetime, timedelta, timezone
        from app.jobs import upsert_interval_schedule
        now = datetime.now(timezone.utc)
        upsert_interval_schedule(schedule_id="ai:test", kind="ai", next_due_at=now - timedelta(seconds=1),
                                 interval_seconds=3600, priority=30, max_attempts=1)
        # Not due yet when the AI job starts: only a lane that enqueues meanwhile can start it.
        upsert_interval_schedule(schedule_id="crawl:test", kind="crawl", next_due_at=now + timedelta(seconds=1),
                                 interval_seconds=3600, priority=50, max_attempts=1)
        stopping, crawled, seen = threading.Event(), threading.Event(), {}

        def ai():
            seen["crawl_ran_meanwhile"] = crawled.wait(10)
            stopping.set()
            return {}

        run_lanes(worker_id="worker-1", pump=self.pump, stopping=stopping,
                  handlers={"ai": ai, "crawl": lambda: crawled.set() or {}})
        self.assertTrue(seen["crawl_ran_meanwhile"])

    def test_the_worker_is_degraded_while_any_lane_is(self):
        self.pump.set_state("degraded", "crawl")
        self.pump.set_state("running", "main")
        self.assertEqual(self.heartbeat()["state"], "degraded")
        self.pump.set_state("running", "crawl")
        self.assertEqual(self.heartbeat()["state"], "running")

    def test_the_heartbeat_renews_the_lease_of_each_lane(self):
        enqueue_job(kind="ai", idempotency_key="ai-1")
        enqueue_job(kind="crawl", idempotency_key="crawl-1")
        main = claim_job(worker_id="worker-1", kinds=MAIN_LANE_KINDS, lease_seconds=60)
        crawl = claim_job(worker_id="worker-1:crawl", kinds=CRAWL_LANE_KINDS, lease_seconds=60)
        with patch.object(config, "WORKER_HEARTBEAT_SECONDS", 0.05):
            self.pump.set_active(main, "main")
            self.pump.set_active(crawl, "crawl")
            self.pump.start()
            deadline = time.monotonic() + 10
            while any(get_job(job.id).lease_expires_at == job.lease_expires_at for job in (main, crawl)):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.pump.close()
        for job in (main, crawl):  # renewed for WORKER_LEASE_SECONDS (300 s) instead of 60 s
            self.assertGreater(get_job(job.id).lease_expires_at, job.lease_expires_at)

    def test_the_heartbeat_names_the_job_of_each_lane(self):
        main = enqueue_job(kind="ai", idempotency_key="ai-1")
        crawl = enqueue_job(kind="crawl", idempotency_key="crawl-1")
        self.pump.set_active(crawl, "crawl")
        self.assertEqual(self.heartbeat()["active_job_id"], crawl.id)
        self.pump.set_active(main, "main")
        value = self.heartbeat()
        self.assertEqual((value["active_job_id"], value["active_job_ids"]),
                         (main.id, {"crawl": crawl.id, "main": main.id}))
        self.pump.set_active(None, "main")
        self.assertEqual(self.heartbeat()["active_job_ids"], {"crawl": crawl.id})


if __name__ == "__main__":
    unittest.main()
