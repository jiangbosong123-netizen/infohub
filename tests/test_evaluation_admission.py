import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError, _verified_holdout, validate_evaluation_dataset
from app.evaluation_admission import admit_sampling_plan
from app.evaluation_sampling import build_sampling_plan


class EvaluationAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.plan = self.root / "plan"
        self.plan.mkdir()
        rows = []
        for number in range(1, 7):
            rows.append({
                "candidate_id": f"candidate-{number}",
                "document_ref": f"legacy-item:{number}",
                "object_ref": f"private-db:items/{number}",
                "content_sha256": hashlib.sha256(f"text {number}".encode()).hexdigest(),
                "event_group_ref": f"event-{number}",
                "origin_group_ref": f"origin-{number}",
                "source_ref": "fixture",
                "language": "en" if number % 2 else "zh",
                "annotation_state": "unlabeled",
            })
        # Transitive connection: same event, then same origin, then same content.
        rows[1]["event_group_ref"] = rows[0]["event_group_ref"]
        rows[2]["origin_group_ref"] = rows[1]["origin_group_ref"]
        rows[3]["content_sha256"] = rows[2]["content_sha256"]
        self.rows = rows
        self.write_plan()

    def write_plan(self):
        (self.plan / "manifest.json").write_text(json.dumps({
            "schema_version": "evaluation-sampling-plan-v1",
            "target_documents": len(self.rows),
            "database_snapshot_fingerprint": "fixture-snapshot",
        }), encoding="utf-8")
        (self.plan / "candidates.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")

    def test_freezes_unlabeled_cases_and_connected_groups(self):
        first = self.root / "first"
        second = self.root / "second"
        report = admit_sampling_plan(self.plan, first, dataset_version="private-v1")
        admit_sampling_plan(self.plan, second, dataset_version="private-v1")
        self.assertEqual((first / "cases.jsonl").read_bytes(), (second / "cases.jsonl").read_bytes())
        self.assertEqual(report.status, "unlabeled")
        self.assertEqual(report.components, 3)
        self.assertFalse(report.publishable_gold)
        cases = [json.loads(line) for line in (first / "cases.jsonl").read_text().splitlines()]
        self.assertEqual(len({row["split"] for row in cases[:4]}), 1)
        self.assertTrue(all(row["annotation"]["state"] == "unlabeled" for row in cases))
        self.assertTrue(all("text" not in row for row in cases))
        self.assertEqual(validate_evaluation_dataset(first).documents, 6)
        with self.assertRaisesRegex(EvaluationDatasetError, "immutable"):
            admit_sampling_plan(self.plan, first, dataset_version="private-v1")

    def test_candidate_with_restricted_content_is_rejected_before_output(self):
        self.rows[0]["title"] = "private title"
        self.write_plan()
        output = self.root / "bad"
        with self.assertRaisesRegex(EvaluationDatasetError, "restricted fields"):
            admit_sampling_plan(self.plan, output, dataset_version="private-v1")
        self.assertFalse(output.exists())

    def test_labeled_or_duplicate_candidate_is_rejected(self):
        self.rows[0]["annotation_state"] = "adjudicated"
        self.write_plan()
        with self.assertRaisesRegex(EvaluationDatasetError, "unlabeled"):
            admit_sampling_plan(self.plan, self.root / "bad", dataset_version="private-v1")
        self.rows[0]["annotation_state"] = "unlabeled"
        self.rows[1]["candidate_id"] = self.rows[0]["candidate_id"]
        self.write_plan()
        with self.assertRaisesRegex(EvaluationDatasetError, "duplicate candidate"):
            admit_sampling_plan(self.plan, self.root / "bad", dataset_version="private-v1")

    def test_source_database_is_verified_before_freezing(self):
        database = self.root / "source.db"
        db = sqlite3.connect(database)
        db.executescript("""
            CREATE TABLE sources(id INTEGER PRIMARY KEY,key TEXT,name TEXT,type TEXT,tier TEXT);
            CREATE TABLE items(id INTEGER PRIMARY KEY,source_id INTEGER,url TEXT,title TEXT,
              title_en TEXT,title_zh TEXT,summary TEXT,raw_summary TEXT,channel TEXT,
              event_type TEXT,official INTEGER,published_at TEXT);
            CREATE TABLE story_items(item_id INTEGER PRIMARY KEY,story_id TEXT);
            CREATE TABLE companies(id INTEGER PRIMARY KEY,slug TEXT,market TEXT,cik TEXT);
            CREATE TABLE item_companies(item_id INTEGER,company_id INTEGER);
            CREATE TABLE item_topics(item_id INTEGER,topic_slug TEXT);
            INSERT INTO sources VALUES(1,'fixture','Fixture','rss','media');
            INSERT INTO items VALUES(1,1,'https://example.test/a','Original title','','',
              'Original summary',NULL,'ai','update',0,'2026-09-20T00:00:00Z');
        """)
        db.commit()
        db.close()
        plan = self.root / "real-plan"
        build_sampling_plan(database, plan, target=1)
        admitted = admit_sampling_plan(plan, self.root / "verified",
                                       dataset_version="verified-v1", database=database)
        self.assertTrue(admitted.source_database_verified)
        db = sqlite3.connect(database)
        db.execute("UPDATE items SET title='Changed title' WHERE id=1")
        db.commit()
        db.close()
        with self.assertRaisesRegex(EvaluationDatasetError, "differs from sampling snapshot"):
            admit_sampling_plan(plan, self.root / "stale",
                                dataset_version="stale-v1", database=database)
        self.assertFalse((self.root / "stale").exists())

    def blind_rows(self):
        rows = []
        for number in range(1, 121):
            source = "source-c" if number > 110 else "source-b" if number > 104 else "source-a"
            rows.append({
                "candidate_id": f"candidate-{number}",
                "document_ref": f"legacy-item:{number}",
                "object_ref": f"private-db:items/{number}",
                "content_sha256": hashlib.sha256(f"text {number}".encode()).hexdigest(),
                "event_group_ref": f"event-{number}",
                "origin_group_ref": f"origin-{number}",
                "source_ref": source,
                "language": "en" if number % 2 else "zh",
                "published_at": "2026-09-21T10:00:00Z" if number > 110 else "2026-08-20T10:00:00Z",
                "annotation_state": "unlabeled",
            })
        return rows

    def test_balanced_split_follows_spec_ratio(self):
        self.rows = self.blind_rows()
        self.write_plan()
        report = admit_sampling_plan(self.plan, self.root / "balanced",
                                     dataset_version="balanced-v3")
        self.assertEqual(report.split_counts,
                         {"dev": 24, "security": 0, "test": 24, "train": 72})
        self.assertFalse(report.publishable_gold)

    def test_blind_split_reserves_recent_window_and_entire_source(self):
        self.rows = self.blind_rows()
        self.rows[0]["event_group_ref"] = self.rows[-1]["event_group_ref"]
        self.write_plan()
        output = self.root / "blind"
        report = admit_sampling_plan(self.plan, output, dataset_version="blind-v1",
                                     split_policy="blind-holdout")
        self.assertEqual(report.holdout_status, "planned_unreviewed")
        self.assertEqual(report.split_counts["test"], 24)
        self.assertEqual(report.split_counts["dev"], 24)
        self.assertEqual(report.split_counts["train"], 72)
        self.assertFalse(report.publishable_gold)
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["admission_version"], "evaluation-admission-v3")
        self.assertEqual(manifest["split_target_ratios"], {"train": .60, "dev": .20, "test": .20})
        cases = [json.loads(line) for line in (output / "cases.jsonl").read_text().splitlines()]
        review = manifest["holdout_review"]
        self.assertEqual(review["status"], "pending")
        self.assertTrue(all(case["split"] == "test" for case in cases
                            if case["source_kind"] in review["heldout_source_refs"]
                            or case["published_at"] >= review["heldout_after"]))
        self.assertEqual(cases[0]["split"], "test")
        self.assertFalse(_verified_holdout(manifest, cases))
        review.update(status="verified", reviewer_id="reviewer-a",
                      recorded_at="2026-09-21T12:00:00Z")
        # A hand-edited status flag is no longer a valid review attestation.
        self.assertFalse(_verified_holdout(manifest, cases))

    def test_blind_split_is_deterministic(self):
        self.rows = self.blind_rows()
        self.write_plan()
        first = self.root / "first-blind"
        second = self.root / "second-blind"
        admit_sampling_plan(self.plan, first, dataset_version="blind-v1",
                            split_policy="blind-holdout")
        admit_sampling_plan(self.plan, second, dataset_version="blind-v1",
                            split_policy="blind-holdout")
        self.assertEqual((first / "cases.jsonl").read_bytes(),
                         (second / "cases.jsonl").read_bytes())
        self.assertEqual((first / "holdout-plan.json").read_bytes(),
                         (second / "holdout-plan.json").read_bytes())

    def test_blind_split_fails_when_no_source_can_be_held_out(self):
        self.rows = self.blind_rows()
        for row in self.rows:
            row["source_ref"] = "only-source"
        self.write_plan()
        with self.assertRaisesRegex(EvaluationDatasetError, "no source fits"):
            admit_sampling_plan(self.plan, self.root / "bad-blind",
                                dataset_version="blind-v1", split_policy="blind-holdout")

    def test_blind_split_fails_when_latest_day_exceeds_budget(self):
        self.rows = self.blind_rows()
        for row in self.rows:
            row["published_at"] = "2026-09-21T10:00:00Z"
        self.write_plan()
        with self.assertRaisesRegex(EvaluationDatasetError, "no bounded recent window"):
            admit_sampling_plan(self.plan, self.root / "bad-recent",
                                dataset_version="blind-v1", split_policy="blind-holdout")


if __name__ == "__main__":
    unittest.main()
