import unittest

from app.analysis_runs import AnalysisRunError
from app.analysis_results import _validate_output
from app.curation_contracts import (
    CURATION_SCHEMAS,
    adapt_legacy_curation,
    validate_curation_data,
)


class CurationContractTests(unittest.TestCase):
    def test_legacy_compound_projection_splits_into_four_reviewable_tasks(self):
        envelopes = adapt_legacy_curation({
            "title": "Company launches product",
            "title_zh": "公司发布产品",
            "summary": "公司发布了一款新产品。",
            "raw_summary": "The company launched a new product.",
            "tmt": 1,
            "score": 78,
            "reason": "产品发布可能影响行业竞争。",
            "ai_cat": "product",
            "official": 1,
            "companies": '["example"]',
        }, subject_version_id="doc-v1", evidence_id="raw-1")
        self.assertEqual(set(envelopes), {"translation", "relevance", "summarization", "importance"})
        self.assertTrue(all(value["status"] == "needs_review" for value in envelopes.values()))
        self.assertTrue(envelopes["relevance"]["data"]["policy_override"])
        self.assertEqual(envelopes["importance"]["data"]["scale"], "editorial_importance_not_probability")
        self.assertEqual(envelopes["summarization"]["data"]["claims"][0]["evidence_ids"], ["raw-1"])

    def test_legacy_sentinels_become_refusal_instead_of_business_values(self):
        envelopes = adapt_legacy_curation({
            "title": "Filtered", "title_zh": "-", "summary": "", "raw_summary": "",
            "tmt": None, "score": -1, "reason": "", "ai_cat": "", "official": 0,
            "companies": "[]",
        }, subject_version_id="doc-v1", evidence_id="raw-1")
        self.assertEqual(envelopes["translation"]["status"], "refused")
        self.assertEqual(envelopes["importance"]["status"], "refused")
        self.assertEqual(envelopes["relevance"]["status"], "insufficient_evidence")
        self.assertEqual(envelopes["summarization"]["status"], "insufficient_evidence")
        self.assertNotIn("translated_title", envelopes["translation"]["data"])
        self.assertNotIn("score", envelopes["importance"]["data"])

    def test_claim_evidence_and_importance_semantics_are_strict(self):
        with self.assertRaisesRegex(AnalysisRunError, "unknown evidence"):
            validate_curation_data(
                task_type="summarization", schema_version=CURATION_SCHEMAS["summarization"],
                status="valid", allowed_evidence={"raw-1"},
                data={"summary": "A", "claims": [{"text": "A", "evidence_ids": ["raw-2"]}]},
            )
        with self.assertRaisesRegex(AnalysisRunError, "editorial importance"):
            validate_curation_data(
                task_type="importance", schema_version=CURATION_SCHEMAS["importance"],
                status="valid", allowed_evidence={"raw-1"},
                data={"score": 80, "rationale": "", "scale": "probability"},
            )

    def test_registered_schema_cannot_be_published_under_another_task(self):
        with self.assertRaisesRegex(AnalysisRunError, "does not match"):
            validate_curation_data(
                task_type="relevance", schema_version=CURATION_SCHEMAS["translation"],
                status="valid", allowed_evidence={"raw-1"}, data={},
            )

    def test_analysis_publication_layer_enforces_registered_contract(self):
        run = {
            "output_schema_version": CURATION_SCHEMAS["summarization"],
            "subject_type": "document",
            "subject_version_id": "doc-v1",
            "task_type": "summarization",
        }
        output = {
            "schema_version": CURATION_SCHEMAS["summarization"],
            "subject": {"type": "document", "version_id": "doc-v1"},
            "status": "valid",
            "evidence_ids": ["raw-1"],
            "data": {"summary": "Unsupported claim", "claims": [
                {"text": "Unsupported claim", "evidence_ids": ["raw-2"]}
            ]},
        }
        with self.assertRaisesRegex(AnalysisRunError, "unknown evidence"):
            _validate_output(run, output, {"raw-1"})


if __name__ == "__main__":
    unittest.main()
