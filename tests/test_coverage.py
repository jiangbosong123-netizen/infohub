import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import company_match, config, coverage, database, security_master, worker
from app.crawler import rss_source, sources

SETTINGS = {
    "tiers": {
        1: {"name": "chips", "min_market_cap": 1e9, "industries": ["Semiconductors"],
            "technology_industries": ["Electrical Products"]},
        2: {"name": "top 2", "top_market_cap": 2},
    },
    "requests": {"research": {"ciks": [7, 12, 99]}},
}


def listing(ticker, cik, cap, industry="Semiconductors", sector="Technology", name=None, exchange="NASDAQ"):
    return dict(ticker=ticker, cik=cik, name=f"{ticker} CORP", display_name=name or f"{ticker.title()}ics Inc.",
                exchange=exchange, market_cap=cap, sector=sector, industry=industry, country="United States")


LISTINGS = [
    listing("CHIP", 1, 5e9, name="Chipworks Technology Inc."),
    listing("TINY", 2, 4e8),                                             # below the tier-1 floor
    listing("PWRX", 3, 2e9, industry="Electrical Products"),             # technology sector: in
    listing("UTIL", 4, 3e9, industry="Electrical Products", sector="Utilities"),  # other sector: out
    listing("MEGA", 5, 9e11, industry="Computer Software"),
    listing("MEGB", 5, 8e11, industry="Computer Software"),             # second class of the same company
    listing("BIGG", 6, 7e11, industry="Retail"),
    listing("SPAC", 8, 9e12, industry="Blank Checks"),                   # never covered
    listing("REQS", 7, 2e8, industry="Auto Parts"),                      # only by request
    listing("OTCX", 9, 9e9, exchange="OTC"),                             # not listed on NYSE/Nasdaq
    listing("NVDA", 10, 4e12, name="NVIDIA Corporation"),                # a watchlist company
    listing("SUPR", 11, 2e10, name="Supermicron Inc."),
    listing("SUPRP", 11, 4e10, name="Supermicron Inc."),                 # its preferred, quoted larger
    listing("MINE", 13, 3e9, name="Bitminer Technologies Inc."),
    listing("MINP", 13, 9e9, name="Bitminer Technologies Inc. 9.5% Series A Perpetual Preferred Stock"),
    listing("PREF", 14, 2e9, name="Preferred Chips Inc."),                # "Preferred" in its own name
    listing("GOOG", 12, 1e11, industry="Computer Software", name="Alphabet Inc."),
    listing("GOOGL", 12, 1e11, industry="Computer Software", name="Alphabet Inc."),  # watchlist holds GOOGL
]


class CoverageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "DB_PATH", Path(folder.name) / "app.db")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        settings = patch.object(coverage, "load", return_value=SETTINGS)
        settings.start()
        self.addCleanup(settings.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:  # a watchlist company before its first SEC crawl: no CIK yet
            db.execute("""INSERT INTO companies(slug,name,name_zh,ticker,market,aliases)
                          VALUES('nvidia','NVIDIA','英伟达','NVDA','US','["NVIDIA","NVDA"]')""")
            db.execute("""INSERT INTO companies(slug,name,name_zh,ticker,market,aliases)
                          VALUES('alphabet','Alphabet','谷歌','GOOGL','US','["Alphabet"]')""")
        with patch.object(security_master, "MIN_LISTINGS", 1):
            security_master.store_listings(LISTINGS)

    def plan(self, level):
        with database.get_db() as db:
            return {company["ticker"]: company["reasons"] for company in coverage.plan(db, level).values()}

    def companies(self):
        with database.get_db() as db:
            return {row["ticker"]: dict(row) for row in db.execute("SELECT * FROM companies")}

    def test_tiers_follow_market_rules_and_requests_only_add_companies(self):
        self.assertEqual(self.plan(0), {})
        # SUPRP is the preferred line of SUPR; GOOGL extends GOOG, both are Alphabet.
        self.assertEqual(self.plan(1), {"CHIP": ["tier 1"], "PWRX": ["tier 1"], "NVDA": ["tier 1"],
                                        "SUPR": ["tier 1"], "MINE": ["tier 1"], "PREF": ["tier 1"],
                                        "REQS": ["request research"], "GOOG": ["request research"]})
        two = self.plan(2)
        self.assertEqual(two["MEGA"], ["tier 2"])  # one company, its largest share class
        self.assertNotIn("MEGB", two)
        self.assertEqual(two["NVDA"], ["tier 1", "tier 2"])
        self.assertNotIn("SPAC", two)
        with database.get_db() as db:
            self.assertEqual(coverage.unresolved_requests(db), {"research": [99]})

    def test_sync_adds_coverage_companies_keeps_the_watchlist_and_retires_quietly(self):
        self.assertEqual(coverage.sync(1), {"tier": 1, "planned": 8, "added": 6, "updated": 0, "retired": 0})
        rows = self.companies()
        self.assertEqual(rows["NVDA"]["origin"], "watchlist")  # matched by ticker, not duplicated
        self.assertEqual(rows["GOOGL"]["origin"], "watchlist")  # matched by the company's other ticker
        self.assertNotIn("GOOG", rows)
        self.assertEqual(json.loads(rows["SUPR"]["aliases"])[0], "$SUPR")
        self.assertNotIn("SUPRP", rows)
        self.assertEqual((rows["CHIP"]["slug"], rows["CHIP"]["origin"], rows["CHIP"]["cik"]),
                         ("cik1", "coverage", "0000000001"))
        self.assertEqual(json.loads(rows["CHIP"]["aliases"]),
                         ["$CHIP", "Chipworks Technology", "=Chipworks", "Chipworks Technology Inc"])
        keys = {source["key"] for source in sources.all_sources()}
        self.assertIn("nasdaq-cik1", keys)
        self.assertIn("googlenews-nvidia", keys)
        self.assertNotIn("googlenews-cik1", keys)  # no per-company Google search for coverage companies
        self.assertEqual(coverage.sync(0)["retired"], 6)
        self.assertEqual(self.companies()["CHIP"]["covered"], 0)
        self.assertNotIn("nasdaq-cik1", {source["key"] for source in sources.all_sources()})

    def test_aliases_are_strict_everywhere_and_looser_in_the_companys_own_feed(self):
        self.assertEqual(coverage.aliases("Intel Corporation", "INTC"), ["$INTC", "=Intel", "Intel Corporation"])
        self.assertEqual(coverage.aliases("Target Corporation", "TGT"), ["$TGT", "Target Corporation"])
        self.assertEqual(coverage.aliases("KLA Corporation", "KLAC"), ["$KLAC", "KLA Corporation"])
        self.assertEqual(coverage.aliases("Applied Materials Inc.", "AMAT"),
                         ["$AMAT", "Applied Materials", "Applied Materials Inc"])
        coverage.sync(1)
        self.assertEqual(company_match.match_companies("Chipworks beats; $PWRX slides"), ["cik1", "cik3"])
        self.assertEqual(company_match.match_companies("chipworks of the world"), [])  # one word: as written
        self.assertTrue(company_match.mentions_in_own_feed("cik3", "PWRX guidance raised"))
        self.assertFalse(company_match.mentions_in_own_feed("cik3", "Stocks fall on rates"))

    def test_a_coverage_feed_keeps_only_entries_about_its_company(self):
        coverage.sync(1)
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>CHIP</title>
            <item><title>CHIP shares jump after earnings</title><link>https://example.com/a</link></item>
            <item><title>Stocks slide on inflation</title><link>https://example.com/b</link></item>
            </channel></rss>"""

        class Response:
            status_code, content, headers = 200, feed, {}

        with patch.object(rss_source.http, "fetch", return_value=Response()):
            entries = rss_source.fetch_rss({"url": "https://example.com/feed", "company_slug": "cik1"})
        self.assertEqual([(entry["title"], entry["companies"]) for entry in entries],
                         [("CHIP shares jump after earnings", ["cik1"])])

    def test_the_worker_schedules_coverage_only_when_a_tier_is_set(self):
        def schedule():
            with database.get_db() as db:
                return db.execute("SELECT enabled,payload_json FROM schedules WHERE id='coverage:daily'").fetchone()
        worker.register_default_schedules()
        self.assertIsNone(schedule())
        with patch.object(config, "COVERAGE_TIER", 1):
            worker.register_default_schedules()
        self.assertEqual((schedule()["enabled"], json.loads(schedule()["payload_json"])), (1, {"tier": 1}))
        worker.register_default_schedules()
        self.assertEqual(schedule()["enabled"], 0)

    def test_the_security_master_refuses_a_suspiciously_short_list(self):
        with self.assertRaisesRegex(RuntimeError, "refusing"):
            security_master.store_listings(LISTINGS)
        self.assertEqual(security_master.display_name("Teradyne Inc. Common Stock"), "Teradyne Inc.")
        self.assertEqual(security_master.display_name("Alphabet Inc. Class A Common Stock"), "Alphabet Inc.")
        for raw, name in (("ASML Holding N.V. New York Registry Shares", "ASML Holding N.V."),
                          ("Arcelor Mittal NY Registry Shares NEW", "Arcelor Mittal"),
                          ("LG Display Co Ltd AMERICAN DEPOSITORY SHARES", "LG Display Co Ltd"),
                          ("Johnson Controls International plc Ordinary Share", "Johnson Controls International plc"),
                          ("Brookfield Corporation Class A Limited Voting Shares", "Brookfield Corporation"),
                          ("United Microelectronics Corporation (NEW) Common Stock", "United Microelectronics Corporation"),
                          ("Iron Mountain Incorporated (Delaware)Common Stock REIT", "Iron Mountain Incorporated"),
                          ("Alphabet Inc. Class C Capital Stock", "Alphabet Inc."),
                          ("Newmont Corporation", "Newmont Corporation")):
            with self.subTest(raw=raw):
                self.assertEqual(security_master.display_name(raw), name)
        self.assertEqual(coverage.aliases("ASML Holding N.V.", "ASML"), ["$ASML", "=ASML", "ASML Holding N.V"])
        self.assertEqual(security_master._ticker("BRK/B"), "BRK-B")


if __name__ == "__main__":
    unittest.main()
