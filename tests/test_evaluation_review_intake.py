import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError, validate_evaluation_dataset
from app.evaluation_review_intake import import_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/foundation-v1"


class EvaluationReviewIntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "synthetic-dataset"
        shutil.copytree(FIXTURE, self.dataset)
        cases = [json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()]
        cases[0]["annotation"] = {"state": "unlabeled", "generated_by_model": False, "labels": {}}
        cases[0]["text_storage"] = "restricted_reference"
        cases[0]["object_ref"] = "private-db:synthetic-fixture/1"
        cases[0].pop("text")
        (self.dataset / "cases.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
        self.case = cases[0]

    def batch(self, source: Path, reviewer: str, *, label="unknown", digest=None,
              model_assistance=False) -> Path:
        batch = self.root / f"batch-{source.name}-{reviewer}"
        batch.mkdir()
        (batch / "manifest.json").write_text(json.dumps({
            "schema_version": "human-relevance-review-batch-v1",
            "source_dataset_version": validate_evaluation_dataset(source).dataset_version,
            "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((source / "cases.jsonl").read_bytes()).hexdigest(),
            "reviewer_id": reviewer, "source": "human", "independent": True,
            "model_assistance": model_assistance,
        }), encoding="utf-8")
        (batch / "reviews.jsonl").write_text(json.dumps({
            "case_id": self.case["case_id"],
            "content_sha256": digest or self.case["content_sha256"],
            "recorded_at": "2026-09-21T12:00:00Z",
            "labels": {"relevance": label},
        }) + "\n", encoding="utf-8")
        return batch

    def test_two_distinct_reviews_remain_provisional_and_immutable(self):
        first_batch = self.batch(self.dataset, "fixture-reviewer-a", label="relevant")
        first = self.root / "first"
        result = import_review_batch(self.dataset, first_batch, first, dataset_version="fixture-review-1")
        self.assertEqual((result.one_review_cases, result.two_review_cases), (1, 0))
        self.assertFalse(result.publishable_gold)
        first_case = json.loads((first / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(first_case["annotation"]["labels"], {})
        self.assertEqual(first_case["annotation"]["state"], "single_annotator")
        second_batch = self.batch(first, "fixture-reviewer-b", label="not_relevant")
        second = self.root / "second"
        result = import_review_batch(first, second_batch, second, dataset_version="fixture-review-2")
        self.assertEqual((result.one_review_cases, result.two_review_cases), (0, 1))
        self.assertFalse(result.publishable_gold)
        second_case = json.loads((second / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(second_case["annotation"]["labels"], {})
        self.assertEqual([r["reviewer_id"] for r in second_case["annotation"]["reviews"]],
                         ["fixture-reviewer-a", "fixture-reviewer-b"])
        self.assertEqual(len(first_case["annotation"]["reviews"]), 1)
        second_rows = [json.loads(line) for line in (second / "cases.jsonl").read_text().splitlines()]
        second_rows[0]["annotation"]["reviews"][1]["content_sha256"] = "0" * 64
        (second / "cases.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in second_rows), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationDatasetError, "frozen-content labels"):
            validate_evaluation_dataset(second)
        with self.assertRaisesRegex(EvaluationDatasetError, "already exists"):
            import_review_batch(first, second_batch, second, dataset_version="fixture-review-2")

    def test_stale_content_and_model_assistance_fail_without_output(self):
        bad_hash = self.batch(self.dataset, "bad-hash", digest="0" * 64)
        with self.assertRaisesRegex(EvaluationDatasetError, "content hash differs"):
            import_review_batch(self.dataset, bad_hash, self.root / "bad-output", dataset_version="bad-v1")
        self.assertFalse((self.root / "bad-output").exists())
        assisted = self.batch(self.dataset, "assisted", model_assistance=True)
        with self.assertRaisesRegex(EvaluationDatasetError, "no-model attestation"):
            import_review_batch(self.dataset, assisted, self.root / "assisted-output",
                                dataset_version="bad-v2")

    def test_same_reviewer_and_invalid_label_are_rejected(self):
        first_batch = self.batch(self.dataset, "same-reviewer")
        first = self.root / "first"
        import_review_batch(self.dataset, first_batch, first, dataset_version="fixture-review-1")
        repeat_batch = self.batch(first, "same-reviewer")
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct reviewer"):
            import_review_batch(first, repeat_batch, self.root / "repeat",
                                dataset_version="fixture-review-2")
        invalid = self.batch(first, "invalid-reviewer", label="neutral")
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid relevance label"):
            import_review_batch(first, invalid, self.root / "invalid",
                                dataset_version="fixture-review-2")

    def test_batch_is_bound_to_exact_source_version(self):
        batch = self.batch(self.dataset, "fixture-reviewer")
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["source_cases_sha256"] = "0" * 64
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationDatasetError, "does not match frozen source"):
            import_review_batch(self.dataset, batch, self.root / "wrong-source",
                                dataset_version="fixture-review-1")
        self.assertFalse((self.root / "wrong-source").exists())

    def test_synthetic_embedded_case_cannot_enter_review_intake(self):
        row = json.loads((FIXTURE / "cases.jsonl").read_text().splitlines()[0])
        batch = self.batch(FIXTURE, "fixture-reviewer", digest=row["content_sha256"])
        with self.assertRaisesRegex(EvaluationDatasetError, "restricted real-data reference"):
            import_review_batch(FIXTURE, batch, self.root / "synthetic-review",
                                dataset_version="synthetic-review-v1")


if __name__ == "__main__":
    unittest.main()
