import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, coverage_stats, database
from app.ai import pipeline, usage
from app.crawler import runner
from app.web import routes

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)  # 11:00 in Asia/Shanghai


def response(prompt=1200, completion=800, text="[]"):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
                           choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class TierMetricsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "DB_PATH", Path(folder.name) / "app.db")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        routes._coverage_stats_cache.clear()

    def usage_rows(self):
        with database.get_db() as db:
            return [dict(row) for row in db.execute(
                "SELECT task,calls,failed_calls,prompt_tokens,completion_tokens,items FROM ai_usage_daily ORDER BY task")]

    def test_each_model_call_adds_its_tokens_and_a_failure_counts_without_tokens(self):
        usage.call("score", lambda: response(), items=30)
        usage.call("score", lambda: response(prompt=100, completion=50), items=2)
        with self.assertRaises(TimeoutError):
            usage.call("score", lambda: (_ for _ in ()).throw(TimeoutError()), items=30)
        self.assertEqual(self.usage_rows(), [dict(task="score", calls=2, failed_calls=1, prompt_tokens=1300,
                                                  completion_tokens=850, items=32)])

    def test_the_scoring_request_is_counted(self):
        class Client:
            def __init__(self, **_):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response()))

        with patch.object(pipeline, "OpenAI", Client):
            pipeline._call_llm([{"id": 1}, {"id": 2}])
            pipeline._call_llm_titles([{"id": 3}])
        self.assertEqual([(row["task"], row["calls"], row["items"]) for row in self.usage_rows()],
                         [("score", 1, 2), ("title", 1, 1)])

    def test_recording_never_breaks_the_call_it_measures(self):
        with database.get_db() as db:
            db.execute("DROP TABLE ai_usage_daily")
        self.assertEqual(usage.call("score", lambda: "answer", items=1), "answer")

    def test_the_summary_counts_items_usage_and_growth_by_local_day(self):
        with database.get_db() as db:
            for key in ("nasdaq-cik1", "cls"):
                db.execute("INSERT INTO sources(key,name,channel,type) VALUES(?,?,'stock','rss')", (key, key))
            db.execute("""INSERT INTO companies(slug,name,market,ticker,origin,aliases)
                          VALUES('cik1','Chipworks','US','CHIP','coverage','["$CHIP"]')""")
        for n, (key, companies) in enumerate((("nasdaq-cik1", ["cik1"]), ("nasdaq-cik1", []), ("cls", ["cik1"]),
                                              ("cls", []))):
            runner.insert_item(key, dict(url=f"https://e.test/{n}", title=f"Story {n}",
                                         published_at=NOW.isoformat(), companies=companies))
        with database.get_db() as db:
            # 20:00Z on 10-15 is 04:00 on 10-16 in Shanghai.
            db.execute("UPDATE items SET fetched_at=?", ((NOW - timedelta(hours=7)).isoformat(),))
            db.execute("UPDATE items SET score=50, tmt=1 WHERE id IN (1,3)")
        with patch.object(usage, "datetime") as clock:
            clock.now.return_value = NOW.astimezone(config.APP_TZ)
            usage.record("score", response(), items=30)
        coverage_stats.record_size(NOW - timedelta(days=1))
        with database.get_db() as db:
            db.execute("UPDATE database_size_daily SET bytes=bytes-4096")
        coverage_stats.record_size(NOW)
        result = coverage_stats.summary(days=2, now=NOW)
        today = result["days"][-1]
        self.assertEqual([day["day"] for day in result["days"]], ["2026-10-15", "2026-10-16"])
        self.assertEqual({key: today[key] for key in ("items", "coverage_feed_items", "coverage_linked_items",
                                                       "scored", "kept")},
                         {"items": 4, "coverage_feed_items": 2, "coverage_linked_items": 2, "scored": 2, "kept": 2})
        self.assertEqual(today["ai"][0]["prompt_tokens"], 1200)
        self.assertEqual(today["database_growth_bytes"], 4096)
        self.assertIsNone(result["days"][0]["database_growth_bytes"])  # no reading the day before
        self.assertEqual(result["companies"], {"coverage": 1})

    def test_the_endpoint_serves_the_summary(self):
        body = TestClient(routes.app).get("/api/coverage").json()
        self.assertEqual(len(body["days"]), 14)
        self.assertEqual(body["tier"], config.COVERAGE_TIER)
        json.dumps(body)


if __name__ == "__main__":
    unittest.main()
