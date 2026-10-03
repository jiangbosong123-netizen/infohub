import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.impact_evaluation import validate_impact_evaluation_dataset
from app.impact_review_intake import import_impact_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/impact-contract-v1"


class ImpactReviewIntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "impact-private"
        shutil.copytree(FIXTURE, self.dataset)
        cases = [
            json.loads(line)
            for line in (self.dataset / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        original_labels = cases[0]["annotation"]["labels"]
        for evidence in cases[0]["event_evidence"]:
            evidence["quote"] = None
        cases[0]["annotation"] = {
            "state": "unlabeled",
            "generated_by_model": False,
            "labels": {},
        }
        cases[0]["text_storage"] = "restricted_reference"
        cases[0]["object_ref"] = "private-db:document-version/1"
        cases[0].pop("text")
        (self.dataset / "cases.jsonl").write_text(
            "".join(
                json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
                for case in cases
            ),
            encoding="utf-8",
        )
        self.case = cases[0]
        self.labels = original_labels
        validate_impact_evaluation_dataset(self.dataset)

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
        report = validate_impact_evaluation_dataset(source)
        (batch / "manifest.json").write_text(
            json.dumps({
                "schema_version": "human-impact-review-batch-v1",
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
            }),
            encoding="utf-8",
        )
        row = {
            "case_id": self.case["case_id"],
            "content_sha256": digest or self.case["content_sha256"],
            "recorded_at": "2026-10-03T12:00:00Z",
            "labels": labels if labels is not None else self.labels,
        }
        if row_extra:
            row.update(row_extra)
        (batch / "reviews.jsonl").write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return batch

    def test_two_independent_reviews_remain_provisional_and_immutable(self):
        first_batch = self.batch(self.dataset, "impact-reviewer-a")
        first = self.root / "review-one"
        result = import_impact_review_batch(
            self.dataset, first_batch, first, dataset_version="impact-review-v1"
        )
        self.assertEqual((result.one_review_cases, result.two_review_cases), (1, 0))
        self.assertFalse(result.publishable_impact_gold)
        first_case = json.loads((first / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(first_case["annotation"]["labels"], {})
        self.assertEqual(first_case["annotation"]["state"], "single_annotator")

        second_batch = self.batch(first, "impact-reviewer-b")
        second = self.root / "review-two"
        result = import_impact_review_batch(
            first, second_batch, second, dataset_version="impact-review-v2"
        )
        self.assertEqual((result.one_review_cases, result.two_review_cases), (0, 1))
        self.assertFalse(result.publishable_impact_gold)
        second_case = json.loads((second / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(second_case["annotation"]["labels"], {})
        self.assertEqual(
            [review["reviewer_id"] for review in second_case["annotation"]["reviews"]],
            ["impact-reviewer-a", "impact-reviewer-b"],
        )
        self.assertEqual(len(first_case["annotation"]["reviews"]), 1)
        with self.assertRaisesRegex(EvaluationDatasetError, "new immutable directory"):
            import_impact_review_batch(
                first, second_batch, second, dataset_version="impact-review-v2"
            )

    def test_stale_content_and_model_assistance_leave_no_output(self):
        stale = self.batch(self.dataset, "stale-reviewer", digest="0" * 64)
        stale_output = self.root / "stale-output"
        with self.assertRaisesRegex(EvaluationDatasetError, "content hash differs"):
            import_impact_review_batch(
                self.dataset, stale, stale_output, dataset_version="impact-stale-v1"
            )
        self.assertFalse(stale_output.exists())

        assisted = self.batch(
            self.dataset, "assisted-reviewer", model_assistance=True
        )
        assisted_output = self.root / "assisted-output"
        with self.assertRaisesRegex(EvaluationDatasetError, "no-model attestation"):
            import_impact_review_batch(
                self.dataset,
                assisted,
                assisted_output,
                dataset_version="impact-assisted-v1",
            )
        self.assertFalse(assisted_output.exists())

    def test_same_reviewer_and_invalid_impact_label_are_rejected(self):
        first_batch = self.batch(self.dataset, "same-reviewer")
        first = self.root / "review-one"
        import_impact_review_batch(
            self.dataset, first_batch, first, dataset_version="impact-review-v1"
        )
        repeat = self.batch(first, "same-reviewer")
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct reviewer"):
            import_impact_review_batch(
                first, repeat, self.root / "repeat", dataset_version="impact-review-v2"
            )

        bad_labels = json.loads(json.dumps(self.labels))
        bad_labels["impact"][0]["aspect"] = "share_price"
        invalid = self.batch(first, "new-reviewer", labels=bad_labels)
        with self.assertRaisesRegex(EvaluationDatasetError, "unsupported aspect"):
            import_impact_review_batch(
                first, invalid, self.root / "invalid", dataset_version="impact-review-v2"
            )

    def test_batch_is_bound_to_exact_source_dataset(self):
        batch = self.batch(self.dataset, "bound-reviewer")
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["source_cases_sha256"] = "0" * 64
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        output = self.root / "wrong-source"
        with self.assertRaisesRegex(EvaluationDatasetError, "does not match frozen source"):
            import_impact_review_batch(
                self.dataset, batch, output, dataset_version="impact-review-v1"
            )
        self.assertFalse(output.exists())

        extra = self.batch(self.dataset, "manifest-extra-reviewer")
        extra_manifest = json.loads((extra / "manifest.json").read_text())
        extra_manifest["comment"] = "untracked side channel"
        (extra / "manifest.json").write_text(json.dumps(extra_manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationDatasetError, "manifest has invalid fields"):
            import_impact_review_batch(
                self.dataset, extra, self.root / "manifest-extra",
                dataset_version="impact-review-v1",
            )

    def test_review_rows_are_closed_and_restricted_text_cannot_leak_quote(self):
        extra = self.batch(
            self.dataset, "extra-reviewer", row_extra={"comment": "hidden side channel"}
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid fields"):
            import_impact_review_batch(
                self.dataset, extra, self.root / "extra", dataset_version="impact-extra-v1"
            )

        leaking_source = self.root / "leaking-source"
        shutil.copytree(self.dataset, leaking_source)
        rows = [
            json.loads(line)
            for line in (leaking_source / "cases.jsonl").read_text().splitlines()
        ]
        rows[0]["event_evidence"][0]["quote"] = "Acme plans to automate billing"
        (leaking_source / "cases.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "must not embed a quote"):
            validate_impact_evaluation_dataset(leaking_source)
        leak_batch = self.batch(self.dataset, "leak-reviewer")
        with self.assertRaisesRegex(EvaluationDatasetError, "must not embed a quote"):
            import_impact_review_batch(
                leaking_source, leak_batch, self.root / "leak",
                dataset_version="impact-leak-v1",
            )
        self.assertFalse((self.root / "leak").exists())

    def test_synthetic_embedded_fixture_cannot_enter_human_intake(self):
        row = json.loads((FIXTURE / "cases.jsonl").read_text().splitlines()[0])
        labels = row["annotation"]["labels"]
        batch = self.batch(FIXTURE, "fixture-reviewer", labels=labels)
        with self.assertRaisesRegex(EvaluationDatasetError, "restricted real-data reference"):
            import_impact_review_batch(
                FIXTURE,
                batch,
                self.root / "synthetic",
                dataset_version="impact-synthetic-v2",
            )

    def test_prior_evidence_signature_is_not_carried_across_new_labels(self):
        manifest = json.loads((self.dataset / "manifest.json").read_text())
        manifest["impact_evidence_review"] = {"status": "stale-placeholder"}
        (self.dataset / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        batch = self.batch(self.dataset, "evidence-reviewer")
        output = self.root / "reviewed"
        import_impact_review_batch(
            self.dataset, batch, output, dataset_version="impact-evidence-review-v1"
        )
        output_manifest = json.loads((output / "manifest.json").read_text())
        self.assertNotIn("impact_evidence_review", output_manifest)

    def test_output_records_hashes_of_the_exact_imported_bytes(self):
        batch = self.batch(self.dataset, "provenance-reviewer")
        output = self.root / "provenance"
        import_impact_review_batch(
            self.dataset, batch, output, dataset_version="impact-provenance-v1"
        )
        manifest = json.loads((output / "manifest.json").read_text())

        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(manifest["parent_dataset_version"], "impact-contract-v1")
        self.assertEqual(manifest["parent_manifest_sha256"], digest(self.dataset / "manifest.json"))
        self.assertEqual(manifest["parent_cases_sha256"], digest(self.dataset / "cases.jsonl"))
        self.assertEqual(
            manifest["impact_review_batch_manifest_sha256"], digest(batch / "manifest.json")
        )
        self.assertEqual(
            manifest["impact_review_batch_cases_sha256"], digest(batch / "reviews.jsonl")
        )
        self.assertEqual(
            manifest["impact_review_batch_schema_version"], "human-impact-review-batch-v1"
        )

    def test_third_reviewer_cannot_join_a_two_review_case(self):
        source = self.dataset
        for number, reviewer in enumerate(("reviewer-a", "reviewer-b"), 1):
            output = self.root / f"two-reviews-{number}"
            import_impact_review_batch(
                source, self.batch(source, reviewer), output,
                dataset_version=f"impact-two-v{number}",
            )
            source = output
        third = self.batch(source, "reviewer-c")
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct reviewer"):
            import_impact_review_batch(
                source, third, self.root / "three", dataset_version="impact-two-v3"
            )
        self.assertFalse((self.root / "three").exists())

    def test_batch_rows_must_be_unique_known_and_present(self):
        row = {
            "case_id": self.case["case_id"],
            "content_sha256": self.case["content_sha256"],
            "recorded_at": "2026-10-03T12:00:00Z",
            "labels": self.labels,
        }
        for name, rows, message in (
            ("duplicate", [row, row], "duplicate or unknown"),
            ("unknown", [{**row, "case_id": "impact-missing"}], "duplicate or unknown"),
            ("empty", [], "batch is empty"),
        ):
            with self.subTest(name):
                batch = self.batch(self.dataset, f"{name}-reviewer")
                (batch / "reviews.jsonl").write_text(
                    "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8"
                )
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    import_impact_review_batch(
                        self.dataset, batch, self.root / name,
                        dataset_version=f"impact-{name}-v1",
                    )
                self.assertFalse((self.root / name).exists())

    def test_output_requires_a_new_dataset_version(self):
        batch = self.batch(self.dataset, "version-reviewer")
        for version in ("impact-contract-v1", "../escape", ""):
            with self.subTest(version):
                with self.assertRaisesRegex(EvaluationDatasetError, "new dataset_version"):
                    import_impact_review_batch(
                        self.dataset, batch, self.root / "versioned",
                        dataset_version=version,
                    )
        self.assertFalse((self.root / "versioned").exists())

    def test_unverified_blind_holdout_rejects_labels_before_reading_batch(self):
        manifest = json.loads((self.dataset / "manifest.json").read_text())
        manifest["split_policy"] = "blind-holdout"
        (self.dataset / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        batch = self.batch(self.dataset, "holdout-reviewer")
        (batch / "manifest.json").write_text("not json", encoding="utf-8")
        output = self.root / "holdout"
        with self.assertRaisesRegex(EvaluationDatasetError, "verified human leakage review"):
            import_impact_review_batch(
                self.dataset, batch, output, dataset_version="impact-holdout-v1"
            )
        self.assertFalse(output.exists())

    def test_repository_paths_must_stay_under_private_evaluation(self):
        repository = Path(__file__).parents[1]
        batch = self.batch(self.dataset, "path-reviewer")
        with self.assertRaisesRegex(EvaluationDatasetError, "under evaluation/private"):
            import_impact_review_batch(
                self.dataset, batch, repository / "evaluation" / "impact-public-output",
                dataset_version="impact-path-v1",
            )
        with self.assertRaisesRegex(EvaluationDatasetError, "batch must be private"):
            import_impact_review_batch(
                self.dataset, repository / "evaluation" / "datasets", self.root / "path",
                dataset_version="impact-path-v1",
            )
        self.assertFalse((repository / "evaluation" / "impact-public-output").exists())
        self.assertFalse((self.root / "path").exists())


if __name__ == "__main__":
    unittest.main()
