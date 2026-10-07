import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import company_match, database
from app.crawler.runner import insert_item
from app.stories import match_score, refresh_derived

T0 = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
SOURCES = (("wire-sina", "sina"), ("wire-cls", "cls"), ("wire-wscn", "wscn_live"), ("feed", "rss"))


class FlashHeadlineTests(unittest.TestCase):
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
            db.executemany("INSERT INTO sources(key,name,channel,type) VALUES(?,?,'stock',?)",
                           [(key, key, kind) for key, kind in SOURCES])
        self.number = 0

    def item(self, source, title, minutes=0, **kwargs):
        self.number += 1
        raw = dict(title=title, url=f"https://example.com/{self.number}", companies=[],
                   published_at=(T0 + timedelta(minutes=minutes)).isoformat())
        raw.update(kwargs)
        self.assertTrue(insert_item(source, raw))
        with database.get_db() as db:
            return db.execute("SELECT max(id) FROM items").fetchone()[0]

    def stories(self, *item_ids):
        with database.get_db() as db:
            return [db.execute("SELECT story_id FROM story_items WHERE item_id=?", (i,)).fetchone()[0]
                    for i in item_ids]

    def row(self, item_id):
        with database.get_db() as db:
            return dict(db.execute("SELECT i.*,s.type AS source_type FROM items i JOIN sources s ON s.id=i.source_id "
                                   "WHERE i.id=?", (item_id,)).fetchone())

    def test_three_wires_with_one_headline_form_one_story(self):
        # Seen 2026-10-02: the body's and the dateline's digits, and an AI event type that
        # disagreed between copies, kept these in three stories under titles-v3.
        ids = [self.item("wire-cls", "财联社10月2日电，特斯拉第三季度交付量486532辆，预估463761辆",
                         event_type="earnings"),
               self.item("wire-sina", "【特斯拉第三季度交付量486532辆，预估463761辆】据公司公告，10月2日披露的数据显示",
                         minutes=1, event_type="product"),
               self.item("wire-wscn", "特斯拉第三季度交付量486532辆, 预估463761辆", minutes=3)]
        refresh_derived()
        self.assertEqual(len(set(self.stories(*ids))), 1)

    def test_a_copy_joins_the_member_that_shares_its_headline(self):
        anchor = self.item("feed", "特斯拉第三季度交付量超出市场预期", event_type="earnings")
        member = self.item("wire-cls", "财联社10月2日电，特斯拉第三季度交付量超出市场预期，股价盘后走高",
                           minutes=5, event_type="earnings")
        refresh_derived()
        story = self.stories(anchor, member)
        self.assertEqual(story[0], story[1])
        # A later wire copy disagrees with the anchor's event type, so only the member matches it.
        copy = self.item("wire-wscn", "特斯拉第三季度交付量超出市场预期，股价盘后走高", minutes=40, event_type="product")
        self.assertEqual(match_score(self.row(copy), self.row(anchor)), 0.0)
        self.assertEqual(refresh_derived()["processed"], 1)
        self.assertEqual(self.stories(copy), [story[0]])

    def test_a_recurring_headline_a_day_apart_stays_separate(self):
        same_day = [self.item("wire-sina", "【美股盘前要闻速递】英伟达盘前上涨，市场关注周五就业数据"),
                    self.item("wire-sina", "【美股盘前要闻速递】苹果发布会前夕，期货小幅走低", minutes=60)]
        next_day = self.item("wire-sina", "【美股盘前要闻速递】油价大涨拖累航空股，美债收益率回落", minutes=24 * 60)
        refresh_derived()
        first, second, third = self.stories(*same_day, next_day)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_cls_brackets_are_column_names_not_headlines(self):
        ids = [self.item("wire-cls", "【公司公告与研报解读】工信部要求推动算力设施扩容提质，国产算力迎来窗口"),
               self.item("wire-cls", "【公司公告与研报解读】人形机器人进入量产元年，电子皮肤市场快速增长", minutes=2)]
        refresh_derived()
        self.assertEqual(len(set(self.stories(*ids))), 2)

    def test_official_documents_keep_their_own_guard(self):
        ids = [self.item("feed", "Tesla quarterly report on form 10-Q", official=1, extra={"form": "10-Q"}),
               self.item("feed", "Tesla quarterly report on form 10-Q", minutes=5, official=1, extra={"form": "10-Q"})]
        refresh_derived()
        self.assertEqual(len(set(self.stories(*ids))), 2)


if __name__ == "__main__":
    unittest.main()
