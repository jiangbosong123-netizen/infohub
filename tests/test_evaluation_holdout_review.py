import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import (
    HOLDOUT_CHECKS, EvaluationDatasetError, _load_cases, _verified_holdout,
    validate_evaluation_dataset,
)
from app.evaluation_holdout_review import freeze_holdout_review
from app.evaluation_review_intake import import_review_batch


class EvaluationHoldoutReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "synthetic-pending"
        self.source.mkdir()
        self.cases = []
        for number, split, source, published in (
            (1, "train", "source-a", "2026-08-20T00:00:00Z"),
            (2, "test", "source-b", "2026-09-20T00:00:00Z"),
        ):
            self.cases.append({
                "case_id": f"case-{number}", "document_ref": f"doc-{number}",
                "event_group_id": f"event-{number}", "origin_group_id": f"origin-{number}",
                "language": "en" if number == 1 else "zh",
                "text_storage": "restricted_reference",
                "object_ref": f"private-db:synthetic/{number}",
                "content_sha256": hashlib.sha256(f"synthetic {number}".encode()).hexdigest(),
                "source_kind": source, "published_at": published, "split": split,
                "annotation": {"state": "unlabeled", "generated_by_model": False, "labels": {}},
            })
        self.source_manifest = {
            "schema_version": "evaluation-dataset-v1", "dataset_version": "synthetic-pending-v1",
            "target_plan": {"documents": 600, "event_groups": 150,
                            "impact_annotations": 300, "security_cases": 50},
            "split_policy": "blind-holdout", "admission_version": "evaluation-admission-v3",
            "source_database_verified_at_admission": True,
            "holdout_review": {"status": "pending", "heldout_after": "2026-09-01T00:00:00Z",
                               "heldout_source_refs": ["source-b"]},
        }
        (self.source / "manifest.json").write_text(json.dumps(self.source_manifest), encoding="utf-8")
        (self.source / "cases.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in self.cases), encoding="utf-8")
        self.review = self.root / "holdout-review-input.json"
        self.write_review()

    def write_review(self, **overrides):
        record = {
            "schema_version": "holdout-review-v1", "source": "human",
            "model_assistance": False, "reviewer_id": "fixture-inspector",
            "recorded_at": "2026-09-21T12:00:00Z",
            "source_dataset_version": "synthetic-pending-v1",
            "source_manifest_sha256": hashlib.sha256((self.source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((self.source / "cases.jsonl").read_bytes()).hexdigest(),
            "heldout_after": "2026-09-01T00:00:00Z", "heldout_source_refs": ["source-b"],
            "checks": {name: True for name in HOLDOUT_CHECKS},
            "inspection_notes": "Synthetic fixture inspected for workflow verification only.",
        }
        record.update(overrides)
        self.review.write_text(json.dumps(record), encoding="utf-8")

    def test_pending_blocks_annotation_then_review_creates_new_version(self):
        batch = self.root / "annotation-batch"
        batch.mkdir()
        (batch / "manifest.json").write_text(json.dumps({
            "schema_version": "human-relevance-review-batch-v1",
            "source_dataset_version": "synthetic-pending-v1",
            "source_manifest_sha256": hashlib.sha256((self.source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((self.source / "cases.jsonl").read_bytes()).hexdigest(),
            "reviewer_id": "fixture-reviewer", "source": "human",
            "independent": True, "model_assistance": False,
        }), encoding="utf-8")
        (batch / "reviews.jsonl").write_text(json.dumps({
            "case_id": "case-2", "content_sha256": self.cases[1]["content_sha256"],
            "recorded_at": "2026-09-21T13:00:00Z", "labels": {"relevance": "unknown"},
        }) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationDatasetError, "requires verified human leakage review"):
            import_review_batch(self.source, batch, self.root / "blocked",
                                dataset_version="blocked-v1")
        self.assertFalse((self.root / "blocked").exists())

        approved = self.root / "synthetic-reviewed"
        result = freeze_holdout_review(self.source, self.review, approved,
                                       dataset_version="synthetic-reviewed-v1")
        self.assertEqual(result.holdout_status, "verified")
        self.assertFalse(result.publishable_gold)
        self.assertEqual((approved / "cases.jsonl").read_bytes(),
                         (self.source / "cases.jsonl").read_bytes())
        approved_manifest = json.loads((approved / "manifest.json").read_text())
        self.assertTrue(_verified_holdout(approved_manifest, _load_cases(approved / "cases.jsonl"), approved))
        batch_manifest = json.loads((batch / "manifest.json").read_text())
        batch_manifest.update({
            "source_dataset_version": "synthetic-reviewed-v1",
            "source_manifest_sha256": hashlib.sha256((approved / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((approved / "cases.jsonl").read_bytes()).hexdigest(),
        })
        (batch / "manifest.json").write_text(json.dumps(batch_manifest), encoding="utf-8")
        annotation = self.root / "synthetic-reviewed-annotation"
        import_review_batch(approved, batch, annotation, dataset_version="reviewed-annotation-v1")
        self.assertTrue((annotation / "holdout-review.json").is_file())
        self.assertFalse(validate_evaluation_dataset(annotation).publishable_gold)

    def test_missing_human_check_or_changed_source_cannot_approve(self):
        checks = {name: True for name in HOLDOUT_CHECKS}
        checks["training_exclusion"] = False
        self.write_review(checks=checks)
        with self.assertRaisesRegex(EvaluationDatasetError, "missing required human checks"):
            freeze_holdout_review(self.source, self.review, self.root / "no-check",
                                  dataset_version="no-check-v1")
        self.assertFalse((self.root / "no-check").exists())
        self.write_review(source_cases_sha256="0" * 64)
        with self.assertRaisesRegex(EvaluationDatasetError, "differs from frozen dataset"):
            freeze_holdout_review(self.source, self.review, self.root / "stale",
                                  dataset_version="stale-v1")

    def test_review_record_tamper_invalidates_verified_status(self):
        approved = self.root / "synthetic-reviewed"
        freeze_holdout_review(self.source, self.review, approved,
                              dataset_version="synthetic-reviewed-v1")
        record = json.loads((approved / "holdout-review.json").read_text())
        record["inspection_notes"] = "tampered"
        (approved / "holdout-review.json").write_text(json.dumps(record), encoding="utf-8")
        manifest = json.loads((approved / "manifest.json").read_text())
        self.assertFalse(_verified_holdout(manifest, _load_cases(approved / "cases.jsonl"), approved))


if __name__ == "__main__":
    unittest.main()
