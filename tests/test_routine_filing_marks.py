import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.routine_filings import mark_routine_filings
from app.web import routes

T = "2026-10-06T09:00:00+00:00"


class LegacyRoutineFilingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (routes, "CURATED_FEED_ENABLED", True)):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        rows = [  # (source, title, extra)
            ("sec", "Acme · Form 4", {"form": "4"}),
            ("sec", "Acme · Form 144/A", {"form": "144/A"}),
            ("sec", "Acme · 8-K 业绩披露", {"form": "8-K"}),
            ("hkex", "腾讯控股 · 港交所公告：翌日披露報表 - [股份購回]", {"code": "0700"}),
            ("hkex", "腾讯控股 · 港交所公告：中期業績", {"code": "0700"}),
        ]
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'sec','SEC','stock','official','sec')")
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(2,'hkex','HKEX','stock','official','hkex')")
            for n, (source, title, extra) in enumerate(rows, start=1):
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,official,tmt,published_at,fetched_at,extra)
                              VALUES(?,?,?,?,'stock',1,1,?,?,?)""",
                           (n, 1 if source == "sec" else 2, f"https://e.test/{n}", title, T, T, json.dumps(extra)))

    def selected_titles(self):
        return sorted(row["title"] for row in routes._query_items(mode="selected"))

    def test_marks_only_routine_forms_and_returns_once(self):
        self.assertEqual(len(self.selected_titles()), 5)
        self.assertEqual(mark_routine_filings(apply=False),
                         {"applied": False, "sec_insider_forms": 2, "hkex_next_day_returns": 1})
        self.assertEqual(len(self.selected_titles()), 5)  # a dry run changes nothing
        mark_routine_filings(apply=True)
        self.assertEqual(self.selected_titles(), ["Acme · 8-K 业绩披露", "腾讯控股 · 港交所公告：中期業績"])
        self.assertEqual(mark_routine_filings(apply=True),
                         {"applied": True, "sec_insider_forms": 0, "hkex_next_day_returns": 0})
        with database.get_db() as db:
            extra = json.loads(db.execute("SELECT extra FROM items WHERE id=1").fetchone()[0])
        self.assertEqual(extra, {"form": "4", "routine": True})

    def test_only_the_maintenance_role_marks(self):
        import cli
        with patch.object(config, "PROCESS_ROLE", "web"), self.assertRaises(config.RuntimeConfigurationError):
            cli.cmd_routine_filings_mark(True)


if __name__ == "__main__":
    unittest.main()
