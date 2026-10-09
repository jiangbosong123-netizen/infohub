import shutil
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app import config, database, external_heartbeat, self_check
from app.self_check import Problem, find_problems, recorded
from app.timeutil import format_utc
from app.web import routes

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
URL = "https://hc-ping.example/0b6c8e1e-secret-token"
Usage = namedtuple("Usage", "total used free")


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, name, value in (
            (database, "DB_PATH", self.root / "app.db"), (config, "DB_PATH", self.root / "app.db"),
            (config, "BLOB_PATH", self.root / "blobs"), (config, "BACKUP_PATH", self.root / "backups"),
            (config, "RUNTIME_PATH", self.root / "runtime"), (config, "ALERT_DISK_FREE_MB", 0),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        llm = patch.object(config, "llm_enabled", return_value=True)
        llm.start()
        self.addCleanup(llm.stop)
        database.init_schema()
        self.add_sources(6, failing=0)

    def add_sources(self, count, failing):
        with database.get_db() as db:
            db.execute("DELETE FROM sources")
            for n in range(count):
                db.execute("INSERT INTO sources(key,name,channel,type,enabled,fail_count) VALUES(?,?,?,?,1,?)",
                           (f"s{n}", f"S{n}", "ai", "rss", 3 if n < failing else 0))

    def add_items(self, age, scored, unscored):
        fetched = (NOW - age).isoformat()
        with database.get_db() as db:
            for n in range(scored + unscored):
                db.execute("""INSERT INTO items(source_id,url,title,channel,score,published_at,fetched_at)
                              VALUES(1,?,?,'ai',?,?,?)""",
                           (f"https://e.test/{age}/{n}", f"t{n}", 50 if n < scored else None, fetched, fetched))

    def add_job(self, state, age):
        stamp = format_utc(NOW - age)
        with database.get_db() as db:
            db.execute("""INSERT INTO jobs(id,kind,request_hash,idempotency_key,state,scheduled_for,
                              next_attempt_at,attempt_count,max_attempts,created_at,updated_at,finished_at)
                          VALUES(?,?,?,?,?,?,?,3,3,?,?,?)""",
                       (f"job-{state}-{age}", "report", "h", f"key-{state}-{age}", state, stamp, stamp,
                        stamp, stamp, stamp))

    def codes(self):
        return [problem.code for problem in find_problems(NOW)]

    def test_a_working_system_has_no_problems(self):
        self.add_items(timedelta(hours=2), scored=10, unscored=1)
        self.assertEqual(self.codes(), [])

    def test_most_sources_failing(self):
        self.add_sources(6, failing=3)
        self.assertEqual(self.codes(), [])  # half is not most
        self.add_sources(6, failing=4)
        self.assertEqual(find_problems(NOW), [Problem("sources_failing", "4/6 个来源最近一次抓取失败")])
        self.add_sources(4, failing=4)
        self.assertEqual(self.codes(), [])  # too few sources to judge

    def test_ai_off_or_stalled(self):
        with patch.object(config, "llm_enabled", return_value=False):
            self.assertEqual(self.codes(), ["ai_off"])
        self.add_items(timedelta(minutes=30), scored=0, unscored=20)  # not yet due
        self.add_items(timedelta(hours=8), scored=0, unscored=20)  # an old backlog drains slowly
        self.add_items(timedelta(hours=2), scored=5, unscored=5)
        self.assertEqual(self.codes(), [])
        self.add_items(timedelta(hours=3), scored=0, unscored=1)
        self.assertEqual(find_problems(NOW), [Problem("ai_stalled", "1–6 小时前抓到的 11 条中 6 条仍未评分")])

    def test_jobs_that_ran_out_of_attempts_in_the_last_day(self):
        self.add_job("dead_letter", timedelta(hours=30))
        self.add_job("succeeded", timedelta(hours=1))
        self.assertEqual(self.codes(), [])
        self.add_job("dead_letter", timedelta(hours=2))
        self.add_job("blocked", timedelta(hours=3))
        self.assertEqual(find_problems(NOW), [Problem("jobs_failed", "24 小时内重试用尽的后台任务：report ×2")])

    def test_backup_age(self):
        from app.worker import register_default_schedules
        register_default_schedules(NOW)
        with database.get_db() as db:
            db.execute("UPDATE schedules SET created_at=? WHERE id='maintenance:backup'",
                       (format_utc(NOW - timedelta(hours=10)),))
        self.assertEqual(self.codes(), [])  # the first night has not come yet
        with database.get_db() as db:
            db.execute("UPDATE schedules SET created_at=? WHERE id='maintenance:backup'",
                       (format_utc(NOW - timedelta(hours=40)),))
        self.assertEqual(find_problems(NOW), [Problem("backup_stale", "夜间备份从未成功")])
        nightly = config.BACKUP_PATH / "nightly"
        (nightly / "app.production.20261008T030000.000000Z.bundle").mkdir(parents=True)
        self.assertEqual(find_problems(NOW + timedelta(hours=2)), [])  # 35 hours
        self.assertEqual(find_problems(NOW + timedelta(hours=4)),
                         [Problem("backup_stale", "最新夜间备份是 37 小时前")])

    def test_low_disk(self):
        with patch.object(config, "ALERT_DISK_FREE_MB", 10), \
                patch.object(shutil, "disk_usage", return_value=Usage(0, 0, 9 * self_check.MB)):
            [problem] = find_problems(NOW)
        self.assertEqual(problem.code, "disk_low")
        self.assertIn("9 MB（告警线 10 MB）", problem.detail)
        with patch.object(config, "ALERT_DISK_FREE_MB", 10), \
                patch.object(shutil, "disk_usage", return_value=Usage(0, 0, 10 * self_check.MB)):
            self.assertEqual(find_problems(NOW), [])


class HeartbeatReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for name, value in (("RUNTIME_PATH", Path(temporary.name)), ("EXTERNAL_HEARTBEAT_URL", URL)):
            item = patch.object(config, name, value)
            item.start()
            self.addCleanup(item.stop)
        response = httpx.Response(200, request=httpx.Request("GET", URL))
        self.get = patch.object(external_heartbeat.httpx, "get", return_value=response).start()
        self.post = patch.object(external_heartbeat.httpx, "post", return_value=response).start()
        self.addCleanup(patch.stopall)

    def test_the_fail_url(self):
        self.assertEqual(external_heartbeat.fail_url(URL), URL + "/fail")
        self.assertEqual(external_heartbeat.fail_url("https://h.example/a/b/?rid=1"), "https://h.example/a/b/fail?rid=1")

    def test_a_clean_check_pings_alive_and_is_recorded(self):
        with patch.object(self_check, "find_problems", return_value=[]):
            self.assertTrue(external_heartbeat.report_cycle())
        self.get.assert_called_once()
        self.post.assert_not_called()
        self.assertEqual(recorded()["problems"], [])

    def test_problems_ping_the_fail_url_with_the_reasons(self):
        problems = [Problem("disk_low", "只剩 9 MB"), Problem("ai_off", "未配置模型")]
        with patch.object(self_check, "find_problems", return_value=problems), \
                self.assertLogs("app.external_heartbeat", "WARNING") as logs:
            self.assertTrue(external_heartbeat.report_cycle())
        self.get.assert_not_called()
        self.assertEqual(self.post.call_args.args[0], URL + "/fail")
        self.assertEqual(self.post.call_args.kwargs["content"].decode(), "disk_low: 只剩 9 MB\nai_off: 未配置模型")
        self.assertEqual(logs.output, ["WARNING:app.external_heartbeat:self-check found: disk_low, ai_off"])
        self.assertEqual(recorded()["problems"], [problem.to_dict() for problem in problems])

    def test_a_broken_check_is_itself_reported(self):
        with patch.object(self_check, "find_problems", side_effect=OSError("secret-token")), \
                self.assertLogs("app.external_heartbeat", "WARNING") as logs:
            external_heartbeat.report_cycle()
        self.assertEqual(self.post.call_args.kwargs["content"].decode(), "self_check_error: OSError")
        self.assertNotIn("secret-token", "\n".join(logs.output))

    def test_the_check_and_record_run_without_a_monitor(self):
        with patch.object(config, "EXTERNAL_HEARTBEAT_URL", ""), \
                patch.object(self_check, "find_problems", return_value=[Problem("ai_off", "x")]), \
                self.assertLogs("app.external_heartbeat", "WARNING"):
            self.assertFalse(external_heartbeat.report_cycle())
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(recorded()["problems"], [{"code": "ai_off", "detail": "x"}])

    def test_the_pipeline_page_shows_the_recorded_result(self):
        self.assertIsNone(recorded())
        self_check.record([Problem("disk_low", "只剩 9 MB")], NOW)
        with patch.object(routes, "_system_snapshot", return_value={
                "pipeline": {"status": "ok", "issues": []}, "version": "v", "worker": {}, "sources": {},
                "jobs": {}, "ingest": {}, "items": {}, "reports": {}, "checked_at": "t"}):
            page = routes.api_pipeline()
        self.assertEqual(page["self_check"], {"checked_at": "2026-10-09T12:00:00.000000Z",
                                              "problems": [{"code": "disk_low", "detail": "只剩 9 MB"}]})


if __name__ == "__main__":
    unittest.main()
