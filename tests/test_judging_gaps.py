import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin, judging_gaps
from app.ai import pipeline, usage
from app.crawler import runner

T = datetime(2026, 10, 27, 1, 9, 24, tzinfo=timezone.utc)


class QuotaError(Exception):
    def __str__(self):
        return "Error code: 429 - {'error': {'code': '1113', 'message': '余额不足'}}"


class JudgingGapTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "app.db"
        for target, name in ((database, "DB_PATH"), (config, "DB_PATH")):
            item = patch.object(target, name, path)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def gaps(self, now):
        with database.get_db() as db:
            return judging_gaps.listed(db, now)

    def test_a_long_failure_is_one_gap_and_a_blip_is_none(self):
        with database.get_db() as db:
            judging_gaps.round_failed(db, "quota", T)
            judging_gaps.round_failed(db, "quota", T + timedelta(minutes=5))  # still the same gap
        self.assertEqual(self.gaps(T + timedelta(minutes=10)), [])  # not listed while short
        self.assertEqual(self.gaps(T + timedelta(hours=1)),
                         [{"from": "2026-10-27T01:09:24Z", "to": None, "reason": "自动记录：模型账户余额不足"}])
        with database.get_db() as db:
            judging_gaps.round_answered(db, T + timedelta(hours=11, minutes=47, seconds=40))
            judging_gaps.round_failed(db, "connection", T + timedelta(hours=12))
            judging_gaps.round_answered(db, T + timedelta(hours=12, minutes=5))  # a blip: dropped
        self.assertEqual(self.gaps(T + timedelta(hours=13)),
                         [{"from": "2026-10-27T01:09:24Z", "to": "2026-10-27T12:57:04Z",
                           "reason": "自动记录：模型账户余额不足"}])

    def test_a_scoring_round_with_no_answer_opens_a_gap_and_the_next_answer_closes_it(self):
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','info')")
        runner.insert_item("test", dict(url="https://e.test/1", title="Story", published_at="2026-10-27T00:00:00+00:00"))
        with patch.object(config, "llm_enabled", return_value=True):
            def fail(payload):
                usage.record("score", failed=True, error=QuotaError())
                raise QuotaError()
            with patch.object(pipeline, "_call_llm", side_effect=fail):
                pipeline.process_pending(limit=5, parallel=1)
            with database.get_db() as db:
                row = db.execute("SELECT started_at, ended_at, reason FROM judging_gaps").fetchone()
                self.assertEqual((row["ended_at"], row["reason"]), (None, "自动记录：模型账户余额不足"))
                db.execute("UPDATE judging_gaps SET started_at='2026-10-27T01:00:00Z'")  # long ago
            answer = [dict(id=1, tmt=True, score=50, title_zh="标题", summary_zh="摘要", reason="理由")]
            with patch.object(pipeline, "_call_llm", return_value=answer):
                pipeline.process_pending(limit=5, parallel=1)
        with database.get_db() as db:
            self.assertIsNotNone(db.execute("SELECT ended_at FROM judging_gaps").fetchone()[0])

    def test_migration_66_records_the_10_27_gap_for_production_only(self):
        for production, expected in ((True, [("2026-10-27T01:09:24Z", "2026-10-27T12:57:04Z")]), (False, [])):
            with self.subTest(production=production), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "app.db"
                with database.get_db(path) as db:
                    db_admin.apply_migrations(db, db_admin.MIGRATIONS[:65])
                    own = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
                with database.get_db(path) as db, \
                        patch.object(db_admin, "PRODUCTION_DATASET_ID", own if production else "dataset_other"):
                    db_admin.apply_migrations(db, db_admin.MIGRATIONS[:66])
                    rows = [tuple(row) for row in db.execute("SELECT started_at, ended_at FROM judging_gaps")]
                self.assertEqual(rows, expected)


if __name__ == "__main__":
    unittest.main()
