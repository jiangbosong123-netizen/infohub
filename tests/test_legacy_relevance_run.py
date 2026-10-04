import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.evaluation_admission import admit_sampling_plan
from app.evaluation_metrics import evaluate_classification
from app.evaluation_sampling import build_sampling_plan
from app.legacy_relevance_run import build_legacy_tmt_run
from app.owner_label_console import OwnerLabelSession
from app.owner_label_intake import import_owner_label_batch


class LegacyRelevanceRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "app.db"
        db = sqlite3.connect(self.database)
        db.executescript("""
            CREATE TABLE sources(id INTEGER PRIMARY KEY,key TEXT,name TEXT,type TEXT,tier TEXT);
            CREATE TABLE items(
                id INTEGER PRIMARY KEY,source_id INTEGER,url TEXT,title TEXT,title_en TEXT,title_zh TEXT,
                summary TEXT,raw_summary TEXT,channel TEXT,event_type TEXT,official INTEGER,
                published_at TEXT,tmt INTEGER,companies TEXT
            );
            CREATE TABLE stories(id TEXT PRIMARY KEY);
            CREATE TABLE story_items(item_id INTEGER PRIMARY KEY,story_id TEXT);
            CREATE TABLE companies(id INTEGER PRIMARY KEY,slug TEXT,market TEXT,cik TEXT);
            CREATE TABLE item_companies(item_id INTEGER,company_id INTEGER);
            CREATE TABLE item_topics(item_id INTEGER,topic_slug TEXT);
            INSERT INTO sources VALUES(1,'wire','Wire','rss','media'),(2,'sec','SEC','sec','official');
        """)
        rows = []
        for item_id in range(1, 41):
            official = int(item_id % 10 == 0)
            companies = '["nvidia"]' if item_id % 7 == 0 else "[]"
            tmt = None if item_id % 13 == 0 else int(item_id % 3 != 0 or official or companies != "[]")
            rows.append((
                item_id, 2 if official else 1, f"https://news.test/{item_id}",
                f"Headline {item_id}" if item_id % 2 else f"标题 {item_id}", "", "",
                f"excerpt {item_id}", f"excerpt {item_id}", "ai", "", official,
                f"2026-09-{1 + item_id % 28:02d}T08:00:00Z", tmt, companies,
            ))
        db.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        db.close()
        build_sampling_plan(self.database, self.root / "plan", target=40, seed="run")
        unlabeled = self.root / "unlabeled"
        admit_sampling_plan(self.root / "plan", unlabeled, dataset_version="run-unlabeled-v1",
                            database=self.database)
        session = OwnerLabelSession(unlabeled, self.database, self.root / "drafts", owner_id="owner")
        self.truth = {}
        for case_id in session.order:
            item_id = int(session.by_id[case_id]["object_ref"].rsplit("/", 1)[1])
            label = "not_relevant" if item_id % 4 == 0 else "relevant"
            session.record(case_id, label, session.by_id[case_id]["content_sha256"])
            self.truth[case_id] = (item_id, label)
        session.export(self.root / "batch")
        self.dataset = self.root / "owner-v1"
        import_owner_label_batch("relevance", unlabeled, self.root / "batch", self.dataset,
                                 dataset_version="run-owner-v1")
        self.test_cases = [
            json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()
            if json.loads(line)["split"] == "test"
        ]
        self.assertTrue(self.test_cases)

    def expected(self) -> dict[str, list[tuple[str, str]]]:
        db = sqlite3.connect(self.database)
        slices: dict[str, list[tuple[str, str]]] = {}
        for case in self.test_cases:
            item_id, truth = self.truth[case["case_id"]]
            tmt, official, companies = db.execute(
                "SELECT tmt,official,companies FROM items WHERE id=?", (item_id,)).fetchone()
            if tmt is None:
                name, predicted = "unscored", "__unscored__"
            else:
                name = "policy_forced" if official or companies != "[]" else "llm_only"
                predicted = "relevant" if tmt else "not_relevant"
            slices.setdefault(name, []).append((truth, predicted))
        db.close()
        return slices

    def test_owner_tier_report_splits_llm_judgment_from_policy_forcing(self):
        # tmt is not part of the frozen content, so an unprocessed row can be simulated safely.
        db = sqlite3.connect(self.database)
        db.execute("UPDATE items SET tmt=NULL WHERE id=?", (self.truth[self.test_cases[0]["case_id"]][0],))
        db.commit()
        db.close()
        report = build_legacy_tmt_run(self.dataset, self.database, self.root / "tmt-run")
        expected = self.expected()
        self.assertEqual(set(expected), {"llm_only", "policy_forced", "unscored"})
        self.assertEqual(report["annotation_tier"], "owner")
        self.assertEqual(report["split"], "test")
        self.assertEqual(report["total"], len(self.test_cases))
        self.assertFalse(report["quality_claim_allowed"])
        self.assertFalse(report["experimental_claim_allowed"])
        self.assertTrue(any("owner recheck" in item for item in report["claim_blockers"]))
        self.assertEqual(set(report["slices"]), set(expected))
        for name, pairs in expected.items():
            self.assertEqual(report["slices"][name]["total"], len(pairs))
            self.assertEqual(report["slices"][name]["correct"], sum(a == p for a, p in pairs))
        self.assertEqual(report["abstained"], len(expected.get("unscored", [])))
        stored = json.loads((self.root / "tmt-run" / "report.json").read_text())
        normalized = json.loads(json.dumps(report["slices"]))
        self.assertEqual(stored["slices"], normalized)
        # The stored run re-evaluates to the same numbers from its own hash-bound files.
        again = evaluate_classification(self.dataset, self.root / "tmt-run" / "run.json")
        self.assertEqual(json.loads(json.dumps(again.to_dict()["slices"])), normalized)

    def test_predictions_must_come_from_the_judged_snapshot(self):
        db = sqlite3.connect(self.database)
        item_id = self.truth[self.test_cases[0]["case_id"]][0]
        db.execute("UPDATE items SET raw_summary='edited' WHERE id=?", (item_id,))
        db.commit()
        db.close()
        with self.assertRaisesRegex(EvaluationDatasetError, "content differs"):
            build_legacy_tmt_run(self.dataset, self.database, self.root / "stale-run")
        self.assertFalse((self.root / "stale-run").exists())

    def test_output_is_private_and_immutable(self):
        repository = Path(__file__).parents[1]
        with self.assertRaisesRegex(EvaluationDatasetError, "outside the repository"):
            build_legacy_tmt_run(self.dataset, self.database, repository / "evaluation" / "tmt-run")
        build_legacy_tmt_run(self.dataset, self.database, self.root / "run")
        with self.assertRaisesRegex(EvaluationDatasetError, "already exists"):
            build_legacy_tmt_run(self.dataset, self.database, self.root / "run")


if __name__ == "__main__":
    unittest.main()
