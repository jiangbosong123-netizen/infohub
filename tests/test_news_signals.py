import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, db_admin
from app.api_auth import create_consumer, issue_api_key
from app.web.routes import app

T = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)  # the window ends at now by default
MU, NVDA = "0000723125", "0001045810"


class NewsSignalsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "app.db"
        db_admin.migrate_database(self.path)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            self.key = issue_api_key(db, create_consumer(db, "ai-research", actor="test"), {"read:signals"},
                                     expires_at=now + timedelta(days=1), actor="test").token
            self.wrong = issue_api_key(db, create_consumer(db, "other", actor="test"), {"read:items"},
                                       expires_at=now + timedelta(days=1), actor="test").token
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type) VALUES
                          (1,'wire','Wire','stock','media','rss'),(2,'sec-edgar','SEC EDGAR','stock','official','sec')""")
            db.execute("""INSERT INTO companies(id,slug,name,market,ticker,cik,aliases) VALUES
                          (1,'micron','Micron','US','MU',?,'[]'),(2,'nvidia','NVIDIA','US','NVDA',?,'[]')""", (MU, NVDA))
            self.ids = {}
            for key, minutes, source, title, companies, score, mood, extra, tmt in (
                    ("cut", 0, 1, "Micron cuts guidance as memory prices fall", [1], 80, -2, {}, 1),
                    ("repost", 5, 1, "Micron cuts guidance, shares slide", [1], 75, -2, {}, 1),
                    ("ban", 10, 1, "Nvidia faces new export controls on H20 chips", [2], 90, -1, {}, 1),
                    ("form4", 15, 2, "Micron Technology · SEC 内部人持股变动", [1], 25, None, {"routine": True}, 1),
                    ("fed", 20, 1, "美联储宣布降息25个基点", [], 85, 1, {}, 1),
                    ("cowos", 25, 1, "台积电CoWoS产能翻倍", [], 60, 1, {}, 1),
                    ("sofa", 30, 1, "Sofa sales rise", [], 90, 1, {}, 1),
                    ("hidden", 35, 1, "Micron the board game", [1], 10, 0, {}, 0)):
                fetched = (T + timedelta(minutes=minutes)).isoformat()
                item_id = db.execute(
                    """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at,companies,score,tmt,
                                         official,extra) VALUES(?,?,?,'stock',?,?,?,?,?,?,?)""",
                    (source, f"https://e.test/{key}", title, fetched, fetched,
                     json.dumps(["micron"] if 1 in companies else ["nvidia"] if companies else []),
                     score, tmt, int(source == 2), json.dumps(extra))).lastrowid
                self.ids[key] = item_id
                for company in companies:
                    db.execute("INSERT OR IGNORE INTO item_companies(item_id,company_id) VALUES(?,?)", (item_id, company))
                if mood is not None:
                    db.execute("INSERT INTO item_moods(item_id,mood,judged_at) VALUES(?,?,?)",
                               (item_id, mood, (T + timedelta(minutes=minutes + 3)).isoformat()))
            db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,first_at,last_at,item_count)
                          VALUES('s1',?,'Micron cuts','stock','https://e.test/cut',?,?,2)""",
                       (self.ids["cut"], T.isoformat(), T.isoformat()))
            db.execute("DELETE FROM story_items WHERE item_id IN (?,?)", (self.ids["cut"], self.ids["repost"]))
            db.executemany("INSERT INTO story_items(item_id,story_id) VALUES(?,'s1')",
                           [(self.ids["cut"],), (self.ids["repost"],)])
        for mocked in (patch("app.web.routes.get_db", lambda: database.get_db(self.path)),
                       patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path)),
                       patch.object(database, "DB_PATH", self.path), patch.object(config, "DB_PATH", self.path),
                       patch.object(config, "API_SIGNALS_ENABLED", True)):
            mocked.start()
            self.addCleanup(mocked.stop)
        from app import company_match  # headline_ciks reads the company aliases
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        self.client = TestClient(app)

    def get(self, token=None, **params):
        return self.client.get("/api/v1/signals/news", params={"since": (T - timedelta(hours=1)).isoformat(), **params},
                               headers={"Authorization": f"Bearer {token or self.key}"})

    def titles(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return [item["title"] for item in response.json()["data"]]

    def test_access_needs_the_flag_and_a_signals_key(self):
        self.assertEqual(self.get(self.wrong, cik=MU).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/signals/news", params={"cik": MU, "since": T.isoformat()}).status_code, 401)
        with patch.object(config, "API_SIGNALS_ENABLED", False):
            self.assertEqual(self.get(cik=MU).status_code, 503)

    def test_company_news_with_independence_and_without_routine_or_hidden_items(self):
        response = self.get(cik=["723125", "999"])
        body = response.json()
        self.assertEqual(self.titles(response), ["Micron cuts guidance as memory prices fall",
                                                 "Micron cuts guidance, shares slide"])
        first, repost = body["data"]
        self.assertEqual((first["independence"], repost["independence"]), ("report", "repost"))
        self.assertEqual((first["ciks"], first["importance"], first["mood"], first["themes"]), ([MU], 80, -2, ["memory"]))
        self.assertEqual(body["unknown_ciks"], ["0000000999"])
        self.assertIn("Micron Technology · SEC 内部人持股变动", self.titles(self.get(cik=MU, include_routine="true")))

    def test_the_headline_says_which_companies_the_story_is_about(self):
        from app import company_match
        with database.get_db(self.path) as db:
            db.execute("UPDATE companies SET aliases='[\"Micron\"]' WHERE id=1")
            db.execute("UPDATE companies SET aliases='[\"Nvidia\"]' WHERE id=2")
            item_id = db.execute(
                """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at,companies,score,tmt)
                   VALUES(1,'https://e.test/both','Micron raises prices for AI buyers','stock',?,?,
                          '["micron","nvidia"]',60,1)""",
                ((T + timedelta(minutes=45)).isoformat(), (T + timedelta(minutes=45)).isoformat())).lastrowid
            db.executemany("INSERT INTO item_companies(item_id,company_id) VALUES(?,?)", [(item_id, 1), (item_id, 2)])
        company_match.invalidate_cache()
        data = {item["id"]: item for item in self.get(cik=[MU, NVDA]).json()["data"]}
        self.assertEqual((data[item_id]["ciks"], data[item_id]["headline_ciks"]), ([MU, NVDA], [MU]))

    def test_each_cik_says_since_when_it_is_collected(self):
        with database.get_db(self.path) as db:
            db.execute("""INSERT INTO company_coverage_periods(company_id,started_at,basis)
                          VALUES(1,'2026-10-15T15:59:42+00:00','first_feed_fetch')""")
            db.execute("""INSERT INTO collection_gaps(started_at,ended_at,reason)
                          VALUES('2026-10-03T16:02:18Z','2026-10-14T22:04:42Z','停机')""")
        body = self.get(cik=[MU, NVDA]).json()
        self.assertEqual(body["coverage"], {MU: {"covered_since": "2026-10-15T15:59:42+00:00", "basis": "first_feed_fetch"}})
        self.assertEqual(body["collection_gaps"], [{"from": "2026-10-03T16:02:18Z", "to": "2026-10-14T22:04:42Z",
                                                    "reason": "停机"}])

    def test_a_refused_item_has_no_judgement(self):
        with database.get_db(self.path) as db:  # the content filter refused it: score -1
            item_id = db.execute(
                """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at,companies,score,tmt)
                   VALUES(1,'https://e.test/refused','Micron and the sensitive story','stock',?,?,'["micron"]',-1,1)""",
                ((T + timedelta(minutes=40)).isoformat(), (T + timedelta(minutes=40)).isoformat())).lastrowid
            db.execute("INSERT INTO item_companies(item_id,company_id) VALUES(?,1)", (item_id,))
        refused = [item for item in self.get(cik=MU).json()["data"] if item["id"] == item_id]
        self.assertEqual([(item["importance"], item["mood"], item["refused"]) for item in refused], [(None, None, True)])
        self.assertNotIn("Micron and the sensitive story", self.titles(self.get(cik=MU, min_score=1)))

    def test_chip_smuggling_counts_as_export_controls(self):
        from app import market_themes
        self.assertEqual(market_themes.classify("台湾拟将走私英伟达芯片至中国定为犯罪"), ["export_controls"])
        self.assertEqual(market_themes.classify("Smuggling Nvidia Chips Into China Is About to Become a Crime"),
                         ["export_controls"])
        self.assertEqual(market_themes.classify("海关查获走私香烟"), [])  # no chip word: not about chips

    def test_industry_and_macro_news_come_by_theme_above_their_floor(self):
        self.assertEqual(self.titles(self.get(scope="macro")), ["美联储宣布降息25个基点"])
        industry = self.get(scope="industry", theme="capacity_supply").json()
        self.assertEqual([(item["title"], item["themes"]) for item in industry["data"]],
                         [("台积电CoWoS产能翻倍", ["capacity_supply"])])
        self.assertEqual(self.titles(self.get(scope="industry", theme="export_controls")),
                         ["Nvidia faces new export controls on H20 chips"])
        self.assertEqual(self.titles(self.get(scope="industry", theme="capacity_supply", min_score=70)), [])

    def test_as_of_hides_what_was_not_known_yet(self):
        # 1 minute after the first item: it was seen, but its judgement came 3 minutes after it.
        response = self.get(cik=MU, as_of=(T + timedelta(minutes=1)).isoformat()).json()
        self.assertEqual([(item["title"], item["importance"], item["mood"]) for item in response["data"]],
                         [("Micron cuts guidance as memory prices fall", None, None)])
        self.assertTrue(response["point_in_time"])
        self.assertEqual(self.get(cik=MU, as_of=(T + timedelta(minutes=1)).isoformat(), min_score=10).json()["data"], [])
        later = self.get(cik=MU, as_of=(T + timedelta(minutes=4)).isoformat()).json()["data"]
        self.assertEqual((later[0]["importance"], later[0]["mood"]), (80, -2))

    def test_pages_continue_after_the_last_item(self):
        first = self.get(cik=[MU, NVDA], limit=2).json()
        self.assertEqual(len(first["data"]), 2)
        rest = self.get(cik=[MU, NVDA], limit=2, after=first["next_after"]).json()
        self.assertEqual([item["title"] for item in rest["data"]], ["Nvidia faces new export controls on H20 chips"])
        self.assertIsNone(rest["next_after"])

    def test_a_new_signals_key_replaces_the_previous_one(self):
        from app.news_signals import issue_signals_key
        with database.get_db(self.path) as db:
            first = issue_signals_key(db, "ai-research-2", actor="test")
            second = issue_signals_key(db, "ai-research-2", actor="test")
        self.assertEqual(self.get(first, cik=MU).status_code, 401)
        self.assertEqual(self.get(second, cik=MU).status_code, 200)

    def test_bad_requests_are_refused(self):
        for params in ({"cik": MU, "theme": "memory"}, {"scope": "industry", "theme": "rates"},
                       {"cik": "abc"}, {"cik": MU, "since": "yesterday"}, {"cik": MU, "limit": "0"},
                       {"scope": "macro", "since": (T - timedelta(days=8)).isoformat()},
                       {"cik": MU, "colour": "red"}, {"scope": "weather"}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 422)


if __name__ == "__main__":
    unittest.main()
