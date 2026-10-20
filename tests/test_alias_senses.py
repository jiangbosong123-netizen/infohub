import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import company_match, database, db_admin, topics
from app.alias_senses import company_sense

# Headlines from the 10-19 backup.
OTHER_SENSES = [
    ("AMD", "EyePoint crashes as pivotal wet AMD trial fails, prompting 53% workforce cut"),
    ("AMD", "Magnolia Eye Group offers Valeda treatment option for select dry AMD patients"),
    ("AMD", "Retina Society 2026: Subretinal deposits in AMD may flag severe aortic stenosis"),
    ("Apple", "The apple pie (inspired by mom, of course) became the symbolic food of a nation"),
    ("Apple", "Seymour holds 51st Apple Festival as longtime attendees pass down its legacy"),
    ("Apple", "UAV-Based Canopy Mapping Reveals Hidden Nitrogen Layers in Apple Orchards"),
    ("Apple", "Fox News 'Antisemitism Exposed' Newsletter: Bibi in the Big Apple"),
    ("Apple", "Fried Apple Pies.png"),
    ("Apple", "Three Apple Festivals worth the drive this weekend"),
    ("苹果", "苹果期货主力合约大涨，红富士收购价走高"),
    ("Meta", "Network meta-analysis ranks neoadjuvant options in cisplatin-eligible MIBC"),
    ("Meta", "Video Games That Get Kids Moving Work as Well as Traditional Sports, Landmark Meta-analysis Finds"),
]
COMPANY_SENSES = [
    ("AMD", "AMD stock hits all-time high as MI450 orders grow"),
    ("AMD", "Amd flies and will keep holding"),
    ("Apple", "Apple unveils the iPhone 18 Pro at its Cupertino event"),
    ("Apple", "Apple TV+ will stream its classic Peanuts holiday specials for free, including the Great Pumpkin"),
    ("Apple", "APPLE INC. SHAREHOLDER ALERT"),
    ("Apple", "Apples-to-apples, Apple's margins beat Samsung's"),
    ("苹果", "苹果发布会定档10月28日，或推出触屏MacBook"),
    ("Meta", "Meta's AI agent Muse is now the No. 2 app in the US"),
    ("META", "Key deals this week: META, ADI, BSP, AAPL, and more"),
]


class CompanySenseTests(unittest.TestCase):
    def test_an_alias_naming_something_else_does_not_count(self):
        for alias, text in OTHER_SENSES:
            with self.subTest(text=text):
                self.assertFalse(company_sense(alias, text))

    def test_an_alias_naming_the_company_counts(self):
        for alias, text in COMPANY_SENSES:
            with self.subTest(text=text):
                self.assertTrue(company_sense(alias, text))
        self.assertTrue(company_sense("NVDA", "anything at all"))  # only listed aliases are checked


class MatchingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "app.db"
        item = patch.object(database, "DB_PATH", self.path)
        item.start()
        self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("DELETE FROM companies")
            for slug, name, aliases in (("amd", "AMD", ["AMD", "超威", "苏姿丰", "Lisa Su"]),
                                        ("apple", "Apple", ["Apple", "AAPL", "苹果", "库克", "Tim Cook"]),
                                        ("meta", "Meta", ["Meta", "Facebook", "META", "Zuckerberg"])):
                db.execute("""INSERT INTO companies(slug,name,market,aliases,origin) VALUES(?,?,'US',?,'watchlist')""",
                           (slug, name, json.dumps(aliases, ensure_ascii=False)))
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)

    def test_company_links_and_topics_skip_the_other_sense(self):
        self.assertEqual(company_match.match_companies("EyePoint will halve workforce after wet AMD trial fails"), [])
        self.assertEqual(company_match.match_companies("AMD shares rise after Lisa Su keynote"), ["amd"])
        self.assertEqual(company_match.match_companies("The best orchards for apple picking"), [])
        self.assertEqual(company_match.match_companies("Apple unveils the iPhone 18"), ["apple"])
        self.assertEqual(company_match.match_companies("Bibi in the Big Apple, says Tim Cook"), ["apple"])  # another alias
        self.assertEqual(company_match.match_companies("Network meta-analysis ranks options"), [])
        with database.get_db() as db:
            definitions = topics.sync_topics(db)
            for item_id, title in ((1, "The best orchards for apple picking"), (2, "Apple unveils the iPhone 18")):
                db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                           (1, "wire", "Wire", "stock", "rss"))
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at,tmt)
                              VALUES(?,1,?,?,'stock','2026-10-19T00:00:00Z','2026-10-19T00:00:00Z',1)""",
                           (item_id, f"https://e.test/{item_id}", title))
                topics.assign_topics(db, dict(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()),
                                     definitions)
            assigned = {row[0]: row[1] for row in db.execute("SELECT item_id, group_concat(topic_slug) FROM item_topics GROUP BY item_id")}
        self.assertNotIn("apple", (assigned.get(1) or "").split(","))
        self.assertIn("apple", assigned[2].split(","))


class MigrationTests(unittest.TestCase):
    def test_migration_63_unlinks_only_the_other_sense(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "app.db"
        with database.get_db(path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:62])
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'wire','Wire','stock','rss')")
            db.execute("""INSERT INTO companies(id,slug,name,market,aliases) VALUES
                          (1,'amd','AMD','US','["AMD","超威","Lisa Su"]'),
                          (2,'apple','Apple','US','["Apple","AAPL","苹果"]')""")
            rows = (
                (1, "EyePoint crashes as wet AMD trial fails", '["amd"]', 70, 0),     # unlinked, still shown
                (2, "AMD shares rise", '["amd"]', 60, 0),                             # kept
                (3, "Vermont apple orchards open for picking", '["apple"]', 0, 0),     # unlinked and hidden
                (4, "Apple orchards and iPhone sales", '["apple"]', 50, 0),           # product name: kept
                (5, "Quarterly results are out", '["apple"]', 10, 0),                 # linked by its feed: kept
                (6, "Wet AMD drug approved", '["amd","apple"]', 5, 1),                # official: kept
            )
            for item_id, title, companies, score, official in rows:
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at,companies,
                                                score,tmt,official) VALUES(?,1,?,?,'stock',?,?,?,?,1,?)""",
                           (item_id, f"https://e.test/{item_id}", title, "2026-10-19T00:00:00Z",
                            "2026-10-19T00:00:00Z", companies, score, official))
                for slug in json.loads(companies):
                    db.execute("INSERT INTO item_companies(item_id,company_id) VALUES(?,?)",
                               (item_id, 1 if slug == "amd" else 2))
        with database.get_db(path) as db:
            self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:63]), (63,))
            items = {row[0]: (json.loads(row[1]), row[2]) for row in db.execute("SELECT id,companies,tmt FROM items")}
            links = sorted(tuple(row) for row in db.execute("SELECT item_id, company_id FROM item_companies"))
        self.assertEqual(items, {1: ([], 1), 2: (["amd"], 1), 3: ([], 0), 4: (["apple"], 1),
                                 5: (["apple"], 1), 6: (["amd", "apple"], 1)})
        self.assertEqual(links, [(2, 1), (4, 2), (5, 2), (6, 1), (6, 2)])


if __name__ == "__main__":
    unittest.main()
