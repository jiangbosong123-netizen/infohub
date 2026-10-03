import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.impact_evaluation import validate_impact_evaluation_dataset
from app.impact_review_agreement import measure_impact_agreement
from app.impact_review_intake import import_impact_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/impact-contract-v1"
REVIEWED = ("impact-fixture-001", "impact-fixture-002", "impact-fixture-006")


class ImpactReviewAgreementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / "impact-private"
        shutil.copytree(FIXTURE, self.dataset)
        cases = [
            json.loads(line)
            for line in (self.dataset / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.labels = {}
        for case in cases:
            if case["case_id"] not in REVIEWED:
                continue
            self.labels[case["case_id"]] = case["annotation"]["labels"]
            for evidence in case["event_evidence"]:
                evidence["quote"] = None
            case["annotation"] = {"state": "unlabeled", "generated_by_model": False, "labels": {}}
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
                "model_assistance": False,
            }),
            encoding="utf-8",
        )
        (batch / "reviews.jsonl").write_text(
            "".join(
                json.dumps({
                    "case_id": case_id,
                    "content_sha256": self.cases[case_id]["content_sha256"],
                    "recorded_at": "2026-10-03T12:00:00Z",
                    "labels": value,
                }, ensure_ascii=False) + "\n"
                for case_id, value in labels.items()
            ),
            encoding="utf-8",
        )
        return batch

    def reviewed_dataset(self, first: dict, second: dict) -> Path:
        one = self.root / "review-one"
        import_impact_review_batch(
            self.dataset, self.batch(self.dataset, "reviewer-a", first), one,
            dataset_version="impact-review-v1",
        )
        two = self.root / "review-two"
        import_impact_review_batch(
            one, self.batch(one, "reviewer-b", second), two,
            dataset_version="impact-review-v2",
        )
        return two

    def train_pair(self) -> Path:
        first = {key: self.labels[key] for key in REVIEWED[:2]}
        second = json.loads(json.dumps(first))
        # Same structure for case 001, but different mechanism wording.
        second[REVIEWED[0]]["impact"][0]["mechanism"] = "Billing automation lowers cost."
        # Direction disagreement for case 002.
        second[REVIEWED[1]]["impact"][0]["direction"] = "mixed"
        return self.reviewed_dataset(first, second)

    def test_reports_direction_kappa_and_each_contract_component(self):
        report = measure_impact_agreement(
            self.train_pair(), reviewer_a="reviewer-a", reviewer_b="reviewer-b"
        )
        self.assertEqual(report.paired_cases, 2)
        direction = report.categorical["direction"]
        self.assertEqual(direction.confusion["positive"]["positive"], 1)
        self.assertEqual(direction.confusion["negative"]["mixed"], 1)
        self.assertEqual(direction.observed_agreement, 0.5)
        self.assertAlmostEqual(direction.expected_agreement, 0.25)
        self.assertAlmostEqual(direction.cohen_kappa, 1 / 3)
        self.assertEqual(report.kappa_target, 0.70)
        self.assertFalse(report.direction_kappa_passed)
        for name, matches in {
            "expected_status": 2, "target": 2, "aspect": 2, "horizon": 2,
            "direction": 1, "intensity_band": 2, "evidence_judgment": 2,
            "evidence": 2, "mechanism_presence": 2, "phenomena": 2,
            "structured_label": 1, "full_label": 0,
        }.items():
            with self.subTest(name):
                self.assertEqual(report.components[name].matches, matches)
                self.assertEqual(report.components[name].total, 2)
        self.assertFalse(report.quality_gate_eligible)

    def test_single_class_fields_do_not_claim_perfect_kappa(self):
        report = measure_impact_agreement(
            self.train_pair(), reviewer_a="reviewer-a", reviewer_b="reviewer-b"
        )
        status = report.categorical["expected_status"]
        self.assertEqual(status.observed_agreement, 1.0)
        self.assertEqual(status.expected_agreement, 1.0)
        self.assertIsNone(status.cohen_kappa)
        self.assertTrue(any(
            warning.startswith("expected_status kappa is undefined")
            for warning in report.warnings
        ))

    def test_evidence_agreement_compares_role_bound_support_and_contradiction(self):
        case_id = REVIEWED[2]
        first = {case_id: self.labels[case_id]}
        second = json.loads(json.dumps(first))
        impact = second[case_id]["impact"][0]
        impact.update({
            "evidence_judgment": "supported",
            "contradicting_evidence_ids": [],
            "phenomena": ["direct_effect"],
        })
        report = measure_impact_agreement(
            self.reviewed_dataset(first, second),
            reviewer_a="reviewer-a", reviewer_b="reviewer-b", split="dev",
        )
        self.assertEqual(report.paired_cases, 1)
        self.assertEqual(report.components["direction"].matches, 1)
        self.assertEqual(report.components["evidence"].matches, 0)
        self.assertEqual(report.components["evidence_judgment"].matches, 0)
        self.assertEqual(report.components["phenomena"].matches, 0)
        self.assertEqual(
            report.categorical["evidence_judgment"].confusion["conflicting"]["supported"], 1
        )

    def test_scope_and_pair_are_explicit(self):
        dataset = self.train_pair()
        train = measure_impact_agreement(
            dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-b", split="train"
        )
        self.assertEqual(train.scope_split, "train")
        self.assertEqual(train.dataset_cases, 4)
        self.assertEqual(train.paired_cases, 2)
        self.assertEqual(train.paired_by_language, {"en": 1, "zh": 1})
        self.assertTrue(any("does not cover" in warning for warning in train.warnings))
        reversed_pair = measure_impact_agreement(
            dataset, reviewer_a="reviewer-b", reviewer_b="reviewer-a", split="train"
        )
        self.assertEqual(
            reversed_pair.categorical["direction"].confusion["mixed"]["negative"], 1
        )
        missing = measure_impact_agreement(
            dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-c"
        )
        self.assertEqual(missing.paired_cases, 0)
        self.assertIsNone(missing.categorical["direction"].cohen_kappa)
        self.assertIsNone(missing.components["direction"].agreement)
        self.assertTrue(any("no cases" in warning for warning in missing.warnings))

    def test_same_reviewer_and_invalid_split_are_rejected(self):
        with self.assertRaisesRegex(EvaluationDatasetError, "distinct reviewers"):
            measure_impact_agreement(
                self.dataset, reviewer_a="reviewer-a", reviewer_b=" reviewer-a "
            )
        with self.assertRaisesRegex(EvaluationDatasetError, "valid split"):
            measure_impact_agreement(
                self.dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-b",
                split="future",
            )

    def test_report_is_read_only_and_serializable(self):
        dataset = self.train_pair()
        before = {
            path.name: path.read_bytes() for path in sorted(dataset.iterdir())
        }
        report = measure_impact_agreement(
            dataset, reviewer_a="reviewer-a", reviewer_b="reviewer-b"
        )
        after = {path.name: path.read_bytes() for path in sorted(dataset.iterdir())}
        self.assertEqual(before, after)
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(payload["report_version"], "impact-review-agreement-v1")
        self.assertEqual(payload["categorical"]["direction"]["observed_agreement"], 0.5)


if __name__ == "__main__":
    unittest.main()
