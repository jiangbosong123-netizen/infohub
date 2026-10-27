import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, db_admin, hot_pairs, worker
from app.web import routes

NOW = datetime.now(timezone.utc)


def at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


class HotPairsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "DB_PATH", Path(folder.name) / "app.db")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS)
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'wire','Wire','ai','rss')")
            # (id, title, companies, heat, first seen hours ago)
            for number, (story, title, companies, heat, hours) in enumerate((
                    ("s1", "Joel Kaplan leaving Meta after 15 years", ["meta"], 0.9, 5),
                    ("s2", "Anthropic in talks with Nscale", ["anthropic"], 0.8, 6),
                    ("s3", "Meta's Joel Kaplan to step down next year", ["meta"], 0.7, 4),
                    ("s4", "Meta ships new glasses", ["meta"], 0.6, 70),       # too far apart from s1/s3
                    ("s5", "Google cloud outage takes down banks", ["google"], 0.5, 3)), 1):
                db.execute("""INSERT INTO items(id,source_id,url,title,summary,channel,published_at,fetched_at)
                              VALUES(?,1,?,?,?,'ai',?,?)""", (number, f"https://e.test/{number}", title,
                                                             f"summary {number}", at(hours), at(hours)))
                db.execute("""INSERT INTO stories(id,anchor_item_id,title,channel,url,heat,source_count,item_count,
                                                  first_at,last_at,company_slugs)
                              VALUES(?,?,?,'ai',?,?,2,3,?,?,?)""",
                           (story, number, title, f"https://e.test/{number}", heat, at(hours), at(1),
                            json.dumps(companies)))
        enabled = patch.object(config, "llm_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_only_pairs_sharing_a_company_within_two_days_are_asked_once(self):
        asked = []

        def model(pairs):
            asked.append(sorted(tuple(sorted((a["id"], b["id"]))) for a, b in pairs))
            return {number: True for number in range(1, len(pairs) + 1)}

        with patch.object(hot_pairs, "_ask", side_effect=model):
            self.assertEqual(hot_pairs.refresh()["same"], 1)
            self.assertEqual(hot_pairs.refresh(), {"asked": 0, "same": 0})  # never asked twice
        self.assertEqual(asked, [[("s1", "s3")]])  # s4 is 65 hours from s1; s2 and s5 share no company
        with database.get_db() as db:
            self.assertEqual(hot_pairs.same_event_groups(db, ["s1", "s2", "s3"]),
                             {"s1": "s1", "s2": "s2", "s3": "s1"})

    def test_an_unreadable_answer_is_asked_again_next_time(self):
        with patch.object(hot_pairs, "_ask", side_effect=ValueError("not json")):
            self.assertTrue(hot_pairs.refresh()["unreadable"])
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM story_pair_judgements").fetchone()[0], 0)

    def test_a_model_without_balance_skips_the_run_instead_of_failing_the_job(self):
        class QuotaError(Exception):
            def __str__(self):
                return "Error code: 429 - {'error': {'code': '1113', 'message': '余额不足'}}"

        with patch.object(hot_pairs, "_ask", side_effect=QuotaError()), self.assertLogs("app.hot_pairs", "WARNING"):
            self.assertEqual(hot_pairs.refresh()["model_error"], "quota")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM story_pair_judgements").fetchone()[0], 0)

    def test_the_model_answer_is_read_strictly(self):
        answer = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='[{"pair": 1, "same": true}, {"pair": 2, "same": "yes"}, {"pair": 9, "same": true}]'))])
        with patch("openai.OpenAI") as client, patch("app.ai.usage.call", side_effect=lambda task, create, items=0: answer):
            pairs = [({"title": "a", "first_at": at(1)}, {"title": "b", "first_at": at(1)})] * 2
            self.assertEqual(hot_pairs._ask(pairs), {1: True})
        client.assert_called_once()

    def test_the_hot_list_shows_one_event_once(self):
        with database.get_db() as db:
            db.execute("""INSERT INTO story_pair_judgements(story_a,story_b,same,judged_at,model)
                          VALUES('s1','s3',1,'t','m'),('s2','s5',0,'t','m')""")
        clusters = routes._top_clusters(4)
        self.assertEqual([c["id"] for c in clusters], ["s1", "s2", "s4", "s5"])
        self.assertEqual([r["id"] for r in clusters[0]["related"]], ["s3"])
        page = TestClient(routes.app).get("/hot").text
        self.assertIn("同一件事：", page)
        self.assertIn("6 篇报道", page)  # 3 + 3 reports of the one event

    def test_the_worker_schedules_it_unless_turned_off(self):
        def schedule():
            with database.get_db() as db:
                return db.execute("SELECT enabled FROM schedules WHERE id='hot-pairs:refresh'").fetchone()
        worker.register_default_schedules()
        self.assertEqual(schedule()["enabled"], 1)
        with patch.object(config, "HOT_PAIRS_ENABLED", False):
            worker.register_default_schedules()
            self.assertEqual(worker.default_handlers()["hot-pairs"](), {"skipped": "disabled"})
        self.assertEqual(schedule()["enabled"], 0)


if __name__ == "__main__":
    unittest.main()


class FeedFoldTests(unittest.TestCase):
    def test_the_feed_shows_one_card_per_judged_event(self):
        items = [dict(id=1, cluster_id="s3", title="Meta's Joel Kaplan to step down next year"),
                 dict(id=2, cluster_id="s2", title="Anthropic in talks with Nscale"),
                 dict(id=3, cluster_id="s1", title="Joel Kaplan leaving Meta after 15 years"),
                 dict(id=4, cluster_id=None, title="No story yet")]
        with patch.object(routes, "same_event_groups", return_value={"s3": "s3", "s2": "s2", "s1": "s3"}), \
                patch.object(routes, "get_db"):
            folded = routes._fold_same_events(items)
        self.assertEqual([it["id"] for it in folded], [1, 2, 4])  # the newest card of the event stays
        self.assertEqual(folded[0]["same_event"], [{"id": "s1", "title": "Joel Kaplan leaving Meta after 15 years"}])
