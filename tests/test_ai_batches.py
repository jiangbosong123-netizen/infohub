import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.ai import pipeline


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


if __name__ == "__main__":
    unittest.main()
