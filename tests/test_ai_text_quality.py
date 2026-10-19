import json
import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, mood, news_signals
from app.ai import daily, pipeline
from app.ai.text_quality import problem, usable
from app.crawler import runner
from app.provenance import display_title
from app.web.routes import app

# Fields the model wrote in production (10-19 scan of 121,957 items and the 10-18 report).
GARBLED = [
    "互联网房产平台股价异>异动，反映市场对其短期估值及业绩预期的分歧。",
    "油价日内大幅波动，直接冲击能源化工成本及通胀>通胀预期。",
    "Piper Sandler指出，受强劲AI需求推动，台*积电产能已售罄至2028年。",
    "海外极端事件对5对品牌形象有损#扰，但对核心F核心业务影响有限。",
    "亚马逊E马逊为加速AI人才布局，正重新聘用前员工。",
    "核心车型全新换代申报，影响新能源0新能源车企竞争与销量预期。",
    "地产股继续走强 �2连板",
    "分析机构给出高于华尔街平均水平的目​​标价。",
    "马斯克特斯拉在澳遭“1恐怖主义1行为针对",
    "得州对数据中心项目发函，或涉监管)A算力基建监管动向。",
    "需持续跟踪三星存储霸主# 地位受冲击程度及先进封装产能争夺1。",
    "OpenAI智能(体窃取加密货币事件\"暴露出目标泛化风险。",
    "[Anthropic招股书：与SpaceX签至2029年最高845亿美元算力?oc=5) — 天价算力锁定协议",
    "数据中心数据中心数据中心扩张",
]
CLEAN = [
    "英国即将任命杰森・斯托克伍德担任投资办公室主席",  # katakana middle dot in a foreign name
    "江波龙回购15.17万股A股用于股权激励",
    "中国石化回购520万股H股，斥资2330万元",
    "小鹏股价下跌3.3%，GF评分为6，分析探讨其是否被低估。",
    "官方确认我国创新能力排名全球第9，科技强国建设迈出坚实步伐。",
    "苹果（AAPL）发布《2026 年环境报告》，称“碳中和”目标提前完成。",
    "台积电利润大增70%，AI热潮延续至2026年",
    "Alphacool推出双G1/4\"口PCIe挡板",
    "哈基姆・杰弗里斯称，民主党将于周二开会",
]


class ProblemTests(unittest.TestCase):
    def test_garbled_model_text_is_caught(self):
        for text in GARBLED:
            with self.subTest(text=text):
                self.assertIsNotNone(problem(text))

    def test_correct_news_text_passes(self):
        for text in CLEAN:
            with self.subTest(text=text):
                self.assertIsNone(problem(text))
        self.assertTrue(usable(None) and usable(""))


class StoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        item = patch.object(database, "DB_PATH", Path(folder.name) / "app.db")
        item.start()
        self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','info')")
        for n in range(2):
            runner.insert_item("test", dict(url=f"https://e.test/{n}", title=f"Story {n}", summary=f"Feed text {n}",
                                            published_at="2026-10-15T08:00:00+00:00"))
        enabled = patch.object(config, "llm_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def row(self, item_id):
        with database.get_db() as db:
            return dict(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def garbled_count(self):
        with database.get_db() as db:
            return db.execute("SELECT COALESCE(SUM(calls),0) FROM ai_failures_daily WHERE kind='garbled'").fetchone()[0]

    def test_a_garbled_field_is_not_stored_and_the_title_gets_one_more_try(self):
        def call(payload):
            return [dict(id=entry["id"], tmt=True, score=60,
                         title_zh="天成>自控：海外子公司获定点" if entry["id"] == 1 else "第二条新闻",
                         summary_zh="美光即将>周三发布财报。" if entry["id"] == 1 else "正常摘要。",
                         reason="股价异>异动，反映分歧。" if entry["id"] == 1 else "正常理由。")
                    for entry in payload]

        with patch.object(pipeline, "_call_llm", side_effect=call):
            pipeline.process_pending(limit=10, parallel=1)
        first, second = self.row(1), self.row(2)
        self.assertEqual((first["score"], first["title_zh"], first["summary"], first["reason"]),
                         (60, "", "Feed text 0", ""))
        self.assertEqual((second["title_zh"], second["summary"], second["reason"]), ("第二条新闻", "正常摘要。", "正常理由。"))
        self.assertEqual(self.garbled_count(), 3)
        # backfill_titles asks again; a second garbled answer keeps the original title for good.
        with patch.object(pipeline, "_call_llm_titles", return_value=[dict(id=1, title_zh="天成>自控")]):
            pipeline.backfill_titles(days=3650)
        self.assertEqual(self.row(1)["title_zh"], "-")
        self.assertEqual(self.garbled_count(), 4)

    def test_text_stored_before_the_check_is_never_shown(self):
        with database.get_db() as db:
            db.execute("""UPDATE items SET score=70, tmt=1, title_zh='台*积电产能售罄', summary='委员>委员会见',
                          raw_summary='Original feed summary', reason='股价异>异动' WHERE id=1""")
        row = self.row(1)
        self.assertEqual(display_title(row), "Story 0")
        page = TestClient(app).get("/?mode=all").text
        self.assertIn("Story 0", page)
        self.assertIn("Original feed summary", page)
        for garbled in ("台*积电", "委员>委员", "股价异>异动"):
            self.assertNotIn(garbled, page)

    def test_the_news_api_hands_out_no_garbled_title(self):
        with database.get_db() as db:
            db.execute("UPDATE items SET score=70, tmt=1, title_zh='台*积电产能售罄' WHERE id=1")
            db.execute("UPDATE items SET score=70, tmt=1, title_zh='-' WHERE id=2")
            db.execute("UPDATE items SET companies='[\"chip\"]', fetched_at='2026-10-15T08:00:00+00:00'")
            company = db.execute("""INSERT INTO companies(slug,name,ticker,market,cik,aliases)
                                    VALUES('chip','Chip','CHIP','US','0000000001','[]')""").lastrowid
            db.executemany("INSERT OR IGNORE INTO item_companies(item_id,company_id) VALUES(?,?)",
                           [(1, company), (2, company)])
        with database.get_db() as db:
            result = news_signals.query_news(db, scope="company", ciks=["1"], themes=[],
                                             since="2026-10-01T00:00:00+00:00", as_of=None, min_score=None,
                                             limit=50, after=None, include_routine=True,
                                             now=datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertEqual(sorted((item["id"], item["title_zh"]) for item in result["data"]), [(1, None), (2, None)])

    def test_the_mood_card_shows_the_original_title(self):
        now = datetime.now(timezone.utc).isoformat()
        with database.get_db() as db:
            for item_id in (1, 2):
                db.execute("UPDATE items SET score=70, tmt=1, fetched_at=?, title_zh=? WHERE id=?",
                           (now, "台*积电产能售罄" if item_id == 1 else "-", item_id))
                db.execute("INSERT INTO item_moods(item_id,mood,judged_at) VALUES(?,?,?)",
                           (item_id, 2 if item_id == 1 else -2, now))
            result = mood.summary(db)
        titles = json.dumps(result, ensure_ascii=False)
        self.assertIn("Story 0", titles)
        self.assertIn("Story 1", titles)
        self.assertNotIn("台*积电", titles)
        self.assertNotIn('"-"', titles)


def _answer(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)


class ReportTests(unittest.TestCase):
    URL = "https://news.google.com/rss/articles/CBMiWEFVX3lxTE0xODUw?oc=5"
    DATA = dict(items=[dict(url=URL, time="12:52", channel="stock", title="台积电利润大增70%", summary="财报",
                            score=90, source_name="Reuters", event_type="earnings", official=0)],
                clusters=[])
    CLEAN = "台积电财报超预期。\n\n## 股市·科技企业\n- **12:52** [台积电利润大增70%](n1) — 需求强劲。"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        item = patch.object(database, "DB_PATH", Path(folder.name) / "app.db")
        item.start()
        self.addCleanup(item.stop)
        database.init_schema()

    def report(self, answers):
        temperatures = []

        class Client:
            def __init__(self, **_):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                temperatures.append(kwargs["temperature"])
                self.prompt = kwargs["messages"][0]["content"]
                return _answer(answers[len(temperatures) - 1])

        with patch("openai.OpenAI", Client):
            text = daily._llm_report("2026-10-18", self.DATA)
        return text, temperatures

    def test_the_model_cites_refs_and_never_sees_a_url(self):
        captured = {}
        original = daily.json.dumps

        def spy(value, **kwargs):
            captured["material"] = value
            return original(value, **kwargs)

        with patch.object(daily.json, "dumps", side_effect=spy):
            text, temperatures = self.report([self.CLEAN])
        self.assertEqual(temperatures, [0.4])
        self.assertIn(f"[台积电利润大增70%]({self.URL})", text)
        self.assertNotIn("news.google.com", json.dumps(captured["material"], ensure_ascii=False))
        self.assertEqual(captured["material"]["items"][0]["ref"], "n1")

    def test_a_garbled_report_is_written_again(self):
        garbled = self.CLEAN + "\n\n## 值得关注\n1. 需持续跟踪三星存储霸主# 地位受冲击程度。"
        text, temperatures = self.report([garbled, self.CLEAN])
        self.assertEqual((text, temperatures), (self.CLEAN.replace("(n1)", f"({self.URL})"), [0.4, 0.2]))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT calls FROM ai_failures_daily WHERE kind='garbled'").fetchone()[0], 1)

    def test_a_link_the_model_typed_itself_counts_as_a_bad_line(self):
        typed = self.CLEAN.replace("(n1)", "(https://news.google.com/rss/articles/CBMiWEFVX3lx?oc=5)")
        text, temperatures = self.report([typed, self.CLEAN])
        self.assertEqual(len(temperatures), 2)
        self.assertIn(self.URL, text)

    def test_after_three_bad_tries_only_the_bad_lines_go(self):
        garbled = self.CLEAN + "\n- **13:00** [台积电利润大增70%](n1) — 台*积电产能售罄。"
        text, temperatures = self.report([garbled] * 3)
        self.assertEqual(temperatures, [0.4, 0.2, 0.1])
        self.assertIn("需求强劲", text)
        self.assertNotIn("台*积电", text)

    def test_a_stored_report_is_shown_without_its_garbled_lines(self):
        stored = ("台积电财报超预期。\n\n## 值得关注\n"
                  "1. HBM：需持续跟踪三星存储霸主# 地位受冲击程度。\n2. 算力：关注订单变化。")
        html = daily.render_report(stored)
        self.assertNotIn("霸主#", html)
        self.assertIn("关注订单变化", html)
        self.assertIn("台积电财报超预期", html)


if __name__ == "__main__":
    unittest.main()
