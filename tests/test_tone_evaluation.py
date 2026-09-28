import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_evaluation import _verified_private_evidence, validate_tone_evaluation_dataset


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneEvaluationDatasetTests(unittest.TestCase):
    def changed(self, mutate_cases=None, mutate_manifest=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "dataset"
        shutil.copytree(FIXTURE, root)
        rows = [json.loads(line) for line in (root / "cases.jsonl").read_text().splitlines()]
        manifest = json.loads((root / "manifest.json").read_text())
        if mutate_cases:
            mutate_cases(rows)
        if mutate_manifest:
            mutate_manifest(manifest)
        (root / "cases.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_fixture_covers_contract_without_claiming_model_quality(self):
        report = validate_tone_evaluation_dataset(FIXTURE)
        self.assertEqual(report.cases, 16)
        self.assertEqual(report.language_counts, {"en": 8, "zh": 8})
        self.assertEqual(report.polarity_counts["unknown"], 4)
        self.assertEqual(report.phenomenon_counts["sarcasm"], 2)
        self.assertFalse(report.publishable_tone_gold)
        self.assertEqual(report.target_gaps["documents"], 584)
        self.assertTrue(any("synthetic" in warning for warning in report.warnings))

    def test_quote_must_match_frozen_unicode_code_points(self):
        def mutate(rows):
            rows[1]["annotation"]["labels"]["tone"]["evidence"][0]["start_offset"] += 1

        with self.assertRaisesRegex(EvaluationDatasetError, "quote does not match"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_quote_hash_must_match(self):
        def mutate(rows):
            rows[0]["annotation"]["labels"]["tone"]["evidence"][0][
                "quote_sha256"
            ] = hashlib.sha256(b"different").hexdigest()

        with self.assertRaisesRegex(EvaluationDatasetError, "quote hash"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_neutral_requires_explicit_zero_intensity(self):
        def mutate(rows):
            rows[6]["annotation"]["labels"]["tone"]["intensity_band"] = "weak"

        with self.assertRaisesRegex(EvaluationDatasetError, "neutral polarity"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_unknown_requires_reason_and_unknown_band(self):
        def mutate(rows):
            tone = rows[12]["annotation"]["labels"]["tone"]
            tone["intensity_band"] = "weak"
            tone["uncertainty_reason"] = None

        with self.assertRaisesRegex(EvaluationDatasetError, "unknown polarity"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_unresolved_target_cannot_be_forced_to_a_direction(self):
        def mutate(rows):
            tone = rows[12]["annotation"]["labels"]["tone"]
            tone["polarity"] = "negative"
            tone["intensity_band"] = "moderate"
            tone["uncertainty_reason"] = None

        with self.assertRaisesRegex(EvaluationDatasetError, "target is unresolved"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_mixed_label_requires_explicit_mixed_slice(self):
        def mutate(rows):
            rows[7]["annotation"]["labels"]["tone"]["phenomena"] = ["reported_speech"]

        with self.assertRaisesRegex(EvaluationDatasetError, "mixed phenomenon"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_phenomena_are_controlled_unique_and_sorted(self):
        def mutate(rows):
            rows[9]["annotation"]["labels"]["tone"]["phenomena"] = [
                "sarcasm",
                "quotation",
                "sarcasm",
            ]

        with self.assertRaisesRegex(EvaluationDatasetError, "unique sorted"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_restricted_text_keeps_only_quote_hash_and_offsets(self):
        def mutate(rows):
            row = rows[0]
            row["text_storage"] = "restricted_reference"
            row["object_ref"] = "private:document-version/1"
            row.pop("text")
            row["annotation"]["labels"]["tone"]["evidence"][0]["quote"] = None

        report = validate_tone_evaluation_dataset(self.changed(mutate))
        self.assertFalse(report.publishable_tone_gold)

    def test_restricted_text_cannot_leak_quote(self):
        def mutate(rows):
            row = rows[0]
            row["text_storage"] = "restricted_reference"
            row["object_ref"] = "private:document-version/1"
            row.pop("text")

        with self.assertRaisesRegex(EvaluationDatasetError, "must not embed a quote"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_manifest_must_pin_every_difficult_slice(self):
        def mutate(manifest):
            del manifest["tone_target_plan"]["required_phenomena"]["sarcasm"]

        with self.assertRaisesRegex(EvaluationDatasetError, "every required phenomenon"):
            validate_tone_evaluation_dataset(self.changed(mutate_manifest=mutate))

    def test_private_evidence_review_is_bound_to_exact_cases_and_record(self):
        root = self.changed()
        manifest = json.loads((root / "manifest.json").read_text())
        cases_hash = hashlib.sha256((root / "cases.jsonl").read_bytes()).hexdigest()
        record = {
            "protocol_version": "tone-evidence-review-v1",
            "source": "human",
            "model_assistance": False,
            "verifier_id": "reviewer-a",
            "recorded_at": "2026-09-28T12:00:00Z",
            "cases_sha256": cases_hash,
            "artifact_manifest_sha256": "a" * 64,
            "artifacts_sha256": "b" * 64,
            "all_cases_verified": True,
            "quote_hash_and_offsets_verified": True,
            "normalized_artifacts_verified": True,
            "inspection_notes": "Verified against private frozen artifacts.",
        }
        record_bytes = (json.dumps(record, sort_keys=True) + "\n").encode()
        (root / "tone-evidence-review.json").write_bytes(record_bytes)
        manifest["tone_evidence_review"] = {
            key: value for key, value in record.items() if key != "inspection_notes"
        }
        manifest["tone_evidence_review"]["status"] = "verified"
        manifest["tone_evidence_review"]["review_record_sha256"] = hashlib.sha256(
            record_bytes
        ).hexdigest()
        manifest["tone_evidence_review"]["normalizer_versions"] = ["plain-text-v1"]
        manifest["tone_evidence_review"]["labels_verified"] = 3
        manifest["tone_evidence_review"]["spans_verified"] = 3
        self.assertTrue(_verified_private_evidence(manifest, root))
        stale_artifacts = json.loads(json.dumps(manifest))
        stale_artifacts["tone_evidence_review"]["artifacts_sha256"] = "c" * 64
        self.assertFalse(_verified_private_evidence(stale_artifacts, root))
        missing_counts = json.loads(json.dumps(manifest))
        del missing_counts["tone_evidence_review"]["spans_verified"]
        self.assertFalse(_verified_private_evidence(missing_counts, root))
        (root / "cases.jsonl").write_text(
            (root / "cases.jsonl").read_text() + "\n", encoding="utf-8"
        )
        self.assertFalse(_verified_private_evidence(manifest, root))

    def test_synthetic_case_cannot_be_relabelled_as_adjudicated_gold(self):
        def mutate(rows):
            rows[0]["annotation"]["state"] = "adjudicated"

        with self.assertRaisesRegex(EvaluationDatasetError, "synthetic text cannot become gold"):
            validate_tone_evaluation_dataset(self.changed(mutate))

    def test_unlabeled_case_is_valid_but_not_counted_as_an_assessment(self):
        def mutate(rows):
            rows[0]["annotation"].update(state="unlabeled", labels={})

        report = validate_tone_evaluation_dataset(self.changed(mutate))
        self.assertEqual(report.cases, 16)
        self.assertEqual(report.assessments, 15)
        self.assertFalse(report.publishable_tone_gold)

    def test_provisional_human_review_uses_the_same_tone_contract(self):
        def mutate(rows):
            row = rows[0]
            labels = row["annotation"]["labels"]
            row["annotation"] = {
                "state": "single_annotator",
                "generated_by_model": False,
                "labels": {},
                "reviews": [{
                    "reviewer_id": "reviewer-a",
                    "source": "human",
                    "independent": True,
                    "content_sha256": row["content_sha256"],
                    "recorded_at": "2026-09-28T12:00:00Z",
                    "labels": labels,
                }],
            }

        report = validate_tone_evaluation_dataset(self.changed(mutate))
        self.assertEqual(report.assessments, 15)

        def invalid(rows):
            mutate(rows)
            rows[0]["annotation"]["reviews"][0]["labels"]["tone"]["polarity"] = "bullish"

        with self.assertRaisesRegex(EvaluationDatasetError, "unsupported polarity"):
            validate_tone_evaluation_dataset(self.changed(invalid))

    def test_model_generated_human_gold_is_rejected_by_base_contract(self):
        def mutate(rows):
            row = rows[0]
            row["text_storage"] = "restricted_reference"
            row["object_ref"] = "private:document-version/1"
            row.pop("text")
            row["annotation"].update(state="adjudicated", generated_by_model=True)

        with self.assertRaisesRegex(EvaluationDatasetError, "model output as gold"):
            validate_tone_evaluation_dataset(self.changed(mutate))


if __name__ == "__main__":
    unittest.main()
