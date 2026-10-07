import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config, database
from app.crawler import hkex_source
from app.web import routes

T = "2026-10-06T09:00:00+00:00"


class RoutineFilingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for target, name, value in ((database, "DB_PATH", path), (config, "DB_PATH", path),
                                    (routes, "CURATED_FEED_ENABLED", True)):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        items = {
            # (official, score, extra)
            "results": (1, None, {"hkex_category": "公告及通告"}),
            "daily return": (1, None, {"hkex_category": "翌日披露報表", "routine": True}),
            "daily return the AI rated highly": (1, 75, {"routine": True}),
            "official without valid extra": (1, None, "not json"),
            "plain report": (0, 40, {}),
        }
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'hkex','HKEX','stock','official','hkex')")
            for n, (title, (official, score, extra)) in enumerate(items.items(), start=1):
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,official,
                              published_at,fetched_at,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (n, 1, f"https://e.test/{n}", title, "stock", score, 1, official, T, T,
                            extra if isinstance(extra, str) else json.dumps(extra, ensure_ascii=False)))

    def titles(self, mode):
        return sorted(row["title"] for row in routes._query_items(mode=mode))

    def test_routine_official_filings_leave_the_selected_feed_only(self):
        self.assertEqual(self.titles("selected"), [
            "daily return the AI rated highly", "official without valid extra", "results"])
        self.assertEqual(len(self.titles("all")), 5)

    def test_next_day_disclosure_returns_are_marked_routine(self):
        records = [
            {"TITLE": "翌日披露報表", "LONG_TEXT": "翌日披露報表 - [股份購回]", "FILE_LINK": "/a.pdf",
             "DATE_TIME": "06/10/2026 17:30"},
            {"TITLE": "翌日披露報表 - 已發行股份變動", "LONG_TEXT": "翌日披露報表 - [其他]", "FILE_LINK": "/b.pdf",
             "DATE_TIME": "06/10/2026 17:40"},
            {"TITLE": "自動股份購回計劃公告", "LONG_TEXT": "公告及通告 - [其他-雜項]", "FILE_LINK": "/c.pdf",
             "DATE_TIME": "06/10/2026 18:00"},
        ]
        company = {"slug": "xiaomi", "name": "Xiaomi", "name_zh": "小米集团", "code": "1810",
                   "hkex_stock_id": "1"}
        response = SimpleNamespace(text=json.dumps({"result": records}, ensure_ascii=False))
        with patch.object(hkex_source.http, "fetch", return_value=response):
            rows = hkex_source._fetch_company(company, "20261006", "20261006")
        self.assertEqual([row["extra"].get("routine") for row in rows], [True, True, None])


if __name__ == "__main__":
    unittest.main()
