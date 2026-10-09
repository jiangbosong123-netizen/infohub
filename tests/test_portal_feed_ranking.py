import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.web import routes

TIMES = [f"2026-09-{day:02d}T{hour:02d}:00:00+00:00" for day in (20, 21) for hour in (8, 9)]


def window_reference(cte: str, base: str, rank_score: str) -> str:
    """The ROW_NUMBER() query the portal used before _first_per_story, kept verbatim."""
    return (cte + ", " if cte else "WITH ") + """eligible AS (""" + base + f"""), ranked AS (
        SELECT eligible.*, ROW_NUMBER() OVER (
            PARTITION BY COALESCE(story_id, 'item:' || id)
            ORDER BY published_at DESC, official DESC, COALESCE({rank_score},-1) DESC, id DESC
        ) AS story_rank FROM eligible)
        SELECT * FROM ranked WHERE story_rank=1
        ORDER BY published_at DESC,id DESC LIMIT ? OFFSET ?"""


def old_query_items(channel="all", company="", event="", cat="", limit=60, offset=0):
    """The previous _query_items selected branch, kept verbatim as the reference."""
    cte, curation_join, visible, score_expr, category_expr = routes.portal_curation_sql(
        routes.CURATION_READ_ENABLED)
    where = f" WHERE {visible}"
    params: list = []
    if routes.CURATED_FEED_ENABLED:
        where += " AND " + routes._selected_clause(score_expr=score_expr)
    if channel and channel != "all":
        where += " AND i.channel=?"
        params.append(channel)
    if cat:
        where += f" AND {category_expr}=?"
        params.append(cat)
    if company:
        where += (" AND EXISTS (SELECT 1 FROM item_companies ic JOIN companies c "
                  "ON c.id=ic.company_id WHERE ic.item_id=i.id AND c.slug=?)")
        params.append(company)
    if event:
        where += " AND i.event_type=?"
        params.append(event)
    base = """SELECT i.*, s.name AS source_name, si.story_id
              FROM items i JOIN sources s ON s.id=i.source_id
              LEFT JOIN story_items si ON si.item_id=i.id""" + curation_join + where
    with database.get_db() as db:
        return db.execute(window_reference(cte, base, "score"), [*params, limit, offset]).fetchall()


class PortalFeedRankingTests(unittest.TestCase):
    """Random items with many ties: the streamed feed must equal the window ranking."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for target, name in ((database, "DB_PATH"), (config, "DB_PATH")):
            item = patch.object(target, name, path)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        rng = random.Random(20261005)
        with database.get_db() as db:
            for source_id, channel in ((1, "ai"), (2, "stock"), (3, "ai")):
                db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                              VALUES(?,?,?,?,'media','rss')""",
                           (source_id, f"s{source_id}", f"Source {source_id}", channel))
            db.execute("""INSERT INTO companies(id,slug,name,market) VALUES(1,'acme','Acme','US'),
                          (2,'globex','Globex','US')""")
            for story in range(25):
                db.execute("""INSERT INTO stories(id,title,channel,url,first_at,last_at,source_count)
                              VALUES(?,?,?,?,?,?,?)""",
                           (f"story-{story:02d}", f"Story {story}", "ai", f"https://e.test/s{story}",
                            TIMES[0], TIMES[-1], rng.choice((1, 2, 3))))
            for item_id in range(1, 241):
                source_id = rng.choice((1, 2, 3))
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,ai_cat,
                              official,event_type,published_at,fetched_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (item_id, source_id, f"https://e.test/{item_id}", f"Item {item_id}",
                            "stock" if source_id == 2 else "ai",
                            rng.choice((None, 40, 70, 70, 90)), rng.choice((None, 1, 1, 0)),
                            rng.choice(("", "model", "product")), rng.choice((0, 0, 1)),
                            rng.choice(("", "earnings", "product")), rng.choice(TIMES), TIMES[-1]))
                if rng.random() < 0.8:
                    db.execute("INSERT INTO story_items(item_id,story_id) VALUES(?,?)",
                               (item_id, f"story-{rng.randrange(25):02d}"))
                if rng.random() < 0.3:
                    db.execute("INSERT INTO item_companies(item_id,company_id) VALUES(?,?)",
                               (item_id, rng.choice((1, 2))))

    def test_selected_feed_matches_window_ranking(self):
        compared = 0
        for channel in ("all", "ai", "stock"):
            for cat, company, event in (("", "", ""), ("model", "", ""), ("", "acme", ""),
                                        ("", "", "earnings"), ("product", "globex", "product")):
                for limit, offset in ((5, 0), (5, 5), (7, 3), (60, 0), (30, 30)):
                    expected = [tuple(row) for row in old_query_items(
                        channel, company, event, cat, limit, offset)]
                    actual = [tuple(row) for row in routes._query_items(
                        channel=channel, company=company, event=event, cat=cat,
                        mode="selected", limit=limit, offset=offset)]
                    self.assertEqual(actual, expected, (channel, cat, company, event, limit, offset))
                    compared += len(expected)
        self.assertGreater(compared, 300)

    def test_first_per_story_matches_window_ranking_for_a_topic_base(self):
        with database.get_db() as db:
            db.execute("""INSERT INTO topics(slug,name,group_key,description,rules,position)
                          VALUES('t','T','g','d','{}',0)""")
            db.executemany("INSERT INTO item_topics(item_id,topic_slug,evidence) VALUES(?, 't', '[]')",
                           [(item_id,) for item_id in range(1, 241, 2)])
            base = """SELECT i.*,s.name AS source_name,si.story_id FROM item_topics it
                      JOIN items i ON i.id=it.item_id JOIN sources s ON s.id=i.source_id
                      LEFT JOIN story_items si ON si.item_id=i.id
                      WHERE it.topic_slug=? AND COALESCE(i.tmt,1)!=0"""
            for limit, offset in ((21, 0), (21, 20), (4, 9)):
                params = ("t", limit, offset)
                expected = db.execute(window_reference("", base, "score"), params).fetchall()
                actual = db.execute(routes._first_per_story("", base, "score"), params).fetchall()
                self.assertEqual([tuple(r) for r in actual], [tuple(r) for r in expected])
                self.assertTrue(expected)


if __name__ == "__main__":
    unittest.main()
