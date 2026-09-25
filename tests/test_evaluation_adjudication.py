import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError, validate_evaluation_dataset
from app.evaluation_adjudication import adjudicate_relevance
from app.evaluation_review_intake import import_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/foundation-v1"


class EvaluationAdjudicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "synthetic-source"
        shutil.copytree(FIXTURE, self.source)
        rows = [json.loads(line) for line in (self.source / "cases.jsonl").read_text().splitlines()]
        self.case_id, self.digest = rows[0]["case_id"], rows[0]["content_sha256"]
        rows[0]["text_storage"] = "restricted_reference"
        rows[0]["object_ref"] = "private-db:synthetic-fixture/1"
        rows[0].pop("text")
        rows[0]["annotation"] = {"state": "unlabeled", "generated_by_model": False, "labels": {}}
        (self.source / "cases.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def review_batch(self, source: Path, reviewer: str, label: str) -> Path:
        batch = self.root / f"batch-{source.name}-{reviewer}"
        batch.mkdir()
        (batch / "manifest.json").write_text(json.dumps({
            "schema_version": "human-relevance-review-batch-v1",
            "source_dataset_version": validate_evaluation_dataset(source).dataset_version,
            "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((source / "cases.jsonl").read_bytes()).hexdigest(),
            "reviewer_id": reviewer, "source": "human", "independent": True,
            "model_assistance": False,
        }), encoding="utf-8")
        (batch / "reviews.jsonl").write_text(json.dumps({
            "case_id": self.case_id, "content_sha256": self.digest,
            "recorded_at": "2026-09-21T12:00:00Z", "labels": {"relevance": label},
        }) + "\n", encoding="utf-8")
        return batch

    def two_reviews(self) -> Path:
        first = self.root / "first"
        import_review_batch(self.source, self.review_batch(self.source, "fixture-a", "relevant"),
                            first, dataset_version="fixture-review-1")
        second = self.root / "second"
        import_review_batch(first, self.review_batch(first, "fixture-b", "not_relevant"),
                            second, dataset_version="fixture-review-2")
        return second

    def decision_batch(self, source: Path, adjudicator: str = "fixture-c",
                       label: str = "unknown", recorded_at: str = "2026-09-21T13:00:00Z") -> Path:
        batch = self.root / f"decision-{source.name}-{adjudicator}-{label}"
        batch.mkdir()
        (batch / "manifest.json").write_text(json.dumps({
            "schema_version": "human-relevance-adjudication-batch-v1",
            "source_dataset_version": validate_evaluation_dataset(source).dataset_version,
            "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((source / "cases.jsonl").read_bytes()).hexdigest(),
            "adjudicator_id": adjudicator, "source": "human", "model_assistance": False,
        }), encoding="utf-8")
        (batch / "decisions.jsonl").write_text(json.dumps({
            "case_id": self.case_id, "content_sha256": self.digest,
            "recorded_at": recorded_at, "labels": {"relevance": label},
            "reason": "Synthetic fixture disagreement resolved for workflow testing.",
        }) + "\n", encoding="utf-8")
        return batch

    def test_third_person_decides_one_case_without_publishing_gold_dataset(self):
        source = self.two_reviews()
        batch = self.decision_batch(source)
        output = self.root / "adjudicated"
        result = adjudicate_relevance(source, batch, output, dataset_version="fixture-adjudicated-v1")
        self.assertEqual((result.adjudicated_in_batch, result.total_adjudicated), (1, 1))
        self.assertFalse(result.publishable_gold)
        first = json.loads((output / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(first["annotation"]["state"], "adjudicated")
        self.assertEqual(first["annotation"]["labels"], {"relevance": "unknown"})
        self.assertEqual(len(first["annotation"]["reviews"]), 2)
        self.assertEqual(first["annotation"]["adjudication"]["adjudicator_id"], "fixture-c")
        self.assertEqual(len(json.loads((source / "cases.jsonl").read_text().splitlines()[0])["annotation"]["reviews"]), 2)
        self.assertFalse(validate_evaluation_dataset(output).publishable_gold)

    def test_reviewer_cannot_adjudicate_and_decision_cannot_precede_reviews(self):
        source = self.two_reviews()
        same = self.decision_batch(source, adjudicator="fixture-a")
        with self.assertRaisesRegex(EvaluationDatasetError, "third person"):
            adjudicate_relevance(source, same, self.root / "same-person",
                                 dataset_version="same-person-v1")
        earlier = self.decision_batch(source, recorded_at="2026-09-21T11:00:00Z")
        with self.assertRaisesRegex(EvaluationDatasetError, "precedes a human review"):
            adjudicate_relevance(source, earlier, self.root / "too-early",
                                 dataset_version="too-early-v1")
        self.assertFalse((self.root / "too-early").exists())

    def test_requires_two_frozen_reviews_and_exact_batch_hashes(self):
        first = self.root / "first"
        import_review_batch(self.source, self.review_batch(self.source, "fixture-a", "relevant"),
                            first, dataset_version="fixture-review-1")
        one_review = self.decision_batch(first)
        with self.assertRaisesRegex(EvaluationDatasetError, "two independent human reviews"):
            adjudicate_relevance(first, one_review, self.root / "one-review",
                                 dataset_version="one-review-v1")
        source = self.root / "second"
        import_review_batch(first, self.review_batch(first, "fixture-b", "not_relevant"),
                            source, dataset_version="fixture-review-2")
        batch = self.decision_batch(source)
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["source_cases_sha256"] = "0" * 64
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationDatasetError, "does not match frozen dataset"):
            adjudicate_relevance(source, batch, self.root / "stale", dataset_version="stale-v1")


if __name__ == "__main__":
    unittest.main()
