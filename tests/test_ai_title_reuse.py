import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import company_match, config, database
from app.ai import pipeline
from app.crawler import runner

TITLE = "OpenAI launches coding agent for enterprises"


class TitleReuseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "BLOB_PATH", Path(folder.name) / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            for key, channel in (("feed", "ai"), ("wire", "ai"), ("stocks", "stock")):
                db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES(?,?,?,'rss','info')",
                           (key, key, channel))
        self.number = 0
        pending = patch.object(pipeline.config, "llm_enabled", return_value=True)
        pending.start()
        self.addCleanup(pending.stop)

    def item(self, title=TITLE, source="wire", hour=12, **fields):
        self.number += 1
        raw = dict(url=f"https://example.com/{self.number}", title=title, companies=["openai"],
                   published_at=f"2026-09-1{hour // 24 + 1}T{hour % 24:02d}:00:00+00:00")
        raw.update(fields)
        self.assertTrue(runner.insert_item(source, raw))
        with database.get_db() as db:
            return db.execute("SELECT max(id) FROM items").fetchone()[0]

    def judged(self, **fields):
        item_id = self.item(source="feed", hour=10, **fields)
        with database.get_db() as db:
            db.execute("""UPDATE items SET score=77, tmt=1, title_zh='OpenAI 推出企业编程智能体',
                          summary='摘要', reason='理由', ai_cat='product' WHERE id=?""", (item_id,))
        return item_id

    def row(self, item_id):
        with database.get_db() as db:
            return dict(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def test_a_repeat_takes_the_earlier_judgement_without_a_model_call(self):
        earlier = self.judged()
        repeat = self.item()
        with patch.object(pipeline, "_call_llm", side_effect=AssertionError("model called")):
            self.assertEqual(pipeline.process_pending(), 1)
        row = self.row(repeat)
        self.assertEqual((row["score"], row["tmt"], row["title_zh"], row["summary"], row["reason"], row["ai_cat"]),
                         (77, 1, "OpenAI 推出企业编程智能体", "摘要", "理由", "product"))
        self.assertEqual(json.loads(row["extra"])["ai_reused_from"], earlier)

    def test_only_the_same_article_is_reused(self):
        self.judged()
        cases = {
            "another channel": self.item(source="stocks"),
            "other companies": self.item(companies=["anthropic"]),
            "ten hours later": self.item(hour=20),
            "official": self.item(official=1),
            "short title": self.item(title="OpenAI news"),
            "another title": self.item(title=TITLE + " today"),
        }
        with patch.object(pipeline, "_call_llm", return_value=[]) as call:
            self.assertEqual(pipeline.process_pending(limit=20), 0)
        self.assertEqual(sorted(row["id"] for row in call.call_args.args[0]), sorted(cases.values()))

    def test_a_daily_stock_move_title_is_judged_again(self):
        # The same template two hours apart can already be another day's move in another market.
        title = "Why Is Arm Holdings Stock Sliding Today?"
        self.judged(title=title)
        repeat = self.item(title=title)
        with patch.object(pipeline, "_call_llm", return_value=[]) as call:
            pipeline.process_pending()
        self.assertEqual([row["id"] for row in call.call_args.args[0]], [repeat])

    def test_a_short_title_is_judged_again(self):
        self.judged(title="OpenAI news")  # too generic to say it is the same article
        repeat = self.item(title="OpenAI news")
        with patch.object(pipeline, "_call_llm", return_value=[]) as call:
            pipeline.process_pending()
        self.assertEqual([row["id"] for row in call.call_args.args[0]], [repeat])

    def test_the_count_includes_reused_and_judged_items(self):
        self.judged()
        self.item()
        fresh = self.item(title="Anthropic opens an office in Tokyo for enterprise customers")
        verdict = dict(id=fresh, score=60, tmt=True, summary_zh="s", reason="r", title_zh="t")
        with patch.object(pipeline, "_call_llm", return_value=[verdict]):
            self.assertEqual(pipeline.process_pending(), 2)

    def test_a_filtered_or_unjudged_earlier_item_is_not_reused(self):
        filtered = self.judged()
        with database.get_db() as db:
            db.execute("UPDATE items SET score=-1 WHERE id=?", (filtered,))
        repeat = self.item()
        with patch.object(pipeline, "_call_llm", return_value=[]) as call:
            pipeline.process_pending()
        self.assertIn(repeat, [row["id"] for row in call.call_args.args[0]])

    def test_reused_items_count_even_when_the_model_fails(self):
        self.judged()
        repeat = self.item()
        fresh = self.item(title="Anthropic opens an office in Tokyo for enterprise customers")
        with patch.object(pipeline, "_call_llm", side_effect=TimeoutError()) as call:
            self.assertEqual(pipeline.process_pending(), 1)
        self.assertEqual([row["id"] for row in call.call_args.args[0]], [fresh])
        self.assertEqual(self.row(repeat)["score"], 77)
        self.assertIsNone(self.row(fresh)["score"])


if __name__ == "__main__":
    unittest.main()
