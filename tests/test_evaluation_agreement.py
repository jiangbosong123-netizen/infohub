import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.evaluation_agreement import measure_relevance_agreement


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/foundation-v1"


class RelevanceAgreementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dataset = Path(temp.name) / "dataset"
        shutil.copytree(FIXTURE, self.dataset)

    def set_reviews(self, left, right, *, third_pair_at=None):
        rows = [json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()]
        for index, (a, b) in enumerate(zip(left, right)):
            case = rows[index]
            digest = case["content_sha256"]
            case.pop("text")
            case["text_storage"] = "restricted_reference"
            case["object_ref"] = f"private:synthetic/{index}"
            reviewer_b = "fixture-c" if index == third_pair_at else "fixture-b"
            case["annotation"] = {
                "state": "single_annotator", "generated_by_model": False, "labels": {},
                "reviews": [
                    {"reviewer_id": reviewer, "source": "human", "independent": True,
                     "content_sha256": digest, "recorded_at": "2026-09-21T12:00:00Z",
                     "labels": {"relevance": label}}
                    for reviewer, label in (("fixture-a", a), (reviewer_b, b))
                ],
            }
        (self.dataset / "cases.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def measure(self):
        return measure_relevance_agreement(
            self.dataset, reviewer_a="fixture-a", reviewer_b="fixture-b")

    def test_kappa_uses_named_pair_and_excludes_adjudicated_gold(self):
        self.set_reviews(
            ["relevant", "relevant", "not_relevant", "not_relevant"],
            ["relevant", "not_relevant", "not_relevant", "not_relevant"],
        )
        rows = [json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()]
        first = rows[0]
        first["annotation"]["state"] = "adjudicated"
        first["annotation"]["labels"] = {"relevance": "unknown"}
        first["annotation"]["adjudication"] = {
            "adjudicator_id": "fixture-c", "source": "human",
            "content_sha256": first["content_sha256"],
            "recorded_at": "2026-09-21T13:00:00Z", "labels": {"relevance": "unknown"},
        }
        (self.dataset / "cases.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = self.measure()
        self.assertEqual(report.paired_cases, 4)
        self.assertEqual(report.confusion["relevant"]["not_relevant"], 1)
        self.assertEqual(report.observed_agreement, 0.75)
        self.assertEqual(report.expected_agreement, 0.5)
        self.assertEqual(report.cohen_kappa, 0.5)
        self.assertFalse(report.paired_by_split.get("test"))
        self.assertIn("dataset is not publishable gold", " ".join(report.warnings))

    def test_different_review_pair_is_not_mixed_into_marginals(self):
        self.set_reviews(["relevant", "not_relevant"],
                         ["not_relevant", "not_relevant"], third_pair_at=1)
        report = self.measure()
        self.assertEqual(report.paired_cases, 1)
        self.assertEqual(report.confusion["relevant"]["not_relevant"], 1)
        self.assertEqual(report.cohen_kappa, 0)

    def test_split_scope_keeps_test_agreement_separate(self):
        self.set_reviews(["relevant"] * 9, ["relevant"] * 8 + ["not_relevant"])
        report = measure_relevance_agreement(
            self.dataset, reviewer_a="fixture-a", reviewer_b="fixture-b", split="test")
        self.assertEqual(report.scope_split, "test")
        self.assertEqual(report.dataset_cases, 3)
        self.assertEqual(report.paired_cases, 2)
        self.assertEqual(report.paired_by_split, {"test": 2})
        with self.assertRaisesRegex(EvaluationDatasetError, "valid split"):
            measure_relevance_agreement(
                self.dataset, reviewer_a="fixture-a", reviewer_b="fixture-b", split="all")

    def test_one_class_and_no_pairs_do_not_claim_perfect_kappa(self):
        self.set_reviews(["relevant", "relevant"], ["relevant", "relevant"])
        report = self.measure()
        self.assertEqual(report.observed_agreement, 1)
        self.assertIsNone(report.cohen_kappa)
        self.assertIn("undefined", " ".join(report.warnings))
        empty = measure_relevance_agreement(
            FIXTURE, reviewer_a="fixture-a", reviewer_b="fixture-b")
        self.assertEqual(empty.paired_cases, 0)
        self.assertIsNone(empty.cohen_kappa)

    def test_rejects_same_reviewer_or_invalid_label(self):
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct"):
            measure_relevance_agreement(
                FIXTURE, reviewer_a="fixture-a", reviewer_b="fixture-a")
        self.set_reviews(["irrelevant"], ["relevant"])
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid relevance"):
            self.measure()


if __name__ == "__main__":
    unittest.main()
