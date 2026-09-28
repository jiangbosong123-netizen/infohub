import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation import _verified_private_evidence, validate_tone_evaluation_dataset
from app.tone_private_evidence_review import freeze_tone_private_evidence_review


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class TonePrivateEvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "adjudicated"
        self.source.mkdir()
        manifest = json.loads((FIXTURE / "manifest.json").read_text())
        manifest["dataset_version"] = "tone-adjudicated-v1"
        case = json.loads((FIXTURE / "cases.jsonl").read_text().splitlines()[0])
        self.text = case.pop("text")
        labels = case["annotation"]["labels"]
        labels["tone"]["evidence"][0]["quote"] = None
        case["text_storage"] = "restricted_reference"
        case["object_ref"] = "private-db:document-version/1"
        review_a = {
            "reviewer_id": "reviewer-a", "source": "human", "independent": True,
            "content_sha256": case["content_sha256"], "recorded_at": "2026-09-28T10:00:00Z",
            "labels": labels,
        }
        review_b = {**json.loads(json.dumps(review_a)), "reviewer_id": "reviewer-b"}
        review_b["recorded_at"] = "2026-09-28T10:05:00Z"
        case["annotation"] = {
            "state": "adjudicated", "generated_by_model": False, "labels": labels,
            "reviews": [review_a, review_b],
            "adjudication": {
                "adjudicator_id": "reviewer-c", "source": "human",
                "content_sha256": case["content_sha256"],
                "recorded_at": "2026-09-28T11:00:00Z", "labels": labels,
                "reason": "The evidence directly supports the final label.",
            },
        }
        self.case = case
        (self.source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.source / "cases.jsonl").write_text(
            json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        validate_tone_evaluation_dataset(self.source)
        self.artifacts = self.root / "artifacts"
        (self.artifacts / "texts").mkdir(parents=True)
        self.text_path = self.artifacts / "texts" / "case.txt"
        self.text_path.write_text(self.text, encoding="utf-8")
        self.write_artifacts()
        self.review = self.root / "evidence-review.json"
        self.write_review()

    def write_artifacts(self, **changes):
        payload = self.text_path.read_bytes()
        row = {
            "case_id": self.case["case_id"],
            "object_ref": self.case["object_ref"],
            "content_sha256": self.case["content_sha256"],
            "normalizer_version": "plain-text-v1",
            "text_ref": "texts/case.txt",
            "text_sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        row.update(changes)
        (self.artifacts / "artifacts.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8"
        )
        artifact_manifest = {
            "schema_version": "tone-private-artifacts-v1",
            "source_dataset_version": "tone-adjudicated-v1",
            "source_manifest_sha256": hashlib.sha256(
                (self.source / "manifest.json").read_bytes()
            ).hexdigest(),
            "source_cases_sha256": hashlib.sha256(
                (self.source / "cases.jsonl").read_bytes()
            ).hexdigest(),
            "artifacts_sha256": hashlib.sha256(
                (self.artifacts / "artifacts.jsonl").read_bytes()
            ).hexdigest(),
            "case_count": 1,
        }
        (self.artifacts / "manifest.json").write_text(
            json.dumps(artifact_manifest), encoding="utf-8"
        )

    def write_review(self, **changes):
        record = {
            "protocol_version": "tone-evidence-review-v1",
            "source_dataset_version": "tone-adjudicated-v1",
            "source_manifest_sha256": hashlib.sha256(
                (self.source / "manifest.json").read_bytes()
            ).hexdigest(),
            "source_cases_sha256": hashlib.sha256(
                (self.source / "cases.jsonl").read_bytes()
            ).hexdigest(),
            "cases_sha256": hashlib.sha256(
                (self.source / "cases.jsonl").read_bytes()
            ).hexdigest(),
            "artifact_manifest_sha256": hashlib.sha256(
                (self.artifacts / "manifest.json").read_bytes()
            ).hexdigest(),
            "artifacts_sha256": hashlib.sha256(
                (self.artifacts / "artifacts.jsonl").read_bytes()
            ).hexdigest(),
            "verifier_id": "reviewer-d",
            "recorded_at": "2026-09-28T12:00:00Z",
            "source": "human",
            "model_assistance": False,
            "all_cases_verified": True,
            "quote_hash_and_offsets_verified": True,
            "normalized_artifacts_verified": True,
            "inspection_notes": "Compared all final and reviewer spans with the frozen text.",
        }
        record.update(changes)
        self.review.write_text(json.dumps(record), encoding="utf-8")

    def freeze(self, name="verified"):
        output = self.root / name
        report = freeze_tone_private_evidence_review(
            self.source, self.artifacts, self.review, output,
            dataset_version=f"tone-{name}-v1",
        )
        return output, report

    def test_freezes_hash_bound_review_without_copying_private_text(self):
        output, report = self.freeze()
        self.assertEqual(report.cases_verified, 1)
        self.assertEqual(report.labels_verified, 3)
        self.assertEqual(report.spans_verified, 3)
        self.assertFalse(report.publishable_tone_gold)
        self.assertEqual((output / "cases.jsonl").read_bytes(), (self.source / "cases.jsonl").read_bytes())
        self.assertFalse((output / "texts").exists())
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertTrue(_verified_private_evidence(manifest, output))
        self.assertEqual(manifest["tone_evidence_review"]["normalizer_versions"], ["plain-text-v1"])
        with self.assertRaisesRegex(EvaluationDatasetError, "new immutable directory"):
            self.freeze()

    def test_wrong_quote_offset_and_corrupt_artifact_fail_closed(self):
        cases = [json.loads((self.source / "cases.jsonl").read_text())]
        annotation = cases[0]["annotation"]
        for labels in [
            annotation["labels"],
            annotation["reviews"][0]["labels"],
            annotation["reviews"][1]["labels"],
            annotation["adjudication"]["labels"],
        ]:
            labels["tone"]["evidence"][0]["start_offset"] += 1
        (self.source / "cases.jsonl").write_text(json.dumps(cases[0]) + "\n")
        self.write_artifacts()
        self.write_review()
        with self.assertRaisesRegex(EvaluationDatasetError, "quote hash or offsets differ"):
            self.freeze("bad-offset")

    def test_tampered_text_hash_and_stale_bundle_fail_closed(self):
        self.text_path.write_text(self.text + " changed", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationDatasetError, "text hash or size differs"):
            self.freeze("tampered")
        self.text_path.write_text(self.text, encoding="utf-8")
        self.write_artifacts()
        artifact_manifest = json.loads((self.artifacts / "manifest.json").read_text())
        artifact_manifest["source_cases_sha256"] = "0" * 64
        (self.artifacts / "manifest.json").write_text(json.dumps(artifact_manifest))
        self.write_review()
        with self.assertRaisesRegex(EvaluationDatasetError, "does not match frozen source"):
            self.freeze("stale")

    def test_path_escape_symlink_and_incomplete_coverage_are_rejected(self):
        self.write_artifacts(text_ref="../escape.txt")
        self.write_review()
        with self.assertRaisesRegex(EvaluationDatasetError, "escapes artifact root"):
            self.freeze("escape")
        outside = self.root / "outside.txt"
        outside.write_text(self.text)
        link = self.artifacts / "texts" / "link.txt"
        os.symlink(outside, link)
        self.write_artifacts(text_ref="texts/link.txt", text_sha256=hashlib.sha256(outside.read_bytes()).hexdigest())
        self.write_review()
        with self.assertRaisesRegex(EvaluationDatasetError, "cannot use symlinks"):
            self.freeze("symlink")
        (self.artifacts / "artifacts.jsonl").write_text("")
        artifact_manifest = json.loads((self.artifacts / "manifest.json").read_text())
        artifact_manifest["artifacts_sha256"] = hashlib.sha256(b"").hexdigest()
        (self.artifacts / "manifest.json").write_text(json.dumps(artifact_manifest))
        self.write_review()
        with self.assertRaisesRegex(EvaluationDatasetError, "cover every source case"):
            self.freeze("missing")

    def test_attestation_and_verifier_independence_are_required(self):
        for changes, message, name in [
            ({"verifier_id": "reviewer-a"}, "independent", "same-person"),
            ({"model_assistance": True}, "does not match artifacts", "assisted"),
            ({"all_cases_verified": False}, "does not match artifacts", "partial"),
            ({"artifact_manifest_sha256": "0" * 64}, "does not match artifacts", "stale-review"),
        ]:
            with self.subTest(name=name):
                self.write_review(**changes)
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    self.freeze(name)
                self.assertFalse((self.root / name).exists())


if __name__ == "__main__":
    unittest.main()
