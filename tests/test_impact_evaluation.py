import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.impact_evaluation import (
    _verified_private_evidence,
    validate_impact_evaluation_dataset,
)


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/impact-contract-v1"


class ImpactEvaluationDatasetTests(unittest.TestCase):
    def changed(self, mutate_cases=None, mutate_manifest=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "dataset"
        shutil.copytree(FIXTURE, root)
        rows = [
            json.loads(line)
            for line in (root / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if mutate_cases:
            mutate_cases(rows)
        if mutate_manifest:
            mutate_manifest(manifest)
        (root / "cases.jsonl").write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return root

    def test_fixture_covers_contract_without_claiming_model_quality(self):
        report = validate_impact_evaluation_dataset(FIXTURE)
        self.assertEqual(report.cases, 16)
        self.assertEqual(report.language_counts, {"en": 8, "zh": 8})
        self.assertEqual(report.status_counts["insufficient_evidence"], 4)
        self.assertEqual(report.evidence_judgment_counts["conflicting"], 1)
        self.assertEqual(report.phenomenon_counts["prompt_injection"], 2)
        self.assertFalse(report.publishable_impact_gold)
        self.assertEqual(report.target_gaps["assessments"], 284)
        self.assertTrue(any("synthetic" in warning for warning in report.warnings))
        committed = json.loads((FIXTURE / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(json.dumps(report.to_dict())), committed)

    def test_horizon_must_use_the_frozen_bucket_boundaries(self):
        def mutate(rows):
            rows[0]["annotation"]["labels"]["impact"][0]["horizon"]["min_days"] = 0

        with self.assertRaisesRegex(EvaluationDatasetError, "horizon boundaries"):
            validate_impact_evaluation_dataset(self.changed(mutate))

        def boolean_boundary(rows):
            rows[0]["annotation"]["labels"]["impact"][0]["horizon"]["min_days"] = False

        with self.assertRaisesRegex(EvaluationDatasetError, "boundary types"):
            validate_impact_evaluation_dataset(self.changed(boolean_boundary))

    def test_insufficient_evidence_is_not_neutral_or_a_supported_assessment(self):
        def mutate(rows):
            label = rows[10]["annotation"]["labels"]["impact"][0]
            label["direction"] = "neutral"
            label["intensity_band"] = "zero"

        with self.assertRaisesRegex(EvaluationDatasetError, "direction.*disagree"):
            validate_impact_evaluation_dataset(self.changed(mutate))

    def test_unknown_direction_requires_a_reason_and_unknown_slice(self):
        def mutate(rows):
            label = rows[10]["annotation"]["labels"]["impact"][0]
            label["uncertainty_reason"] = None

        with self.assertRaisesRegex(EvaluationDatasetError, "insufficient_evidence semantics"):
            validate_impact_evaluation_dataset(self.changed(mutate))

    def test_conflicting_judgment_requires_role_bound_contradiction(self):
        def mutate(rows):
            rows[5]["event_evidence"][1]["role"] = "supports"

        with self.assertRaisesRegex(EvaluationDatasetError, "direct contradiction"):
            validate_impact_evaluation_dataset(self.changed(mutate))

    def test_generated_or_truncated_material_is_not_direct_impact_evidence(self):
        def generated(rows):
            rows[0]["event_evidence"][0]["payload_kind"] = "generated_metadata"

        with self.assertRaisesRegex(EvaluationDatasetError, "usable direct support"):
            validate_impact_evaluation_dataset(self.changed(generated))

        def truncated(rows):
            rows[0]["event_evidence"][0]["truncated"] = True

        with self.assertRaisesRegex(EvaluationDatasetError, "usable direct support"):
            validate_impact_evaluation_dataset(self.changed(truncated))

    def test_numeric_revision_requires_same_period_prior_and_revised_facts(self):
        def missing_prior(rows):
            rows[6]["event_facts"][0]["kind"] = "actual"

        with self.assertRaisesRegex(EvaluationDatasetError, "matching prior and revised"):
            validate_impact_evaluation_dataset(self.changed(missing_prior))

        def floating_value(rows):
            rows[6]["event_facts"][0]["value"] = 100.0

        with self.assertRaisesRegex(EvaluationDatasetError, "fact value"):
            validate_impact_evaluation_dataset(self.changed(floating_value))

    def test_evidence_quote_and_hash_are_bound_to_frozen_text(self):
        def bad_offset(rows):
            rows[1]["event_evidence"][0]["start_offset"] += 1

        with self.assertRaisesRegex(EvaluationDatasetError, "quote does not match"):
            validate_impact_evaluation_dataset(self.changed(bad_offset))

        def bad_hash(rows):
            rows[1]["event_evidence"][0]["quote_sha256"] = hashlib.sha256(
                b"different"
            ).hexdigest()

        with self.assertRaisesRegex(EvaluationDatasetError, "quote hash"):
            validate_impact_evaluation_dataset(self.changed(bad_hash))

    def test_known_direction_uses_discrete_intensity_without_uncertainty(self):
        def mutate(rows):
            label = rows[0]["annotation"]["labels"]["impact"][0]
            label["intensity_band"] = "unknown"
            label["uncertainty_reason"] = "maybe"

        with self.assertRaisesRegex(EvaluationDatasetError, "known direction"):
            validate_impact_evaluation_dataset(self.changed(mutate))

    def test_prompt_injection_cannot_enter_a_natural_split(self):
        def mutate(rows):
            rows[14]["split"] = "test"

        with self.assertRaisesRegex(EvaluationDatasetError, "prompt_injection"):
            validate_impact_evaluation_dataset(self.changed(mutate))

    def test_manifest_must_pin_every_difficult_slice(self):
        def mutate(manifest):
            del manifest["impact_target_plan"]["required_phenomena"]["numeric_revision"]

        with self.assertRaisesRegex(EvaluationDatasetError, "every required phenomenon"):
            validate_impact_evaluation_dataset(self.changed(mutate_manifest=mutate))

    def test_lowering_manifest_targets_cannot_publish_the_fixture(self):
        def mutate(manifest):
            plan = manifest["impact_target_plan"]
            for key in plan:
                if key == "required_phenomena":
                    plan[key] = {name: 0 for name in plan[key]}
                else:
                    plan[key] = 0

        report = validate_impact_evaluation_dataset(self.changed(mutate_manifest=mutate))
        self.assertFalse(report.publishable_impact_gold)
        self.assertEqual(report.target_gaps["assessments"], 284)
        self.assertEqual(report.target_gaps["phenomenon:numeric_revision"], 29)

    def test_restricted_text_cannot_leak_event_quotes(self):
        def valid_restricted(rows):
            row = rows[0]
            row["text_storage"] = "restricted_reference"
            row["object_ref"] = "private:document-version/1"
            row.pop("text")
            row["event_evidence"][0]["quote"] = None

        report = validate_impact_evaluation_dataset(self.changed(valid_restricted))
        self.assertFalse(report.publishable_impact_gold)

        def leaked(rows):
            row = rows[0]
            row["text_storage"] = "restricted_reference"
            row["object_ref"] = "private:document-version/1"
            row.pop("text")

        with self.assertRaisesRegex(EvaluationDatasetError, "must not embed a quote"):
            validate_impact_evaluation_dataset(self.changed(leaked))

    def test_provisional_human_review_uses_the_same_impact_contract(self):
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
                    "recorded_at": "2026-10-03T12:00:00Z",
                    "labels": labels,
                }],
            }

        report = validate_impact_evaluation_dataset(self.changed(mutate))
        self.assertEqual(report.assessments, 15)

        def invalid(rows):
            mutate(rows)
            rows[0]["annotation"]["reviews"][0]["labels"]["impact"][0][
                "aspect"
            ] = "share_price"

        with self.assertRaisesRegex(EvaluationDatasetError, "unsupported aspect"):
            validate_impact_evaluation_dataset(self.changed(invalid))

    def test_synthetic_case_cannot_be_relabelled_as_adjudicated_gold(self):
        def mutate(rows):
            rows[0]["annotation"]["state"] = "adjudicated"

        with self.assertRaisesRegex(EvaluationDatasetError, "synthetic text cannot become gold"):
            validate_impact_evaluation_dataset(self.changed(mutate))

    def test_private_evidence_review_is_hash_bound_to_exact_cases(self):
        root = self.changed()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        cases_hash = hashlib.sha256((root / "cases.jsonl").read_bytes()).hexdigest()
        record = {
            "protocol_version": "impact-evidence-review-v1",
            "source": "human",
            "model_assistance": False,
            "verifier_id": "reviewer-a",
            "recorded_at": "2026-10-03T12:00:00Z",
            "cases_sha256": cases_hash,
            "assessments_verified": 16,
            "all_cases_verified": True,
            "event_roles_verified": True,
            "normalized_artifacts_verified": True,
            "inspection_notes": "Verified event evidence and roles against frozen artifacts.",
        }
        record_bytes = (json.dumps(record, sort_keys=True) + "\n").encode()
        (root / "impact-evidence-review.json").write_bytes(record_bytes)
        manifest["impact_evidence_review"] = {
            key: value for key, value in record.items() if key != "inspection_notes"
        }
        manifest["impact_evidence_review"]["status"] = "verified"
        manifest["impact_evidence_review"]["review_record_sha256"] = hashlib.sha256(
            record_bytes
        ).hexdigest()
        self.assertTrue(_verified_private_evidence(manifest, root))
        stale = json.loads(json.dumps(manifest))
        stale["impact_evidence_review"]["cases_sha256"] = "a" * 64
        self.assertFalse(_verified_private_evidence(stale, root))
        (root / "cases.jsonl").write_text(
            (root / "cases.jsonl").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        self.assertFalse(_verified_private_evidence(manifest, root))


if __name__ == "__main__":
    unittest.main()
