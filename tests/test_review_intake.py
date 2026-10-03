import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import evaluation_review_intake, impact_review_intake, tone_review_intake
from app.evaluation import EvaluationDatasetError
from app.review_intake import run_review_intake


DATASETS = Path(__file__).parents[1] / "evaluation/datasets"


def _relevance(case: dict) -> dict:
    return {"relevance": "unknown"}


def _tone(case: dict) -> dict:
    labels = case["annotation"]["labels"]
    labels["tone"]["evidence"][0]["quote"] = None
    return labels


def _impact(case: dict) -> dict:
    for evidence in case["event_evidence"]:
        evidence["quote"] = None
    return case["annotation"]["labels"]


TASKS = (
    (evaluation_review_intake.TASK, "foundation-v1", _relevance),
    (tone_review_intake.TASK, "tone-contract-v1", _tone),
    (impact_review_intake.TASK, "impact-contract-v1", _impact),
)


class SharedReviewIntakeTests(unittest.TestCase):
    """Every task must enforce the same intake rules; only labels and schema names differ."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def prepare(self, fixture: str, restrict) -> tuple[Path, dict, dict]:
        dataset = self.root / f"{fixture}-private"
        shutil.copytree(DATASETS / fixture, dataset)
        cases = [
            json.loads(line)
            for line in (dataset / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        labels = restrict(cases[0])
        cases[0]["annotation"] = {"state": "unlabeled", "generated_by_model": False, "labels": {}}
        cases[0]["text_storage"] = "restricted_reference"
        cases[0]["object_ref"] = "private-db:document-version/1"
        cases[0].pop("text")
        (dataset / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
            encoding="utf-8",
        )
        return dataset, cases[0], labels

    def batch(self, task, dataset: Path, case: dict, labels: dict, name: str) -> Path:
        batch = self.root / f"batch-{dataset.name}-{name}"
        batch.mkdir()
        (batch / "manifest.json").write_text(json.dumps({
            "schema_version": task.batch_version,
            "source_dataset_version": task.validate_dataset(dataset).dataset_version,
            "source_manifest_sha256": hashlib.sha256(
                (dataset / "manifest.json").read_bytes()
            ).hexdigest(),
            "source_cases_sha256": hashlib.sha256(
                (dataset / "cases.jsonl").read_bytes()
            ).hexdigest(),
            "reviewer_id": f"{name}-reviewer",
            "source": "human",
            "independent": True,
            "model_assistance": False,
        }), encoding="utf-8")
        (batch / "reviews.jsonl").write_text(json.dumps({
            "case_id": case["case_id"],
            "content_sha256": case["content_sha256"],
            "recorded_at": "2026-10-03T12:00:00Z",
            "labels": labels,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        return batch

    def test_every_task_records_byte_exact_provenance(self):
        for task, fixture, restrict in TASKS:
            with self.subTest(task.batch_version):
                dataset, case, labels = self.prepare(fixture, restrict)
                batch = self.batch(task, dataset, case, labels, "accepted")
                output = self.root / f"{fixture}-reviewed"
                counts = run_review_intake(
                    task, dataset, batch, output, dataset_version=f"{fixture}-reviewed"
                )
                self.assertEqual((counts.one_review_cases, counts.publishable), (1, False))
                manifest = json.loads((output / "manifest.json").read_text())
                for key, path in {
                    "parent_manifest_sha256": dataset / "manifest.json",
                    "parent_cases_sha256": dataset / "cases.jsonl",
                    f"{task.manifest_prefix}_manifest_sha256": batch / "manifest.json",
                    f"{task.manifest_prefix}_cases_sha256": batch / "reviews.jsonl",
                }.items():
                    self.assertEqual(
                        manifest[key], hashlib.sha256(path.read_bytes()).hexdigest(), key
                    )

    def test_every_task_rejects_side_channels_and_public_batches(self):
        repository = Path(__file__).parents[1]
        for task, fixture, restrict in TASKS:
            dataset, case, labels = self.prepare(fixture, restrict)
            with self.subTest(task.batch_version, rule="manifest fields"):
                batch = self.batch(task, dataset, case, labels, "manifest-extra")
                manifest = json.loads((batch / "manifest.json").read_text())
                manifest["comment"] = "side channel"
                (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(EvaluationDatasetError, "manifest has invalid fields"):
                    run_review_intake(
                        task, dataset, batch, self.root / "out", dataset_version="out-v1"
                    )
            with self.subTest(task.batch_version, rule="row fields"):
                batch = self.batch(task, dataset, case, labels, "row-extra")
                row = json.loads((batch / "reviews.jsonl").read_text())
                row["comment"] = "side channel"
                (batch / "reviews.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(EvaluationDatasetError, "row has invalid fields"):
                    run_review_intake(
                        task, dataset, batch, self.root / "out", dataset_version="out-v1"
                    )
            with self.subTest(task.batch_version, rule="public batch"):
                with self.assertRaisesRegex(EvaluationDatasetError, "batch must be private"):
                    run_review_intake(
                        task, dataset, repository / "evaluation" / "datasets",
                        self.root / "out", dataset_version="out-v1",
                    )
            self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
    unittest.main()
