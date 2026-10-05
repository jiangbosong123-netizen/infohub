import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.curation_hot_query import curated_top_clusters
from app.web import routes
from tests.test_query_plans import plan, traced_statements

NOW = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def old_top_cluster_ids(limit, channel="all", topic="", days=2):
    """The legacy branch of _top_clusters before story_member_filters, kept verbatim."""
    cutoff = (NOW - timedelta(days=days)).isoformat()
    cte, curation_join, visible, _, _ = routes.portal_curation_sql(
        routes.CURATION_READ_ENABLED, streamed=True)
    any_visible = (f"AND EXISTS(SELECT 1 FROM story_items si JOIN items i ON i.id=si.item_id "
                   f"{curation_join} WHERE si.story_id=st.id AND {visible})"
                   if routes.CURATION_READ_ENABLED else "")
    with database.get_db() as db:
        return [row["id"] for row in db.execute(
            cte + f"""SELECT st.*,st.last_at AS updated_at FROM stories st
               WHERE st.redirect_to IS NULL AND st.item_count>0 AND st.last_at>=?
               {any_visible}
               AND (?='all' OR EXISTS(SELECT 1 FROM story_items si JOIN items i ON i.id=si.item_id
                   {curation_join} WHERE si.story_id=st.id AND i.channel=? AND {visible}))
               AND (?='' OR EXISTS(SELECT 1 FROM story_items si JOIN item_topics it ON it.item_id=si.item_id
                   JOIN items i ON i.id=si.item_id {curation_join}
                   WHERE si.story_id=st.id AND it.topic_slug=? AND {visible}))
               ORDER BY (st.source_count>=2) DESC,st.heat DESC,st.id LIMIT ?""",
            (cutoff, channel, channel, topic, topic, limit))]


class StoryHotListFilterTests(unittest.TestCase):
    """Stories mixing channels, topics, hidden items and ages: filters must keep the same rows."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for item in (patch.object(database, "DB_PATH", path), patch.object(config, "DB_PATH", path),
                     patch.object(routes, "datetime", _Clock),
                     patch.object(routes, "CURATION_HOT_ENABLED", False)):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        rng = random.Random(20261006)
        with database.get_db() as db:
            for source_id, channel in ((1, "ai"), (2, "stock"), (3, "robot")):
                db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                              VALUES(?,?,?,?,'media','rss')""",
                           (source_id, f"s{source_id}", f"Source {source_id}", channel))
            db.executemany("""INSERT INTO topics(slug,name,group_key,description,rules,position)
                              VALUES(?,?,'g','d','{}',?)""",
                           [("chips", "Chips", 0), ("policy", "Policy", 1), ("rare", "Rare", 2)])
            for story in range(60):
                last_at = NOW - timedelta(hours=rng.choice((3, 20, 47, 49, 100, 300, 400)), minutes=30)
                db.execute("""INSERT INTO stories(id,title,channel,url,first_at,last_at,source_count,
                              heat,item_count,redirect_to) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (f"story-{story:02d}", f"Story {story}", "ai", f"https://e.test/s{story}",
                            last_at.isoformat(), last_at.isoformat(), rng.choice((1, 2, 3)),
                            rng.choice((0.1, 0.5, 0.5, 0.9)), rng.choice((0, 1, 2, 2)),
                            "story-00" if story and rng.random() < 0.1 else None))
            for item_id in range(1, 301):
                source_id = rng.choice((1, 1, 2, 3))
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,
                              published_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                           (item_id, source_id, f"https://e.test/{item_id}", f"Item {item_id}",
                            ("ai", "stock", "robot")[source_id - 1], rng.choice((None, 40, 90)),
                            rng.choice((None, 1, 0, 0)), NOW.isoformat(), NOW.isoformat()))
                if rng.random() < 0.85:
                    db.execute("INSERT INTO story_items(item_id,story_id) VALUES(?,?)",
                               (item_id, f"story-{rng.randrange(60):02d}"))
                for slug, chance in (("chips", 0.3), ("policy", 0.2), ("rare", 0.02)):
                    if rng.random() < chance:
                        db.execute("""INSERT INTO item_topics(item_id,topic_slug,evidence)
                                      VALUES(?,?,'[]')""", (item_id, slug))

    def test_filters_keep_exactly_the_previous_stories(self):
        compared = 0
        unfiltered = {}
        for read in (False, True):
            with patch.object(routes, "CURATION_READ_ENABLED", read):
                for days in (2, 14, 30):
                    for channel in ("all", "ai", "stock", "robot", "none"):
                        for topic in ("", "chips", "policy", "rare", "missing"):
                            for limit in (5, 50):
                                expected = old_top_cluster_ids(limit, channel, topic, days)
                                actual = [row["id"] for row in routes._top_clusters(
                                    limit, channel=channel, topic=topic, days=days)]
                                self.assertEqual(actual, expected,
                                                 (read, days, channel, topic, limit))
                                compared += len(expected)
                                if channel == "all" and not topic:
                                    unfiltered[read, days, limit] = expected
        self.assertGreater(compared, 1000)
        # Stories whose members are all hidden drop out only when visibility is read.
        self.assertTrue(any(unfiltered[False, days, limit] != unfiltered[True, days, limit]
                            for days in (2, 14, 30) for limit in (5, 50)))

    def test_story_filters_start_from_the_storys_members(self):
        cutoff = (NOW - timedelta(days=2)).isoformat()
        for read in (False, True):
            with patch.object(routes, "CURATION_READ_ENABLED", read), \
                    traced_statements() as statements:
                routes._top_clusters(8, channel="stock", topic="chips", days=2)
                with database.get_db() as db:
                    curated_top_clusters(db, limit=8, channel="stock", topic="chips",
                                         cutoff=cutoff, now=NOW)
            reads = [sql for sql in statements if "FROM stories st" in sql or "FROM curation_story_metrics m" in sql]
            self.assertEqual(len(reads), 2, statements)
            for sql in reads:
                lines = plan(sql)
                self.assertFalse([line for line in lines if "idx_items_channel_pub" in line], lines)
                self.assertGreaterEqual(
                    len([line for line in lines if "idx_story_items_story" in line]), 2, lines)


if __name__ == "__main__":
    unittest.main()
