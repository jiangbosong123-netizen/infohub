import json
import re
import unittest

from fastapi.testclient import TestClient

from app import silver_relevance_labeler as silver
from app.ai import pipeline
from app.evaluation import EvaluationDatasetError, owner_protocol, validate_evaluation_dataset
from app.owner_label_console import OwnerLabelSession, create_owner_label_console
from app.owner_label_intake import redefine_owner_dataset
from app.relevance_definition import CURRENT_DEFINITION, DEFINITIONS
from tests.test_owner_recheck import OwnerRecheckFixture

V1, V2 = "relevance-definition-v1", "relevance-definition-v2"


def _always_relevant(messages):
    ids = [row["id"] for row in json.loads(messages[1]["content"])]
    return json.dumps([{"id": i, "label": "relevant"} for i in ids]), {"prompt_tokens": 1, "completion_tokens": 1}


class RelevanceDefinitionChangeTests(OwnerRecheckFixture):
    def redefine(self, source=None, output="owner-v2", definition=V2):
        return redefine_owner_dataset(
            "relevance", source or self.dataset, self.root / output, dataset_version=f"recheck-{output}",
            definition=definition, decision="D26 relevance includes markets and macro", decided_at="2026-10-11",
        )

    def manifest(self, dataset):
        return json.loads((dataset / "manifest.json").read_text())

    def test_the_owner_decision_changes_the_definition_and_nothing_else(self):
        result = self.redefine()
        output = self.root / "owner-v2"
        self.assertEqual((result["from_definition"], result["to_definition"]), (V1, V2))
        manifest = self.manifest(output)
        self.assertEqual(owner_protocol(manifest)["label_definition"], V2)
        self.assertEqual(manifest["label_definition_change"]["decision"], "D26 relevance includes markets and macro")
        self.assertTrue(manifest["label_definition_change"]["labels_unchanged"])
        self.assertEqual(manifest["parent_dataset_version"], "recheck-owner-v1")
        self.assertEqual([case["annotation"] for case in self.cases(output)],
                         [case["annotation"] for case in self.cases(self.dataset)])
        self.assertEqual(validate_evaluation_dataset(output).dataset_version, "recheck-owner-v2")

    def test_the_recheck_then_shows_and_records_the_new_definition(self):
        before = OwnerLabelSession(self.dataset, self.database, self.root / "drafts-v1",
                                   owner_id="owner", mode="recheck")
        self.assertEqual(before.definition, V1)  # a dataset keeps the definition it declares
        self.redefine(output="redefined")
        self.dataset = self.root / "redefined"
        session = OwnerLabelSession(self.dataset, self.database, self.root / "drafts",
                                    owner_id="owner", mode="recheck")
        self.assertEqual(session.definition, V2)
        client = TestClient(create_owner_label_console(session, csrf_token="t"), base_url="http://127.0.0.1")
        page = client.get("/", follow_redirects=True).text
        self.assertIn(DEFINITIONS[V2].question, page)
        self.assertNotIn(DEFINITIONS[V1].question, page)
        rechecked = self.recheck_via_console()  # its batch must carry v2 to be accepted
        self.assertEqual(owner_protocol(self.manifest(rechecked))["label_definition"], V2)

    def test_refusals(self):
        for definition, message in ((V1, "already uses"), ("relevance-definition-v9", "unknown relevance")):
            with self.subTest(definition=definition), self.assertRaisesRegex(EvaluationDatasetError, message):
                self.redefine(definition=definition)
        with self.assertRaisesRegex(EvaluationDatasetError, "no single-owner protocol"):
            self.redefine(source=self.root / "unlabeled")
        rechecked = self.recheck_via_console()
        with self.assertRaisesRegex(EvaluationDatasetError, "under the old definition"):
            self.redefine(source=rechecked, output="owner-v3")


class SilverLabelerDefinitionTests(OwnerRecheckFixture):
    def label(self, dataset, output, **options):
        return silver.label_cases(dataset, self.database, self.root / output, complete=_always_relevant,
                                  model="glm-4.6", base_url="https://open.bigmodel.cn/api/paas/v4",
                                  max_calls=40, **options)

    def test_the_v1_labeler_is_byte_for_byte_the_one_that_ran(self):
        config = silver.labeler_config("glm-4.6", "https://open.bigmodel.cn/api/paas/v4", V1)
        # Recorded by the 2026-10-04 run in its labeler.json.
        self.assertEqual(config["system_prompt_sha256"],
                         "41baa9defbe6f2e8fd57f3d6998fe2a05275fb0f2cc6801efe9de1cb28dc4c63")
        self.assertEqual(silver.config_sha256(config),
                         "dc19372a13205b5c915698e7c648e4d6cad8723d461ff6066e0a41b436268070")
        self.assertEqual(silver.labeler_config("m", "https://h", V2)["labeler_version"], "relevance-llm-v2")
        self.assertIn("财经与市场", silver.SYSTEM_PROMPTS[V2])

    def test_labels_are_never_graded_across_definitions(self):
        self.label(self.dataset, "v1-labels")
        silver.build_prediction_run(self.root / "v1-labels", self.dataset, self.root / "v1-run")
        redefine_owner_dataset("relevance", self.dataset, self.root / "owner-v2", dataset_version="recheck-owner-v2",
                               definition=V2, decision="D26", decided_at="2026-10-11")
        with self.assertRaisesRegex(silver.SilverLabelerError, "different relevance definitions"):
            silver.build_prediction_run(self.root / "v1-labels", self.root / "owner-v2", self.root / "mixed-run")

    def test_all_splits_predicts_owner_labeled_cases_for_comparison(self):
        redefine_owner_dataset("relevance", self.dataset, self.root / "owner-v2", dataset_version="recheck-owner-v2",
                               definition=V2, decision="D26", decided_at="2026-10-11")
        tests = sum(case["split"] == "test" for case in self.cases(self.dataset))
        self.assertEqual(self.label(self.root / "owner-v2", "test-only")["cases"], tests)
        everything = self.label(self.root / "owner-v2", "all", all_splits=True)
        self.assertEqual(everything["cases"], len(self.cases(self.dataset)))
        manifest = json.loads((self.root / "all" / "labeler.json").read_text())
        self.assertEqual((manifest["config"]["label_definition"], manifest["config"]["labeler_version"]),
                         (V2, "relevance-llm-v2"))
        silver.build_prediction_run(self.root / "all", self.root / "owner-v2", self.root / "v2-run")


class CurationPromptTests(unittest.TestCase):
    def test_the_curation_prompt_follows_the_current_definition(self):
        self.assertEqual(CURRENT_DEFINITION, V2)
        self.assertIn("货币政策与央行", pipeline.SYSTEM_PROMPT)
        self.assertNotIn("「美联储加息预期」不算", pipeline.SYSTEM_PROMPT)
        self.assertRegex(pipeline.SYSTEM_PROMPT, re.compile("只是与关注公司同名"))
