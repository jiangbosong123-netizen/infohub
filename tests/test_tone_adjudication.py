import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.tone_adjudication import adjudicate_tone
from app.tone_evaluation import validate_tone_evaluation_dataset
from app.tone_review_intake import import_tone_review_batch


FIXTURE = Path(__file__).parents[1] / "evaluation/datasets/tone-contract-v1"


class ToneAdjudicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "tone-private"
        shutil.copytree(FIXTURE, source)
        cases = [json.loads(line) for line in (source / "cases.jsonl").read_text().splitlines()]
        self.case = cases[0]
        self.labels = self.case["annotation"]["labels"]
        self.labels["tone"]["evidence"][0]["quote"] = None
        self.case["annotation"] = {
            "state": "unlabeled",
            "generated_by_model": False,
            "labels": {},
        }
        self.case["text_storage"] = "restricted_reference"
        self.case["object_ref"] = "private-db:document-version/1"
        self.case.pop("text")
        (source / "cases.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
            encoding="utf-8",
        )
        first = self._review(source, "reviewer-a", self.labels, "tone-review-one")
        alternate = json.loads(json.dumps(self.labels))
        alternate["tone"]["polarity"] = "mixed"
        alternate["tone"]["intensity_band"] = "moderate"
        alternate["tone"]["phenomena"] = sorted(
            set(alternate["tone"]["phenomena"] + ["mixed"])
        )
        self.reviewed = self._review(first, "reviewer-b", alternate, "tone-review-two")

    def _review(self, source: Path, reviewer: str, labels: dict, version: str) -> Path:
        batch = self.root / f"batch-{reviewer}"
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
        (batch / "reviews.jsonl").write_text(
            json.dumps(
                {
                    "case_id": self.case["case_id"],
                    "content_sha256": self.case["content_sha256"],
                    "recorded_at": "2026-09-28T12:00:00Z",
                    "labels": labels,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.root / version
        import_tone_review_batch(source, batch, output, dataset_version=version)
        return output

    def decision_batch(
        self,
        adjudicator="reviewer-c",
        *,
        labels=None,
        digest=None,
        recorded_at="2026-09-28T13:00:00Z",
        reason="Evidence and target support the positive reading.",
        model_assistance=False,
        manifest_hash=None,
    ) -> Path:
        batch = self.root / f"decision-{adjudicator}-{len(list(self.root.glob('decision-*')))}"
        batch.mkdir()
        report = validate_tone_evaluation_dataset(self.reviewed)
        (batch / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "human-tone-adjudication-batch-v1",
                    "source_dataset_version": report.dataset_version,
                    "source_manifest_sha256": manifest_hash
                    or hashlib.sha256((self.reviewed / "manifest.json").read_bytes()).hexdigest(),
                    "source_cases_sha256": hashlib.sha256(
                        (self.reviewed / "cases.jsonl").read_bytes()
                    ).hexdigest(),
                    "adjudicator_id": adjudicator,
                    "source": "human",
                    "model_assistance": model_assistance,
                }
            ),
            encoding="utf-8",
        )
        (batch / "decisions.jsonl").write_text(
            json.dumps(
                {
                    "case_id": self.case["case_id"],
                    "content_sha256": digest or self.case["content_sha256"],
                    "recorded_at": recorded_at,
                    "labels": labels if labels is not None else self.labels,
                    "reason": reason,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return batch

    def test_third_person_freezes_final_label_without_claiming_release(self):
        batch = self.decision_batch()
        output = self.root / "adjudicated"
        report = adjudicate_tone(
            self.reviewed, batch, output, dataset_version="tone-adjudicated-v1"
        )
        self.assertEqual(report.adjudicated_in_batch, 1)
        self.assertEqual(report.total_adjudicated, 1)
        self.assertFalse(report.publishable_tone_gold)
        case = json.loads((output / "cases.jsonl").read_text().splitlines()[0])
        self.assertEqual(case["annotation"]["state"], "adjudicated")
        self.assertEqual(case["annotation"]["labels"], self.labels)
        self.assertEqual(len(case["annotation"]["reviews"]), 2)
        self.assertEqual(case["annotation"]["adjudication"]["adjudicator_id"], "reviewer-c")
        self.assertNotIn(
            "tone_evidence_review", json.loads((output / "manifest.json").read_text())
        )
        with self.assertRaisesRegex(EvaluationDatasetError, "new immutable directory"):
            adjudicate_tone(
                self.reviewed, batch, output, dataset_version="tone-adjudicated-v1"
            )

    def test_reviewer_cannot_adjudicate_and_decision_must_follow_reviews(self):
        same = self.decision_batch(adjudicator="reviewer-a")
        with self.assertRaisesRegex(EvaluationDatasetError, "third person"):
            adjudicate_tone(
                self.reviewed, same, self.root / "same", dataset_version="tone-same-v1"
            )
        early = self.decision_batch(recorded_at="2026-09-28T11:00:00Z")
        with self.assertRaisesRegex(EvaluationDatasetError, "precedes a human review"):
            adjudicate_tone(
                self.reviewed, early, self.root / "early", dataset_version="tone-early-v1"
            )

    def test_stale_content_model_assistance_and_source_hash_fail_closed(self):
        stale = self.decision_batch(digest="0" * 64)
        stale_output = self.root / "stale"
        with self.assertRaisesRegex(EvaluationDatasetError, "content hash differs"):
            adjudicate_tone(
                self.reviewed, stale, stale_output, dataset_version="tone-stale-v1"
            )
        self.assertFalse(stale_output.exists())
        assisted = self.decision_batch(model_assistance=True)
        with self.assertRaisesRegex(EvaluationDatasetError, "no-model attestation"):
            adjudicate_tone(
                self.reviewed,
                assisted,
                self.root / "assisted",
                dataset_version="tone-assisted-v1",
            )
        wrong = self.decision_batch(manifest_hash="0" * 64)
        with self.assertRaisesRegex(EvaluationDatasetError, "does not match frozen dataset"):
            adjudicate_tone(
                self.reviewed, wrong, self.root / "wrong", dataset_version="tone-wrong-v1"
            )

    def test_invalid_tone_label_reason_and_extra_fields_are_rejected(self):
        bad_labels = json.loads(json.dumps(self.labels))
        bad_labels["tone"]["polarity"] = "bullish"
        invalid = self.decision_batch(labels=bad_labels)
        with self.assertRaisesRegex(EvaluationDatasetError, "unsupported polarity"):
            adjudicate_tone(
                self.reviewed, invalid, self.root / "invalid", dataset_version="tone-invalid-v1"
            )
        empty_reason = self.decision_batch(reason=" ")
        with self.assertRaisesRegex(EvaluationDatasetError, "requires reason"):
            adjudicate_tone(
                self.reviewed,
                empty_reason,
                self.root / "empty-reason",
                dataset_version="tone-empty-v1",
            )
        extra = self.decision_batch()
        row = json.loads((extra / "decisions.jsonl").read_text())
        row["hidden"] = "side channel"
        (extra / "decisions.jsonl").write_text(json.dumps(row) + "\n")
        with self.assertRaisesRegex(EvaluationDatasetError, "invalid fields"):
            adjudicate_tone(
                self.reviewed, extra, self.root / "extra", dataset_version="tone-extra-v1"
            )


if __name__ == "__main__":
    unittest.main()
