import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database
from app.web import routes
from tests.test_query_plans import plan, traced_statements

TIMES = [f"2026-09-{day:02d}T{hour:02d}:00:00+00:00" for day in (18, 19, 20, 21) for hour in (8, 9, 10)]
TOPICS = (("big", 0.6), ("mid", 0.2), ("small", 0.03), ("tied-a", 0.1), ("tied-b", 0.1), ("empty", 0))


def old_topic_stats(slug=None):
    """_topic_stats before the per-item index form, kept verbatim as the reference."""
    cte, _, _, score_expr, _ = routes.portal_curation_sql(routes.CURATION_READ_ENABLED, streamed=True)
    if routes.CURATION_READ_ENABLED:
        score_expr = "(SELECT cv.score FROM curation_values cv WHERE cv.item_id=i.id)"
    selected = routes._selected_clause(score_expr=score_expr) if routes.CURATED_FEED_ENABLED else "1=1"
    item_join = ("LEFT JOIN items i ON i.id=it.item_id AND "
                 "(SELECT cv.visible FROM curation_values cv WHERE cv.item_id=it.item_id)=1"
                 if routes.CURATION_READ_ENABLED else
                 "LEFT JOIN items i ON i.id=it.item_id AND COALESCE(i.tmt,1)!=0")
    with database.get_db() as db:
        return [dict(r) for r in db.execute(cte + f"""SELECT t.*,COUNT(i.id) AS total,
            COALESCE(SUM(CASE WHEN i.id IS NOT NULL AND {selected} THEN 1 ELSE 0 END),0) AS selected,
            MAX(i.published_at) AS last_at FROM topics t
            LEFT JOIN item_topics it ON it.topic_slug=t.slug
            {item_join}
            WHERE t.enabled=1{' AND t.slug=?' if slug is not None else ''}
            GROUP BY t.slug ORDER BY t.position""", () if slug is None else (slug,))]


def old_topic_feed_ids(slug, mode, page):
    """The topic feed joined from the topic's assignments, as before, kept verbatim."""
    cte, curation_join, visible, score_expr, _ = routes.portal_curation_sql(
        routes.CURATION_READ_ENABLED, streamed=True)
    selected = (" AND " + routes._selected_clause(score_expr=score_expr)
                if mode == "selected" and routes.CURATED_FEED_ENABLED else "")
    base = f"""SELECT i.*,s.name AS source_name,si.story_id
        {', cv.score AS curation_rank_score' if routes.CURATION_READ_ENABLED else ''} FROM item_topics it
        JOIN items i ON i.id=it.item_id JOIN sources s ON s.id=i.source_id
        LEFT JOIN story_items si ON si.item_id=i.id
        {curation_join}
        WHERE it.topic_slug=? AND {visible} {selected}"""
    if mode == "selected":
        sql = routes._first_per_story(
            cte, base, "curation_rank_score" if routes.CURATION_READ_ENABLED else "score")
    else:
        sql = cte + base + " ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?"
    with database.get_db() as db:
        rows = db.execute(sql, (slug, 21, (page - 1) * 20)).fetchall()
    return [row["id"] for row in rows[:20]], len(rows) > 20


class TopicPageTests(unittest.TestCase):
    """Random items in overlapping topics: counts and feeds must equal the previous queries."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for item in (patch.object(database, "DB_PATH", path), patch.object(config, "DB_PATH", path),
                     patch.object(routes, "TOPIC_READ_ENABLED", False),
                     patch.object(routes, "CURATION_HOT_ENABLED", False)):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        rng = random.Random(20261006)
        with database.get_db() as db:
            db.execute("DELETE FROM item_topics")
            db.execute("DELETE FROM topics")
            for source_id, channel in ((1, "ai"), (2, "stock")):
                db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                              VALUES(?,?,?,?,'media','rss')""",
                           (source_id, f"s{source_id}", f"Source {source_id}", channel))
            for position, (slug, _) in enumerate(TOPICS):
                # Two topics share a position, so their relative order is part of the comparison.
                db.execute("""INSERT INTO topics(slug,name,group_key,description,rules,position)
                              VALUES(?,?,'g','d','{}',?)""",
                           (slug, slug.title(), min(position, 3)))
            db.execute("""INSERT INTO topics(slug,name,group_key,description,rules,position,enabled)
                          VALUES('off','Off','g','d','{}',9,0)""")
            for story in range(30):
                db.execute("""INSERT INTO stories(id,title,channel,url,first_at,last_at,source_count)
                              VALUES(?,?,?,?,?,?,?)""",
                           (f"story-{story:02d}", f"Story {story}", "ai", f"https://e.test/s{story}",
                            TIMES[0], TIMES[-1], rng.choice((1, 2, 3))))
            for item_id in range(1, 401):
                source_id = rng.choice((1, 1, 2))
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,official,
                              published_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (item_id, source_id, f"https://e.test/{item_id}", f"Item {item_id}",
                            "stock" if source_id == 2 else "ai", rng.choice((None, 40, 70, 90)),
                            rng.choice((None, 1, 1, 0)), rng.choice((0, 0, 1)),
                            rng.choice(TIMES), TIMES[-1]))
                if rng.random() < 0.7:
                    db.execute("INSERT INTO story_items(item_id,story_id) VALUES(?,?)",
                               (item_id, f"story-{rng.randrange(30):02d}"))
                for slug, chance in (*TOPICS, ("off", 0.2)):
                    if rng.random() < chance:
                        db.execute("""INSERT INTO item_topics(item_id,topic_slug,evidence)
                                      VALUES(?,?,'[]')""", (item_id, slug))
        self.client = TestClient(routes.app)

    def flags(self):
        for read in (False, True):
            for feed in (False, True):
                with patch.object(routes, "CURATION_READ_ENABLED", read), \
                        patch.object(routes, "CURATED_FEED_ENABLED", feed):
                    yield read, feed

    def test_topic_index_counts_match_the_per_assignment_query(self):
        for read, feed in self.flags():
            expected = old_topic_stats()
            with database.get_db() as db:
                self.assertEqual(routes._topic_stats(db), expected, (read, feed))
            for slug, _ in TOPICS:
                with database.get_db() as db:
                    self.assertEqual(routes._topic_stats(db, slug), old_topic_stats(slug))
            totals = {row["slug"]: (row["total"], row["selected"]) for row in expected}
            if feed:
                self.assertGreater(totals["big"][0], totals["big"][1])
            else:
                self.assertEqual(totals["big"][0], totals["big"][1])
            self.assertEqual(totals["empty"], (0, 0))
            self.assertNotIn("off", totals)
            page = self.client.get("/topics")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.context["total"], len(expected))

    def test_topic_feed_matches_the_assignment_joined_feed(self):
        compared = 0
        for read, feed in self.flags():
            for slug, _ in TOPICS:
                for mode in ("selected", "all"):
                    for page in (1, 2, 3, 9):
                        expected_ids, expected_next = old_topic_feed_ids(slug, mode, page)
                        response = self.client.get(f"/topics/{slug}?mode={mode}&page={page}")
                        self.assertEqual(response.status_code, 200)
                        ids = [row["id"] for day in response.context["days"] for row in day["rows"]]
                        self.assertEqual((ids, response.context["has_next"]),
                                         (expected_ids, expected_next), (read, feed, slug, mode, page))
                        compared += len(ids)
        self.assertGreater(compared, 1000)

    def test_topic_feed_streams_newest_items_with_the_member_list_as_a_filter(self):
        for read, feed in self.flags():
            with traced_statements() as statements:
                self.client.get("/topics/big?mode=all")
            feeds = [sql for sql in statements if "FROM items i" in sql and "item_topics WHERE topic_slug=" in sql]
            self.assertEqual(len(feeds), 1, (read, feed))
            lines = plan(feeds[0])
            self.assertIn("SCAN i USING INDEX idx_items_pub", lines, lines)
            self.assertFalse([line for line in lines if "TEMP B-TREE FOR ORDER BY" in line], lines)


if __name__ == "__main__":
    unittest.main()
