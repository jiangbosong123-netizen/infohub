import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import company_match, database
from app.crawler.runner import insert_item
from app.stories import _Facts, refresh_derived

# Real titles (and the AI's translations) from the legacy Google News items.
DAILY_MOVES = [
    "Why is Arm stock sliding today? By Investing.com",
    "Why Is XPeng Stock Gaining Thursday?",
    "What's Going On With Oracle Stock Tuesday?",
    "What Is Going on With Amazon Stock on Monday?",
    "Why Is ORCL Stock Tumbling 10% Overnight?",
    "Why Is TSM Stock Rising Premarket?",
    "Why Alphabet Stock Just Popped",
    "Why Is Apple Stock Falling When Chipmakers Are Rising?",
    "Meta (META) Stock Trades Up, Here Is Why",
    "Taiwan Semiconductor Manufacturing (NYSE:TSM) Stock Price Down 1.2% - Here's What Happened",
    "Palantir stock price ended at $167.23 on Friday, after gaining 0.83%",
    "QQQ is up 1.7% today, on AMD stock price movement",
    "Red day on Tuesday for Broadcom stock after losing 1.58%",
    "AVGO Stock Falls Premarket: China Reportedly Audits Broadcom's Grip On State-Backed Data Centers",
    "Apple (AAPL) Rises Higher Than Market: Key Facts",
    "甲骨文股价今日下跌原因分析",
    "亚马逊周一股价走势如何？",
    "Meta Platforms 股价动态",
    "甲骨文股价下跌：发生了什么？",
]
OTHER_STOCK_PIECES = [
    "Why AMD Stock Jumped 30% in September",
    "Nvidia and AMD Can't Make AI Chips Without This Growth Stock. Here's Why",
    "Apple's Foldable Phone Has Arrived. Here's Why I Don't Think It'll Send the Stock Soaring",
    "Why Meta's 13% Stock Gain Last Week Could Be Just the Start",
    "UBS raises Palantir stock price target on strong AI demand momentum",
    "Prediction: This Will Be Palantir's Stock Price by the End of 2028",
    "Step Aside, Nvidia: 1 Reason Why Meta Platforms Could Become the Next AI Stock to Watch",
    "AMD股价为何在9月大涨30%",
]


class StockMoveTemplateTests(unittest.TestCase):
    def test_daily_move_pieces_are_recognised(self):
        for title in DAILY_MOVES:
            with self.subTest(title=title):
                self.assertTrue(_Facts({"title": title, "title_zh": None}).stock_move)

    def test_recaps_opinions_and_targets_are_not(self):
        for title in OTHER_STOCK_PIECES:
            with self.subTest(title=title):
                self.assertFalse(_Facts({"title": title, "title_zh": None}).stock_move)

    def test_a_translation_counts(self):
        self.assertTrue(_Facts({"title": "Why Arm Holdings Has Investors Talking",
                                "title_zh": "Arm股价今日为何上涨？"}).stock_move)


class StockMoveClusteringTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(database, "DB_PATH", Path(folder.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('feed','Feed','stock','googlenews')")
        self.number = 0

    def item(self, title, published_at):
        self.number += 1
        self.assertTrue(insert_item("feed", dict(title=title, url=f"https://example.com/{self.number}",
                                                 published_at=published_at, companies=["arm"])))
        with database.get_db() as db:
            return db.execute("SELECT max(id) FROM items").fetchone()[0]

    def story(self, item_id):
        with database.get_db() as db:
            return db.execute("SELECT story_id FROM story_items WHERE item_id=?", (item_id,)).fetchone()[0]

    def test_a_days_move_only_joins_pieces_of_the_same_us_trading_day(self):
        # 2026-09-28 14:01Z and 09-29 02:59Z are both Monday 28 September in New York.
        monday = [self.item("Why is Arm stock sliding today?", "2026-09-28T14:01:00+00:00"),
                  self.item("Why is Arm stock sliding today? By Investing.com", "2026-09-29T02:59:00+00:00")]
        tuesday = self.item("Why is Arm stock sliding today?", "2026-09-29T14:34:00+00:00")
        refresh_derived()
        self.assertEqual(self.story(monday[0]), self.story(monday[1]))
        self.assertNotEqual(self.story(monday[0]), self.story(tuesday))


    def test_copies_of_one_days_piece_leave_a_story_anchored_on_another_day(self):
        # An older matcher filed Thursday's copies under Tuesday's piece, as former anchors of two
        # events merged into it; each copy would otherwise keep the other there.
        tuesday = self.item("Why is Arm stock up 3% today?", "2026-09-15T13:55:00+00:00")
        thursday = [self.item("Why is Arm stock rising today? By Investing.com", "2026-09-17T09:38:00+00:00"),
                    self.item("Why is Arm stock rising today? By Investing.com", "2026-09-17T10:49:00+00:00")]
        refresh_derived()
        story = self.story(tuesday)
        with database.get_db() as db:
            for n, item_id in enumerate(thursday):
                db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,first_at,last_at,redirect_to)
                              VALUES(?,?,'t','stock','https://e.test',?,?,?)""",
                           (f"merged-{n}", item_id, "2026-09-17", "2026-09-17", story))
                db.execute("UPDATE story_items SET story_id=? WHERE item_id=?", (story, item_id))
            db.execute("UPDATE story_items SET match_reason='titles-v2'")
        refresh_derived()
        self.assertEqual(self.story(thursday[0]), self.story(thursday[1]))
        self.assertNotEqual(self.story(thursday[0]), story)

if __name__ == "__main__":
    unittest.main()
