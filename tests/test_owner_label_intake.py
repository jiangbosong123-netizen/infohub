import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError, validate_evaluation_dataset
from app.evaluation_review_intake import import_review_batch
from app.impact_evaluation import validate_impact_evaluation_dataset
from app.owner_label_intake import TASKS, import_owner_label_batch
from app.tone_evaluation import validate_tone_evaluation_dataset


DATASETS = Path(__file__).parents[1] / "evaluation/datasets"
FIXTURES = {"relevance": "foundation-v1", "tone": "tone-contract-v1", "impact": "impact-contract-v1"}
GOLD_FLAGS = {
    "relevance": "publishable_gold",
    "tone": "publishable_tone_gold",
    "impact": "publishable_impact_gold",
}
VALIDATORS = {
    "relevance": validate_evaluation_dataset,
    "tone": validate_tone_evaluation_dataset,
    "impact": validate_impact_evaluation_dataset,
}


def _restrict(task: str, case: dict) -> dict:
    """Turn a synthetic case into a restricted unlabeled one; return a valid label for it."""
    if task == "relevance":
        labels = {"relevance": "relevant"}
    else:
        labels = case["annotation"]["labels"]
        if task == "tone":
            for span in labels["tone"]["evidence"]:
                span["quote"] = None
        else:
            for evidence in case["event_evidence"]:
                evidence["quote"] = None
    case["annotation"] = {"state": "unlabeled", "generated_by_model": False, "labels": {}}
    case["text_storage"] = "restricted_reference"
    case["object_ref"] = f"private-db:{case['document_ref']}"
    case.pop("text")
    return labels


class OwnerLabelIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def prepare(self, task: str, count: int = 2) -> tuple[Path, list[dict], dict[str, dict]]:
        dataset = self.root / f"{task}-private"
        shutil.copytree(DATASETS / FIXTURES[task], dataset)
        cases = [
            json.loads(line)
            for line in (dataset / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        labels = {case["case_id"]: _restrict(task, case) for case in cases[:count]}
        self.write_cases(dataset, cases)
        return dataset, cases, labels

    @staticmethod
    def write_cases(dataset: Path, cases: list[dict]) -> None:
        (dataset / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
            encoding="utf-8",
        )

    def batch(self, for_task: str, source: Path, labels: dict[str, dict], cases: list[dict], *,
              name: str = "owner", owner: str = "owner-1", **overrides) -> Path:
        batch = self.root / f"batch-{source.name}-{name}"
        batch.mkdir()
        digests = {case["case_id"]: case["content_sha256"] for case in cases}
        manifest = {
            "schema_version": "owner-label-batch-v1",
            "task": for_task,
            "label_definition": f"{for_task}-definition-v1",
            "source_dataset_version": VALIDATORS[for_task](source).dataset_version,
            "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((source / "cases.jsonl").read_bytes()).hexdigest(),
            "owner_id": owner,
            "source": "human",
            "blind": True,
            "model_assistance": False,
            **overrides,
        }
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (batch / "reviews.jsonl").write_text(
            "".join(
                json.dumps({
                    "case_id": case_id,
                    "content_sha256": digests[case_id],
                    "recorded_at": "2026-10-03T12:00:00Z",
                    "labels": value,
                }, ensure_ascii=False) + "\n"
                for case_id, value in labels.items()
            ),
            encoding="utf-8",
        )
        return batch

    def load(self, dataset: Path) -> tuple[dict, list[dict]]:
        manifest = json.loads((dataset / "manifest.json").read_text())
        cases = [json.loads(line) for line in (dataset / "cases.jsonl").read_text().splitlines()]
        return manifest, cases

    def test_owner_labels_become_final_experimental_labels_for_every_task(self):
        for task in TASKS:
            with self.subTest(task):
                dataset, cases, labels = self.prepare(task)
                first_id, second_id = list(labels)
                one = self.root / f"{task}-owner-one"
                report = import_owner_label_batch(
                    task, dataset, self.batch(task, dataset, {first_id: labels[first_id]}, cases),
                    one, dataset_version=f"{task}-owner-v1",
                )
                self.assertEqual((report.labeled_cases, report.owner_labeled_cases), (1, 1))
                self.assertFalse(report.publishable_gold)
                self.assertEqual(report.to_dict()["claim_scope"], "experimental")
                manifest, labeled = self.load(one)
                self.assertEqual(
                    manifest["annotation_protocol"],
                    {"version": "single-owner-v1", "owner_id": "owner-1",
                     "label_definition": f"{task}-definition-v1"},
                )
                case = next(item for item in labeled if item["case_id"] == first_id)
                self.assertEqual(case["annotation"]["state"], "owner_labeled")
                self.assertEqual(case["annotation"]["labels"], labels[first_id])
                self.assertEqual(case["annotation"]["owner_label"]["labels"], labels[first_id])
                self.assertTrue(case["annotation"]["owner_label"]["blind"])

                _, current = self.load(one)
                two = self.root / f"{task}-owner-two"
                report = import_owner_label_batch(
                    task, one,
                    self.batch(task, one, {second_id: labels[second_id]}, current, name="second"),
                    two, dataset_version=f"{task}-owner-v2",
                )
                self.assertEqual(report.owner_labeled_cases, 2)
                self.assertIs(VALIDATORS[task](two).to_dict()[GOLD_FLAGS[task]], False)

    def test_owner_identity_and_attestation_are_enforced(self):
        dataset, cases, labels = self.prepare("relevance")
        first_id, second_id = list(labels)
        one = self.root / "owner-one"
        import_owner_label_batch(
            "relevance", dataset,
            self.batch("relevance", dataset, {first_id: labels[first_id]}, cases),
            one, dataset_version="owner-v1",
        )
        _, current = self.load(one)
        for name, kwargs, message in (
            ("other-owner", {"owner": "someone-else"}, "owner differs"),
            ("not-blind", {"blind": False}, "blind, no-model"),
            ("assisted", {"model_assistance": True}, "blind, no-model"),
            ("wrong-task", {"task": "tone"}, "not for task relevance"),
            ("other-definition", {"label_definition": "relevance-definition-v2"},
             "definition differs"),
        ):
            with self.subTest(name):
                batch = self.batch(
                    "relevance", one, {second_id: labels[second_id]}, current, name=name, **kwargs
                )
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    import_owner_label_batch(
                        "relevance", one, batch, self.root / name, dataset_version="owner-v2"
                    )
                self.assertFalse((self.root / name).exists())

    def test_labeled_or_reviewed_cases_cannot_be_relabelled_by_intake(self):
        dataset, cases, labels = self.prepare("relevance")
        first_id = next(iter(labels))
        one = self.root / "owner-one"
        import_owner_label_batch(
            "relevance", dataset,
            self.batch("relevance", dataset, {first_id: labels[first_id]}, cases),
            one, dataset_version="owner-v1",
        )
        _, current = self.load(one)
        again = self.batch(
            "relevance", one, {first_id: {"relevance": "not_relevant"}}, current, name="again"
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "already has annotations"):
            import_owner_label_batch(
                "relevance", one, again, self.root / "again", dataset_version="owner-v2"
            )

        # A case holding a provisional multi-reviewer opinion cannot switch tiers either.
        second_id = list(labels)[1]
        review = self.root / "review-batch"
        review.mkdir()
        (review / "manifest.json").write_text(json.dumps({
            "schema_version": "human-relevance-review-batch-v1",
            "source_dataset_version": "owner-v1",
            "source_manifest_sha256": hashlib.sha256((one / "manifest.json").read_bytes()).hexdigest(),
            "source_cases_sha256": hashlib.sha256((one / "cases.jsonl").read_bytes()).hexdigest(),
            "reviewer_id": "reviewer-a", "source": "human", "independent": True,
            "model_assistance": False,
        }), encoding="utf-8")
        second_digest = next(c["content_sha256"] for c in current if c["case_id"] == second_id)
        (review / "reviews.jsonl").write_text(json.dumps({
            "case_id": second_id, "content_sha256": second_digest,
            "recorded_at": "2026-10-03T12:00:00Z", "labels": {"relevance": "unknown"},
        }) + "\n", encoding="utf-8")
        reviewed = self.root / "reviewed"
        import_review_batch(one, review, reviewed, dataset_version="owner-reviewed-v1")
        _, reviewed_cases = self.load(reviewed)
        switch = self.batch(
            "relevance", reviewed, {second_id: labels[second_id]}, reviewed_cases, name="switch"
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "already has annotations"):
            import_owner_label_batch(
                "relevance", reviewed, switch, self.root / "switch",
                dataset_version="owner-reviewed-v2",
            )

    def test_task_contract_still_validates_owner_labels(self):
        dataset, cases, labels = self.prepare("impact", count=1)
        case_id = next(iter(labels))
        bad = json.loads(json.dumps(labels[case_id]))
        bad["impact"][0]["aspect"] = "share_price"
        with self.assertRaisesRegex(EvaluationDatasetError, "unsupported aspect"):
            import_owner_label_batch(
                "impact", dataset, self.batch("impact", dataset, {case_id: bad}, cases),
                self.root / "bad", dataset_version="impact-owner-v1",
            )
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid relevance label"):
            relevance, relevance_cases, relevance_labels = self.prepare("relevance", count=1)
            relevance_id = next(iter(relevance_labels))
            import_owner_label_batch(
                "relevance", relevance,
                self.batch("relevance", relevance, {relevance_id: {"relevance": "maybe"}},
                           relevance_cases, name="bad-relevance"),
                self.root / "bad-relevance", dataset_version="relevance-owner-v1",
            )


class OwnerAndSilverValidatorTests(unittest.TestCase):
    """Hand-edited datasets must not be able to fake or mix annotation tiers."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset = Path(temporary.name) / "dataset"
        shutil.copytree(DATASETS / "foundation-v1", self.dataset)
        self.manifest = json.loads((self.dataset / "manifest.json").read_text())
        self.cases = [
            json.loads(line) for line in (self.dataset / "cases.jsonl").read_text().splitlines()
        ]
        self.case = self.cases[0]
        _restrict("relevance", self.case)
        self.manifest["annotation_protocol"] = {
            "version": "single-owner-v1", "owner_id": "owner-1",
            "label_definition": "relevance-definition-v1",
        }

    def owner_label(self, **overrides) -> dict:
        labels = {"relevance": "relevant"}
        record = {
            "owner_id": "owner-1", "source": "human", "blind": True, "model_assistance": False,
            "content_sha256": self.case["content_sha256"],
            "recorded_at": "2026-10-03T12:00:00Z", "labels": labels,
        }
        record.update(overrides)
        return {"state": "owner_labeled", "generated_by_model": False,
                "labels": labels, "owner_label": record}

    def silver(self, **overrides) -> dict:
        labeler = {
            "labeler_id": "relevance-rule", "labeler_version": "v0", "config_sha256": "a" * 64,
            "content_sha256": self.case["content_sha256"], "generated_at": "2026-10-03T12:00:00Z",
        }
        labeler.update(overrides)
        return {"state": "algorithm_labeled", "generated_by_model": True,
                "labels": {"relevance": "relevant"}, "labeler": labeler}

    def validate(self):
        (self.dataset / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        OwnerLabelIntakeTests.write_cases(self.dataset, self.cases)
        return validate_evaluation_dataset(self.dataset)

    def test_valid_owner_and_silver_cases_are_counted_but_never_gold(self):
        self.case["annotation"] = self.owner_label()
        report = self.validate()
        self.assertEqual(report.annotation_state_counts["owner_labeled"], 1)
        self.assertFalse(report.publishable_gold)

    def test_owner_label_rejections(self):
        for name, annotation, message in (
            ("labels differ", {**self.owner_label(), "labels": {"relevance": "unknown"}},
             "does not match final labels"),
            ("assisted", self.owner_label(model_assistance=True), "blind, human"),
            ("other owner", self.owner_label(owner_id="owner-2"), "blind, human"),
            ("stale hash", self.owner_label(content_sha256="0" * 64), "frozen-content"),
            ("model output", {**self.owner_label(), "generated_by_model": True},
             "cannot use model output"),
            ("mixed tiers", {**self.owner_label(), "reviews": [{"reviewer_id": "x"}]},
             "mixes owner_labeled"),
        ):
            with self.subTest(name):
                self.case["annotation"] = annotation
                with self.assertRaisesRegex(EvaluationDatasetError, message):
                    self.validate()
        self.case["annotation"] = self.owner_label()
        del self.manifest["annotation_protocol"]
        with self.assertRaisesRegex(EvaluationDatasetError, "requires manifest annotation_protocol"):
            self.validate()

    def test_silver_labels_stay_out_of_evaluation_splits(self):
        self.assertEqual(self.case["split"], "train")
        self.case["annotation"] = self.silver()
        self.assertEqual(self.validate().annotation_state_counts["algorithm_labeled"], 1)
        for name, annotation, split, message in (
            ("test split", self.silver(), "test", "only enter train/dev"),
            ("not marked generated", {**self.silver(), "generated_by_model": False}, "train",
             "must set generated_by_model"),
            ("no config hash", self.silver(config_sha256="abc"), "train", "config_sha256"),
        ):
            with self.subTest(name):
                self.case["annotation"] = annotation
                original = self.case["split"]
                self.case["split"] = split
                try:
                    with self.assertRaisesRegex(EvaluationDatasetError, message):
                        self.validate()
                finally:
                    self.case["split"] = original

    def test_tier_provenance_cannot_ride_on_other_states(self):
        self.case["annotation"] = {
            "state": "unlabeled", "generated_by_model": False, "labels": {},
            "owner_label": self.owner_label()["owner_label"],
        }
        with self.assertRaisesRegex(EvaluationDatasetError, "owner_label outside owner tier"):
            self.validate()
        self.case["annotation"] = {**self.owner_label(), "labeler": self.silver()["labeler"]}
        with self.assertRaisesRegex(EvaluationDatasetError, "labeler outside silver tier"):
            self.validate()

    def test_owner_labels_do_not_shrink_gold_impact_gaps(self):
        before = validate_evaluation_dataset(DATASETS / "foundation-v1").impact_annotations
        self.case["annotation"] = self.owner_label()
        self.assertEqual(self.validate().impact_annotations, before)


if __name__ == "__main__":
    unittest.main()
