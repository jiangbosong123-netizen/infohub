import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.evaluation import EvaluationDatasetError, owner_recheck_sample, validate_evaluation_dataset
from app.evaluation_admission import admit_sampling_plan
from app.evaluation_sampling import build_sampling_plan
from app.legacy_relevance_run import build_legacy_tmt_run
from app.owner_label_console import OwnerConsoleError, OwnerLabelSession, create_owner_label_console
from app.owner_label_intake import import_owner_label_batch, import_owner_recheck_batch
from app.owner_recheck import owner_recheck_report

FIRST_LABELED_AT = "2026-09-01T00:00:00Z"


class OwnerRecheckTests(unittest.TestCase):
    """The D23 recheck replaces a second annotator with a delayed blind relabel by the owner."""

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
            INSERT INTO sources VALUES(1,'wire','Wire','rss','media');
        """)
        db.executemany(
            "INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(
                item_id, 1, f"https://news.test/{item_id}",
                f"Headline {item_id}" if item_id % 2 else f"标题 {item_id}", "", "",
                f"excerpt {item_id}", f"excerpt {item_id}", "ai", "", 0,
                f"2026-08-{1 + item_id % 28:02d}T08:00:00Z", int(item_id % 3 != 0), "[]",
            ) for item_id in range(1, 201)],
        )
        db.commit()
        db.close()
        build_sampling_plan(self.database, self.root / "plan", target=200, seed="recheck")
        unlabeled = self.root / "unlabeled"
        admit_sampling_plan(self.root / "plan", unlabeled, dataset_version="recheck-unlabeled-v1",
                            database=self.database)
        cases = self.cases(unlabeled)
        self.truth = {
            case["case_id"]: "not_relevant" if int(case["object_ref"].rsplit("/", 1)[1]) % 4 == 0
            else "relevant"
            for case in cases
        }
        batch = self.owner_batch(unlabeled, "owner-label-batch-v1", self.truth, FIRST_LABELED_AT)
        self.dataset = self.root / "owner-v1"
        import_owner_label_batch("relevance", unlabeled, batch, self.dataset,
                                 dataset_version="recheck-owner-v1")
        self.sample = owner_recheck_sample(self.cases(self.dataset))
        self.assertEqual(len(self.sample), 30)

    @staticmethod
    def cases(dataset: Path) -> list[dict]:
        return [json.loads(line) for line in (dataset / "cases.jsonl").read_text().splitlines()]

    def owner_batch(self, source: Path, schema: str, labels: dict[str, str], recorded_at: str,
                    *, owner: str = "owner") -> Path:
        batch = self.root / f"batch-{source.name}-{schema}-{len(list(self.root.iterdir()))}"
        batch.mkdir()
        digests = {case["case_id"]: case["content_sha256"] for case in self.cases(source)}
        (batch / "manifest.json").write_text(json.dumps({
            "schema_version": schema, "task": "relevance",
            "label_definition": "relevance-definition-v1",
            "source_dataset_version": validate_evaluation_dataset(source).dataset_version,
            "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((source / "cases.jsonl").read_bytes()).hexdigest(),
            "owner_id": owner, "source": "human", "blind": True, "model_assistance": False,
        }), encoding="utf-8")
        (batch / "reviews.jsonl").write_text("".join(
            json.dumps({"case_id": case_id, "content_sha256": digests[case_id],
                        "recorded_at": recorded_at, "labels": {"relevance": label}}) + "\n"
            for case_id, label in labels.items()
        ), encoding="utf-8")
        return batch

    def recheck_via_console(self, flip: set[str] = frozenset()) -> Path:
        session = OwnerLabelSession(self.dataset, self.database, self.root / "drafts",
                                    owner_id="owner", mode="recheck")
        self.assertEqual(session.order, self.sample)
        for case_id in session.order:
            label = self.truth[case_id]
            if case_id in flip:
                label = "unknown"
            session.record(case_id, label, session.by_id[case_id]["content_sha256"])
        batch = self.root / "recheck-batch"
        session.export(batch)
        self.assertEqual(json.loads((batch / "manifest.json").read_text())["schema_version"],
                         "owner-recheck-batch-v1")
        output = self.root / "owner-v2"
        result = import_owner_recheck_batch("relevance", self.dataset, batch, output,
                                            dataset_version="recheck-owner-v2")
        self.assertEqual(result["rechecked_cases"], 30)
        return output

    def test_agreeing_recheck_completes_and_unblocks_experimental_claim(self):
        before = owner_recheck_report(self.dataset, "relevance")
        self.assertFalse(before.complete)
        self.assertEqual((before.sample_size, before.eligible_now, before.rechecked), (30, 30, 0))
        before_metrics = build_legacy_tmt_run(self.dataset, self.database, self.root / "before-run")
        self.assertFalse(before_metrics["experimental_claim_allowed"])
        self.assertTrue(any("still need a recheck" in item for item in before_metrics["claim_blockers"]))

        rechecked = self.recheck_via_console()
        report = owner_recheck_report(rechecked, "relevance")
        self.assertEqual((report.rechecked, report.disagreements), (30, 0))
        self.assertAlmostEqual(report.agreement.cohen_kappa, 1.0)
        self.assertTrue(report.complete, report.blockers)
        # Final labels never change through a recheck.
        self.assertEqual(
            {case["case_id"]: case["annotation"]["labels"]["relevance"] for case in self.cases(rechecked)},
            self.truth,
        )
        metrics = build_legacy_tmt_run(rechecked, self.database, self.root / "after-run")
        self.assertTrue(metrics["experimental_claim_allowed"], metrics["claim_blockers"])
        self.assertFalse(metrics["quality_claim_allowed"])
        self.assertEqual(metrics["annotation_tier"], "owner")

    def test_disagreements_keep_claims_blocked_until_resolved(self):
        rechecked = self.recheck_via_console(flip={self.sample[0], self.sample[1]})
        report = owner_recheck_report(rechecked, "relevance")
        self.assertEqual(report.disagreements, 2)
        self.assertFalse(report.complete)
        self.assertTrue(any("await an owner resolution" in item for item in report.blockers))
        metrics = build_legacy_tmt_run(rechecked, self.database, self.root / "run")
        self.assertFalse(metrics["experimental_claim_allowed"])

    def test_recheck_must_be_late_in_sample_and_by_the_owner(self):
        sampled, outside = self.sample[0], next(c for c in self.truth if c not in self.sample)
        for name, labels, recorded_at, owner, message in (
            ("early", {sampled: "relevant"}, "2026-09-03T00:00:00Z", "owner", "less than 7 days"),
            ("outside", {outside: "relevant"}, "2026-09-20T00:00:00Z", "owner", "not in the recheck sample"),
            ("other-owner", {sampled: "relevant"}, "2026-09-20T00:00:00Z", "owner-2", "dataset's owner"),
        ):
            with self.subTest(name):
                batch = self.owner_batch(self.dataset, "owner-recheck-batch-v1", labels, recorded_at,
                                         owner=owner)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    import_owner_recheck_batch("relevance", self.dataset, batch, self.root / name,
                                               dataset_version=f"recheck-{name}")
                self.assertFalse((self.root / name).exists())

    def test_validator_rejects_hand_made_early_or_out_of_sample_rechecks(self):
        rechecked = self.recheck_via_console()
        cases = self.cases(rechecked)
        target = next(case for case in cases if case["case_id"] == self.sample[0])
        target["annotation"]["owner_recheck"]["recorded_at"] = "2026-09-02T00:00:00Z"
        (rechecked / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
        with self.assertRaisesRegex(EvaluationDatasetError, "at least 7 days"):
            validate_evaluation_dataset(rechecked)
        target["annotation"]["owner_recheck"]["recorded_at"] = "2026-09-20T00:00:00Z"
        outside = next(case for case in cases if case["case_id"] not in self.sample)
        outside["annotation"]["owner_recheck"] = dict(target["annotation"]["owner_recheck"],
                                                      content_sha256=outside["content_sha256"])
        (rechecked / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
        with self.assertRaisesRegex(EvaluationDatasetError, "not in the owner-recheck-v1 sample"):
            validate_evaluation_dataset(rechecked)

    def test_console_offers_only_due_sample_cases_without_first_labels(self):
        session = OwnerLabelSession(self.dataset, self.database, self.root / "drafts",
                                    owner_id="owner", mode="recheck")
        self.assertEqual(set(session.by_id), set(self.sample))
        page = TestClient(create_owner_label_console(session, csrf_token="t")).get(
            f"/case/{self.sample[0]}").text
        self.assertIn("延时自复核", page)
        self.assertNotIn("当前标签", page)
        # First labels made just now are not due for a week.
        unlabeled = self.root / "unlabeled"
        fresh = self.root / "fresh"
        import_owner_label_batch(
            "relevance", unlabeled,
            self.owner_batch(unlabeled, "owner-label-batch-v1", self.truth, "2099-01-01T00:00:00Z"),
            fresh, dataset_version="recheck-fresh-v1",
        )
        with self.assertRaisesRegex(OwnerConsoleError, "no sampled case is due"):
            OwnerLabelSession(fresh, self.database, self.root / "drafts", owner_id="owner",
                              mode="recheck")


if __name__ == "__main__":
    unittest.main()
