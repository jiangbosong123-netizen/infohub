import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import company_match, database
from app.crawler.runner import insert_item
from app.stories import refresh_derived
from app.web.routes import app

T0 = datetime.now(timezone.utc) - timedelta(hours=3)


class StoryMergeTests(unittest.TestCase):
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
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('feed','Feed','ai','rss')")
        self.number = 0

    def item(self, title, minutes, **kwargs):
        self.number += 1
        raw = dict(title=title, url=f"https://example.com/{self.number}", companies=["openai"],
                   published_at=(T0 + timedelta(minutes=minutes)).isoformat())
        raw.update(kwargs)
        self.assertTrue(insert_item("feed", raw))
        with database.get_db() as db:
            return db.execute("SELECT max(id) FROM items").fetchone()[0]

    def story(self, item_id):
        with database.get_db() as db:
            return db.execute("SELECT story_id FROM story_items WHERE item_id=?", (item_id,)).fetchone()[0]

    def edit(self, item_id, **values):
        with database.get_db() as db:
            for column, value in values.items():
                db.execute(f"UPDATE items SET {column}=? WHERE id=?", (value, item_id))

    def test_two_events_merge_once_a_translation_makes_their_anchors_match(self):
        english = [self.item("OpenAI releases a new coding platform for developers", 0),
                   self.item("OpenAI releases a new coding platform for developers today", 1)]
        chinese = [self.item("OpenAI 发布全新开发者编程平台", 2), self.item("OpenAI 发布全新开发者编程平台 今日", 3)]
        refresh_derived()
        earlier, later = self.story(english[0]), self.story(chinese[0])
        self.assertNotEqual(earlier, later)
        self.assertEqual({self.story(i) for i in english}, {earlier})
        self.edit(english[0], title_zh="OpenAI 发布全新开发者编程平台")
        self.assertEqual(refresh_derived()["merged"], 1)
        self.assertEqual({self.story(i) for i in english + chinese}, {earlier})
        response = TestClient(app).get("/story/" + later, follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (302, "/story/" + earlier))
        with database.get_db() as db:
            counts = dict(db.execute("SELECT id,item_count FROM stories WHERE id IN (?,?)", (earlier, later)).fetchall())
        self.assertEqual(counts, {earlier: 4, later: 0})

    def three_events_two_merged(self):
        # Event types keep three events apart. Once B's and C's types are cleared, B's anchor
        # matches A's best and C's matches only B's: A and C differ in their version number.
        titles = {"a": "OpenAI launches GPT 4.1 coding platform for developers",
                  "b": "OpenAI launches GPT coding platform for developers",
                  "c": "OpenAI launches GPT 4.2 coding platform for all developers worldwide"}
        ids = {}
        for n, (name, kind) in enumerate((("a", "product"), ("b", "earnings"), ("c", "ma"))):
            ids[name] = [self.item(titles[name], 2 * n, event_type=kind),
                         self.item(titles[name] + " today", 2 * n + 1, event_type=kind)]
        refresh_derived()
        self.assertEqual(len({self.story(i[0]) for i in ids.values()}), 3)
        self.edit(ids["b"][0], event_type="")
        self.edit(ids["c"][0], event_type="")
        self.assertEqual(refresh_derived()["merged"], 1)
        return ids

    def test_editing_a_merged_events_anchor_rechecks_the_members_it_brought(self):
        ids = self.three_events_two_merged()
        self.assertEqual(self.story(ids["b"][1]), self.story(ids["a"][0]))
        self.edit(ids["b"][0], title="Paris weather turns cold this week")
        refresh_derived()
        self.assertNotEqual(self.story(ids["b"][1]), self.story(ids["a"][0]))
        self.assertNotEqual(self.story(ids["b"][0]), self.story(ids["a"][0]))

    def test_merges_do_not_chain_through_a_story_merged_in_the_same_pass(self):
        ids = self.three_events_two_merged()
        self.assertEqual({self.story(i) for i in ids["a"] + ids["b"]}, {self.story(ids["a"][0])})
        self.assertEqual({self.story(i) for i in ids["c"]}, {self.story(ids["c"][0])})
        self.assertNotEqual(self.story(ids["c"][0]), self.story(ids["a"][0]))
        # B's other article matched B's anchor, not A's (their event types differ); it stays
        # because B's anchor still represents the members it brought, also on a full reindex.
        partition = {i: self.story(i) for group in ids.values() for i in group}
        with database.get_db() as db:
            db.execute("UPDATE story_items SET match_reason='titles-v3'")
        self.assertEqual(refresh_derived()["merged"], 0)
        self.assertEqual({i: self.story(i) for i in partition}, partition)

    def test_an_anchor_sharing_a_headline_with_another_events_member_merges(self):
        english = self.item("OpenAI coding platform passes one million developers", 0)
        wires = [self.item("OpenAI编程平台开发者突破100万", minutes) for minutes in (5, 20, 25)]
        refresh_derived()
        earlier, later = self.story(english), self.story(wires[0])
        self.assertEqual({self.story(i) for i in wires}, {later})
        # A split an older matcher left behind: one wire copy sits with the English report,
        # while the event anchored on another copy keeps its anchor when re-checked.
        with database.get_db() as db:
            db.execute("UPDATE story_items SET story_id=? WHERE item_id=?", (earlier, wires[1]))
            db.execute("INSERT INTO derived_dirty(item_id) VALUES(?)", (wires[0],))
        self.assertEqual(refresh_derived()["merged"], 1)
        self.assertEqual({self.story(i) for i in [english, *wires]}, {earlier})

    def test_an_anchor_finds_another_event_with_its_headline_past_its_own_members(self):
        copies = [self.item("OpenAI编程平台开发者突破100万", minutes) for minutes in (0, 5, 20, 25)]
        refresh_derived()
        story = self.story(copies[0])
        # Two events carrying one headline, as an older matcher could leave them; the second
        # holds the earliest copy, so its anchor's own members must not hide the first event.
        with database.get_db() as db:
            db.execute("UPDATE stories SET anchor_item_id=? WHERE id=?", (copies[1], story))
            db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,first_at,last_at)
                          SELECT 'second',id,title,channel,url,published_at,published_at FROM items WHERE id=?""",
                       (copies[3],))
            db.execute("UPDATE story_items SET story_id='second' WHERE item_id IN (?,?)", (copies[0], copies[3]))
            db.execute("INSERT INTO derived_dirty(item_id) VALUES(?)", (copies[3],))
        self.assertEqual(refresh_derived()["merged"], 1)
        self.assertEqual({self.story(i) for i in copies}, {story})

    def test_unrelated_events_stay_apart(self):
        ids = [self.item("OpenAI releases a new coding platform for developers", 0),
               self.item("OpenAI releases a new coding platform for developers today", 1),
               self.item("OpenAI signs a cloud computing contract with Oracle", 2),
               self.item("OpenAI signs a cloud computing contract with Oracle today", 3)]
        self.assertEqual(refresh_derived()["merged"], 0)
        self.assertEqual(len({self.story(i) for i in ids}), 2)


if __name__ == "__main__":
    unittest.main()
