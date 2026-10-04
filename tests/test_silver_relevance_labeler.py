import json
import re
import unittest
from pathlib import Path

from app.evaluation import validate_evaluation_dataset
from app.owner_label_console import OwnerLabelSession
from app.silver_relevance_labeler import (
    NO_LABEL,
    SilverLabelerError,
    _parse,
    build_prediction_run,
    import_silver_labels,
    label_cases,
)
from tests.test_owner_recheck import OwnerRecheckFixture


class FakeModel:
    """Labels by item number parity; can be told to misbehave on chosen calls."""

    def __init__(self, garbage_calls=()):
        self.calls = 0
        self.garbage_calls = set(garbage_calls)
        self.payloads = []

    def __call__(self, messages):
        self.calls += 1
        payload = json.loads(messages[1]["content"])
        self.payloads.append(payload)
        if self.calls in self.garbage_calls:
            return "sorry, I cannot comply", {"prompt_tokens": 10, "completion_tokens": 3}
        rows = []
        for row in payload:
            number = int(re.search(r"(\d+)", row["title"]).group(1))
            rows.append({"id": row["id"], "label": "not_relevant" if number % 4 == 0 else "relevant"})
        return json.dumps(rows), {"prompt_tokens": 100, "completion_tokens": 20}


class SilverRelevanceLabelerTests(OwnerRecheckFixture):
    def setUp(self):
        super().setUp()
        # The owner-labeled dataset keeps only the test split; train/dev start unlabeled again.
        unlabeled = self.root / "unlabeled"
        test_only = {
            case["case_id"]: self.truth[case["case_id"]]
            for case in self.cases(unlabeled) if case["split"] == "test"
        }
        from app.owner_label_intake import import_owner_label_batch
        batch = self.owner_batch(unlabeled, "owner-label-batch-v1", test_only, "2026-09-01T00:00:00Z")
        self.owner = self.root / "owner-test-only"
        import_owner_label_batch("relevance", unlabeled, batch, self.owner,
                                 dataset_version="silver-owner-v1")
        self.test_ids = set(test_only)

    def label(self, model, name="labels", **kwargs):
        return label_cases(self.owner, self.database, self.root / name, complete=model,
                           model="fake-model", base_url="https://llm.test/v1",
                           max_calls=kwargs.pop("max_calls", 40), **kwargs)

    def test_labeler_is_evaluated_on_owner_test_then_imported_as_silver(self):
        model = FakeModel()
        summary = self.label(model)
        cases = self.cases(self.owner)
        expected = sum(1 for c in cases if c["split"] in {"train", "dev", "test"})
        self.assertEqual(summary["cases"], expected)
        self.assertEqual(summary["calls"], model.calls)
        for payload in model.payloads:
            for row in payload:  # only frozen source content is sent, never labels or model fields
                self.assertEqual(set(row), {"id", "title", "excerpt"})
        manifest = json.loads((self.root / "labels" / "labeler.json").read_text())
        self.assertNotIn("key", json.dumps(manifest["config"]).lower())
        self.assertEqual(manifest["config"]["endpoint_host"], "llm.test")

        report = build_prediction_run(self.root / "labels", self.owner, self.root / "eval-run")
        self.assertEqual((report["annotation_tier"], report["split"]), ("owner", "test"))
        self.assertEqual(report["total"], len(self.test_ids))
        self.assertEqual(report["missing_predictions"], 0)
        self.assertEqual(report["accuracy"], 1.0)  # the fake mirrors the owner's rule

        result = import_silver_labels(self.root / "labels", self.root / "eval-run", self.owner,
                                      self.root / "silver-v1", dataset_version="silver-v1")
        self.assertEqual(result["imported"], expected - len(self.test_ids))
        self.assertTrue(result["evaluation"]["meets_relevance_macro_f1_target"])
        states = validate_evaluation_dataset(self.root / "silver-v1").annotation_state_counts
        self.assertEqual(states["owner_labeled"], len(self.test_ids))
        self.assertEqual(states["algorithm_labeled"], result["imported"])
        for case in self.cases(self.root / "silver-v1"):
            if case["split"] == "test":
                self.assertEqual(case["annotation"]["state"], "owner_labeled")
            else:
                self.assertEqual(case["annotation"]["labeler"]["labeler_version"], "relevance-llm-v1")
        silver_manifest = json.loads((self.root / "silver-v1" / "manifest.json").read_text())
        self.assertEqual(silver_manifest["silver_labeler_evaluation"]["total"], len(self.test_ids))

    def test_budget_and_dry_run_never_spend(self):
        model = FakeModel()
        with self.assertRaisesRegex(SilverLabelerError, "exceed the budget"):
            self.label(model, max_calls=2)
        self.assertEqual(model.calls, 0)
        self.assertFalse((self.root / "labels").exists())
        summary = self.label(model, name="dry", dry_run=True)
        self.assertEqual(model.calls, 0)
        self.assertEqual(summary["labels"][NO_LABEL], summary["cases"])
        with self.assertRaisesRegex(SilverLabelerError, "dry run"):
            build_prediction_run(self.root / "dry", self.owner, self.root / "dry-run")

    def test_malformed_output_is_retried_once_then_left_unlabeled(self):
        model = FakeModel(garbage_calls={1, 3, 4})
        summary = self.label(model)
        self.assertEqual(summary["failed_batches"], 1)  # batch 1 recovered, batch 2 failed twice
        self.assertEqual(summary["labels"][NO_LABEL], 20)
        record = json.loads((self.root / "labels" / "calls" / "0002.json").read_text())
        self.assertEqual(len(record["attempts"]), 2)
        self.assertTrue(all("error" in attempt for attempt in record["attempts"]))

    def test_injected_or_partial_responses_are_rejected(self):
        for text in (
            '[{"id": 1, "label": "relevant"}]',
            '[{"id": 1, "label": "relevant"}, {"id": 2, "label": "very relevant"}]',
            '[{"id": 1, "label": "relevant"}, {"id": 3, "label": "relevant"}]',
            '[{"id": 1, "label": "relevant", "note": "x"}, {"id": 2, "label": "unknown"}]',
        ):
            with self.subTest(text=text), self.assertRaises(SilverLabelerError):
                _parse(text, [1, 2])
        self.assertEqual(_parse('ok [{"id": 2, "label": "unknown"}, {"id": 1, "label": "relevant"}]', [1, 2]),
                         {1: "relevant", 2: "unknown"})

    def test_import_needs_matching_owner_evaluation_and_respects_owner_labels(self):
        self.label(FakeModel())
        build_prediction_run(self.root / "labels", self.owner, self.root / "eval-run")
        # Another configuration cannot borrow this evaluation.
        self.label(FakeModel(), name="labels-b")
        manifest = json.loads((self.root / "labels-b" / "labeler.json").read_text())
        run = json.loads((self.root / "eval-run" / "run.json").read_text())
        self.assertEqual(manifest["config_sha256"], run["method_config_sha256"])  # same config
        run["method_config_sha256"] = "0" * 64
        (self.root / "eval-run-other").mkdir()
        (self.root / "eval-run-other" / "run.json").write_text(json.dumps(run))
        with self.assertRaisesRegex(SilverLabelerError, "different labeler configuration"):
            import_silver_labels(self.root / "labels", self.root / "eval-run-other", self.owner,
                                 self.root / "x", dataset_version="x-v1")
        # The owner labels a train case after the labeler ran: the owner label wins.
        session = OwnerLabelSession(self.owner, self.database, self.root / "drafts", owner_id="owner")
        owner_case = next(c for c in session.order if session.by_id[c]["split"] == "train")
        session.record(owner_case, "unknown", session.by_id[owner_case]["content_sha256"])
        session.export(self.root / "owner-extra")
        from app.owner_label_intake import import_owner_label_batch
        import_owner_label_batch("relevance", self.owner, self.root / "owner-extra",
                                 self.root / "owner-v2", dataset_version="silver-owner-v2")
        with self.assertRaisesRegex(SilverLabelerError, "source dataset version"):
            import_silver_labels(self.root / "labels", self.root / "eval-run", self.root / "owner-v2",
                                 self.root / "y", dataset_version="y-v1")
        build_prediction_run(self.root / "labels", self.root / "owner-v2", self.root / "eval-run-v2")
        result = import_silver_labels(self.root / "labels", self.root / "eval-run-v2",
                                      self.root / "owner-v2", self.root / "silver-v2",
                                      dataset_version="silver-v2")
        self.assertEqual(result["skipped_owner_labeled"], 1)
        kept = next(c for c in self.cases(self.root / "silver-v2") if c["case_id"] == owner_case)
        self.assertEqual(kept["annotation"]["state"], "owner_labeled")


if __name__ == "__main__":
    unittest.main()
