import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.ai import pipeline
from app.crawler import runner


class FilterError(Exception):
    def __str__(self):
        return "Error code: 400 - {'error': {'code': '1301', 'message': 'contentFilter'}}"


class AiBatchSplitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        item = patch.object(database, "DB_PATH", Path(folder.name) / "app.db")
        item.start()
        self.addCleanup(item.stop)
        database.init_schema()
        self.calls = []

    def model(self, filtered=(), garbled=(), down=False):
        def call(payload):
            ids = [entry["id"] for entry in payload]
            self.calls.append(ids)
            if down:
                raise ConnectionError("network down")
            if set(ids) & set(filtered):
                raise FilterError()
            if set(ids) & set(garbled):
                raise ValueError("Expecting property name enclosed in double quotes")
            return [{"id": item_id} for item_id in ids]
        return call

    def judge(self, ids, **model):
        with patch.object(pipeline, "_call_llm", side_effect=self.model(**model)):
            results, skipped = pipeline._judge([{"id": item_id} for item_id in ids])
        return sorted(result["id"] for result in results), skipped

    def test_a_clean_batch_is_one_call(self):
        self.assertEqual(self.judge(range(30)), (list(range(30)), []))
        self.assertEqual(len(self.calls), 1)

    def test_a_filtered_item_is_isolated_by_halving_not_by_thirty_calls(self):
        judged, skipped = self.judge(range(30), filtered={17})
        self.assertEqual((judged, skipped), ([n for n in range(30) if n != 17], [17]))
        self.assertLessEqual(len(self.calls), 11)  # about 2·log2(30), against 31 one by one

    def test_an_unreadable_answer_keeps_every_other_item(self):
        judged, skipped = self.judge(range(30), garbled={3})
        self.assertEqual((judged, skipped), ([n for n in range(30) if n != 3], []))  # 3 stays pending

    def test_a_network_failure_is_not_split(self):
        self.assertEqual(self.judge(range(30), down=True), ([], []))
        self.assertEqual(len(self.calls), 1)


class ParallelScoringTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        item = patch.object(database, "DB_PATH", Path(folder.name) / "app.db")
        item.start()
        self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','info')")
        for n in range(12):
            runner.insert_item("test", dict(url=f"https://e.test/{n}", title=f"Story {n}",
                                            published_at="2026-10-15T08:00:00+00:00"))
        enabled = patch.object(config, "llm_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    @staticmethod
    def verdicts(payload):
        return [dict(id=entry["id"], tmt=True, score=50, summary_zh="s", title_zh="t", reason="r")
                for entry in payload]

    def scored(self):
        with database.get_db() as db:
            return db.execute("SELECT COUNT(*) FROM items WHERE score IS NOT NULL").fetchone()[0]

    def test_parallel_batches_are_in_flight_together(self):
        together = threading.Barrier(3, timeout=5)  # breaks unless all three requests wait at once
        sizes = []

        def call(payload):
            sizes.append(len(payload))
            together.wait()
            return self.verdicts(payload)

        with patch.object(pipeline, "_call_llm", side_effect=call):
            self.assertEqual(pipeline.process_pending(limit=4, parallel=3), 12)
        self.assertEqual(sorted(sizes), [4, 4, 4])
        self.assertEqual(self.scored(), 12)

    def test_a_failed_request_leaves_only_its_own_batch_pending(self):
        def call(payload):
            if 1 in [entry["id"] for entry in payload]:
                raise ConnectionError("reset by peer")
            return self.verdicts(payload)

        with patch.object(pipeline, "_call_llm", side_effect=call):
            self.assertEqual(pipeline.process_pending(limit=4, parallel=3), 8)
        self.assertEqual(self.scored(), 8)

    def test_an_answer_naming_another_requests_item_is_ignored(self):
        def call(payload):
            ids = [entry["id"] for entry in payload]
            verdicts = [dict(id=item_id, tmt=True, score=50, summary_zh=f"summary of {item_id}", title_zh="t",
                             reason="r") for item_id in ids]
            if 1 in ids:  # the model copies the neighbouring chunk's id 11 instead of 1
                verdicts[ids.index(1)]["id"] = 11
            return verdicts

        with patch.object(pipeline, "_call_llm", side_effect=call):
            pipeline.process_pending(limit=4, parallel=3)
        with database.get_db() as db:
            rows = {row["id"]: (row["score"], row["summary"]) for row in db.execute("SELECT id,score,summary FROM items")}
        self.assertEqual(rows[11], (50, "summary of 11"))  # its own request's verdict
        self.assertIsNone(rows[1][0])  # unanswered, so it waits for the next round

    def test_routine_filings_take_a_fixed_judgement_without_the_model(self):
        with database.get_db() as db:
            db.execute("""UPDATE items SET official=1, event_type='insider', title='Chipworks Inc. · SEC 内部人持股变动',
                          extra=json_set(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END,'$.routine',json('true'))
                          WHERE id IN (1,2)""")
        sent = []

        def call(payload):
            sent.extend(entry["id"] for entry in payload)
            return self.verdicts(payload)

        with patch.object(pipeline, "_call_llm", side_effect=call):
            self.assertEqual(pipeline.process_pending(limit=30), 12)
        self.assertEqual(sorted(sent), list(range(3, 13)))
        with database.get_db() as db:
            row = dict(db.execute("SELECT score,tmt,title_zh,reason,event_type,extra FROM items WHERE id=1").fetchone())
        self.assertEqual((row["score"], row["tmt"], row["title_zh"], row["reason"], row["event_type"]),
                         (pipeline.ROUTINE_SCORE, 1, "Chipworks Inc. · SEC 内部人持股变动", "", "insider"))
        self.assertEqual(json.loads(row["extra"])["ai_rule"], "routine")

    def test_the_backfills_leave_unscored_items_to_scoring(self):
        with database.get_db() as db:
            db.execute("UPDATE items SET tmt=NULL")
            db.execute("UPDATE items SET score=40 WHERE id=3")
        sent = []

        def call(payload):
            sent.append([entry["id"] for entry in payload])
            return []

        with patch.object(pipeline, "_call_llm_tmt", side_effect=call), \
                patch.object(pipeline, "_call_llm_titles", side_effect=call):
            pipeline.backfill_tmt(max_batches=1)
            pipeline.backfill_titles(days=36500, max_batches=1)
        self.assertEqual(sent, [[3], [3]])


if __name__ == "__main__":
    unittest.main()
