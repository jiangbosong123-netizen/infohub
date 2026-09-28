import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation import validate_tone_evaluation_dataset
from app.tone_review_intake import import_tone_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneReviewIntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "tone-private"
        shutil.copytree(FIXTURE, self.dataset)
        cases = [json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()]
        original_labels = cases[0]["annotation"]["labels"]
        original_labels["tone"]["evidence"][0]["quote"] = None
        cases[0]["annotation"] = {
            "state": "unlabeled",
            "generated_by_model": False,
            "labels": {},
        }
        cases[0]["text_storage"] = "restricted_reference"
        cases[0]["object_ref"] = "private-db:document-version/1"
        cases[0].pop("text")
        (self.dataset / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
            encoding="utf-8",
        )
        self.case = cases[0]
        self.labels = original_labels
        validate_tone_evaluation_dataset(self.dataset)

    def batch(
        self,
        source: Path,
        reviewer: str,
        *,
        labels=None,
        digest=None,
        model_assistance=False,
        row_extra=None,
    ) -> Path:
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
                    "model_assistance": model_assistance,
                }
            ),
            encoding="utf-8",
        )
        row = {
            "case_id": self.case["case_id"],
            "content_sha256": digest or self.case["content_sha256"],
            "recorded_at": "2026-09-28T12:00:00Z",
            "labels": labels if labels is not None else self.labels,
        }
        if row_extra:
            row.update(row_extra)
        (batch / "reviews.jsonl").write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return batch

    def test_two_independent_reviews_remain_provisional_and_immutable(self):
        first_batch = self.batch(self.dataset, "tone-reviewer-a")
        first = self.root / "review-one"
        result = import_tone_review_batch(
            self.dataset, first_batch, first, dataset_version="tone-review-v1"
        )
        self.assertEqual((result.one_review_cases, result.two_review_cases), (1, 0))
        self.assertFalse(result.publishable_tone_gold)
        first_case = json.loads((first / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(first_case["annotation"]["labels"], {})
        self.assertEqual(first_case["annotation"]["state"], "single_annotator")

        second_batch = self.batch(first, "tone-reviewer-b")
        second = self.root / "review-two"
        result = import_tone_review_batch(
            first, second_batch, second, dataset_version="tone-review-v2"
        )
        self.assertEqual((result.one_review_cases, result.two_review_cases), (0, 1))
        self.assertFalse(result.publishable_tone_gold)
        second_case = json.loads((second / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(second_case["annotation"]["labels"], {})
        self.assertEqual(
            [review["reviewer_id"] for review in second_case["annotation"]["reviews"]],
            ["tone-reviewer-a", "tone-reviewer-b"],
        )
        self.assertEqual(len(first_case["annotation"]["reviews"]), 1)
        with self.assertRaisesRegex(EvaluationDatasetError, "new immutable directory"):
            import_tone_review_batch(
                first, second_batch, second, dataset_version="tone-review-v2"
            )

    def test_stale_content_and_model_assistance_leave_no_output(self):
        stale = self.batch(self.dataset, "stale-reviewer", digest="0" * 64)
        stale_output = self.root / "stale-output"
        with self.assertRaisesRegex(EvaluationDatasetError, "content hash differs"):
            import_tone_review_batch(
                self.dataset, stale, stale_output, dataset_version="tone-stale-v1"
            )
        self.assertFalse(stale_output.exists())

        assisted = self.batch(
            self.dataset, "assisted-reviewer", model_assistance=True
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "no-model attestation"):
            import_tone_review_batch(
                self.dataset,
                assisted,
                self.root / "assisted-output",
                dataset_version="tone-assisted-v1",
            )

    def test_same_reviewer_and_invalid_tone_label_are_rejected(self):
        first_batch = self.batch(self.dataset, "same-reviewer")
        first = self.root / "review-one"
        import_tone_review_batch(
            self.dataset, first_batch, first, dataset_version="tone-review-v1"
        )
        repeat = self.batch(first, "same-reviewer")
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct reviewer"):
            import_tone_review_batch(
                first, repeat, self.root / "repeat", dataset_version="tone-review-v2"
            )

        bad_labels = json.loads(json.dumps(self.labels))
        bad_labels["tone"]["polarity"] = "bullish"
        invalid = self.batch(first, "new-reviewer", labels=bad_labels)
        with self.assertRaisesRegex(EvaluationDatasetError, "unsupported polarity"):
            import_tone_review_batch(
                first, invalid, self.root / "invalid", dataset_version="tone-review-v2"
            )

    def test_batch_is_bound_to_exact_source_dataset(self):
        batch = self.batch(self.dataset, "bound-reviewer")
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["source_cases_sha256"] = "0" * 64
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        output = self.root / "wrong-source"
        with self.assertRaisesRegex(EvaluationDatasetError, "does not match frozen source"):
            import_tone_review_batch(
                self.dataset, batch, output, dataset_version="tone-review-v1"
            )
        self.assertFalse(output.exists())

    def test_review_rows_are_closed_and_restricted_text_cannot_leak_quote(self):
        extra = self.batch(
            self.dataset, "extra-reviewer", row_extra={"comment": "hidden side channel"}
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid fields"):
            import_tone_review_batch(
                self.dataset, extra, self.root / "extra", dataset_version="tone-extra-v1"
            )

        leaking_labels = json.loads(json.dumps(self.labels))
        leaking_labels["tone"]["evidence"][0]["quote"] = "outlook is strong"
        leak = self.batch(self.dataset, "leak-reviewer", labels=leaking_labels)
        with self.assertRaisesRegex(EvaluationDatasetError, "must not embed a quote"):
            import_tone_review_batch(
                self.dataset, leak, self.root / "leak", dataset_version="tone-leak-v1"
            )

    def test_synthetic_embedded_fixture_cannot_enter_human_intake(self):
        row = json.loads((FIXTURE / "cases.jsonl").read_text().splitlines()[0])
        labels = row["annotation"]["labels"]
        batch = self.batch(FIXTURE, "fixture-reviewer", labels=labels)
        with self.assertRaisesRegex(EvaluationDatasetError, "restricted real-data reference"):
            import_tone_review_batch(
                FIXTURE, batch, self.root / "synthetic", dataset_version="tone-synthetic-v2"
            )

    def test_prior_evidence_signature_is_not_carried_across_new_labels(self):
        manifest = json.loads((self.dataset / "manifest.json").read_text())
        manifest["tone_evidence_review"] = {"status": "stale-placeholder"}
        (self.dataset / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        batch = self.batch(self.dataset, "evidence-reviewer")
        output = self.root / "reviewed"
        import_tone_review_batch(
            self.dataset, batch, output, dataset_version="tone-evidence-review-v1"
        )
        output_manifest = json.loads((output / "manifest.json").read_text())
        self.assertNotIn("tone_evidence_review", output_manifest)


if __name__ == "__main__":
    unittest.main()
