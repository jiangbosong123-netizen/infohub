import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation import validate_tone_evaluation_dataset
from app.tone_review_agreement import measure_tone_agreement
from app.tone_review_intake import import_tone_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneReviewAgreementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / "tone-private"
        shutil.copytree(FIXTURE, self.dataset)
        cases = [json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()]
        self.labels = {}
        for case in cases[:2]:
            labels = case["annotation"]["labels"]
            labels["tone"]["evidence"][0]["quote"] = None
            self.labels[case["case_id"]] = labels
            case["annotation"] = {
                "state": "unlabeled",
                "generated_by_model": False,
                "labels": {},
            }
            case["text_storage"] = "restricted_reference"
            case["object_ref"] = f"private-db:{case['document_ref']}"
            case.pop("text")
        (self.dataset / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
            encoding="utf-8",
        )
        self.cases = {case["case_id"]: case for case in cases}

    def batch(self, source: Path, reviewer: str, labels: dict[str, dict]) -> Path:
        batch = self.root / f"batch-{source.name}-{reviewer}"
        batch.mkdir()
        report = validate_tone_evaluation_dataset(source)
        (batch / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "human-tone-review-batch-v1",
                    "source_dataset_version": report.dataset_version,
                    "source_manifest_sha256": hashlib.sha256(
                        (source / "manifest.json").read_bytes()
                    ).hexdigest(),
                    "source_cases_sha256": hashlib.sha256(
                        (source / "cases.jsonl").read_bytes()
                    ).hexdigest(),
                    "reviewer_id": reviewer,
                    "source": "human",
                    "independent": True,
                    "model_assistance": False,
                }
            ),
            encoding="utf-8",
        )
        rows = []
        for case_id, value in labels.items():
            rows.append(
                {
                    "case_id": case_id,
                    "content_sha256": self.cases[case_id]["content_sha256"],
                    "recorded_at": "2026-09-28T12:00:00Z",
                    "labels": value,
                }
            )
        (batch / "reviews.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return batch

    def reviewed_dataset(self) -> Path:
        first_batch = self.batch(self.dataset, "reviewer-a", self.labels)
        first = self.root / "review-one"
        import_tone_review_batch(
            self.dataset, first_batch, first, dataset_version="tone-review-v1"
        )
        second_labels = json.loads(json.dumps(self.labels))
        second_case_id = list(second_labels)[1]
        second_labels[second_case_id]["tone"]["polarity"] = "neutral"
        second_labels[second_case_id]["tone"]["intensity_band"] = "zero"
        second_labels[second_case_id]["tone"]["evidence"][0]["start_offset"] += 1
        second_labels[second_case_id]["tone"]["evidence"][0]["end_offset"] += 1
        second_batch = self.batch(first, "reviewer-b", second_labels)
        second = self.root / "review-two"
        import_tone_review_batch(
            first, second_batch, second, dataset_version="tone-review-v2"
        )
        return second

    def test_reports_polarity_kappa_and_each_contract_component(self):
        dataset = self.reviewed_dataset()
        report = measure_tone_agreement(
            dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-b"
        )
        self.assertEqual(report.paired_cases, 2)
        self.assertEqual(report.polarity_confusion["positive"]["positive"], 1)
        self.assertEqual(report.polarity_confusion["negative"]["neutral"], 1)
        self.assertEqual(report.polarity_observed_agreement, 0.5)
        self.assertAlmostEqual(report.polarity_expected_agreement, 0.25)
        self.assertAlmostEqual(report.polarity_cohen_kappa, 1 / 3)
        self.assertEqual(report.polarity_kappa_target, 0.70)
        self.assertFalse(report.polarity_kappa_passed)
        self.assertEqual(report.components["aspect"].matches, 2)
        self.assertEqual(report.components["polarity"].matches, 1)
        self.assertEqual(report.components["evidence"].matches, 1)
        self.assertEqual(report.components["full_label"].matches, 1)
        self.assertFalse(report.quality_gate_eligible)

    def test_scope_and_pair_are_explicit(self):
        dataset = self.reviewed_dataset()
        train = measure_tone_agreement(
            dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-b", split="train"
        )
        self.assertEqual(train.dataset_cases, 4)
        self.assertEqual(train.paired_cases, 2)
        missing = measure_tone_agreement(
            dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-c", split="test"
        )
        self.assertEqual(missing.paired_cases, 0)
        self.assertIsNone(missing.polarity_cohen_kappa)
        self.assertTrue(any("no cases" in warning for warning in missing.warnings))

    def test_same_reviewer_and_invalid_split_are_rejected(self):
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct reviewers"):
            measure_tone_agreement(
                self.dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-a"
            )
        with self.assertRaisesRegex(EvaluationDatasetError, "valid split"):
            measure_tone_agreement(
                self.dataset,
                reviewer_a="reviewer-a",
                reviewer_b="reviewer-b",
                split="future",
            )

    def test_one_class_agreement_does_not_claim_perfect_kappa(self):
        first_case = {next(iter(self.labels)): next(iter(self.labels.values()))}
        first_batch = self.batch(self.dataset, "reviewer-a", first_case)
        first = self.root / "one-a"
        import_tone_review_batch(
            self.dataset, first_batch, first, dataset_version="tone-one-v1"
        )
        second_batch = self.batch(first, "reviewer-b", first_case)
        second = self.root / "one-b"
        import_tone_review_batch(
            first, second_batch, second, dataset_version="tone-one-v2"
        )
        report = measure_tone_agreement(
            second, reviewer_a="reviewer-a", reviewer_b="reviewer-b"
        )
        self.assertEqual(report.polarity_observed_agreement, 1.0)
        self.assertIsNone(report.polarity_cohen_kappa)
        self.assertTrue(any("undefined" in warning for warning in report.warnings))


if __name__ == "__main__":
    unittest.main()
