import copy
import unittest

from app.report_drafts import render_validated_draft


MANIFEST = {
    "schema_version": "infohub.report-input/1.0",
    "report_type": "calendar_daily",
    "date": "2026-09-18",
    "point_in_time_status": "legacy_mutable_unverified",
    "coverage": {"selected_by_channel": {"stock": 1, "ai": 1}},
    "items": [
        {"item_id": 11, "document_version_id": "doc-stock", "channel": "stock",
         "source_name": "Market Source", "url": "https://example.test/stock"},
        {"item_id": 12, "document_version_id": "doc-ai", "channel": "ai",
         "source_name": "AI Source", "url": "https://example.test/ai"},
    ],
}
DRAFT = {
    "schema_version": "infohub.report-draft/1.0",
    "date": "2026-09-18",
    "sections": [
        {"channel": "stock", "claims": [
            {"text": "公司发布季度数据", "input_ordinals": [0]},
        ]},
        {"channel": "ai", "claims": [
            {"text": "模型发布了更新", "input_ordinals": [1]},
        ]},
    ],
}


class ReportDraftTests(unittest.TestCase):
    def test_valid_draft_renders_only_manifest_source_links(self):
        content, citations, coverage = render_validated_draft(MANIFEST, DRAFT)
        self.assertIn("公司发布季度数据【1】", content)
        self.assertIn("https://example.test/stock", content)
        self.assertIn("https://example.test/ai", content)
        self.assertEqual([row["input_ordinal"] for row in citations], [0, 1])
        self.assertEqual(coverage["citation_count"], 2)
        self.assertEqual(coverage["claim_entailment_status"], "not_automatically_verified")
        self.assertEqual(coverage["point_in_time_status"], MANIFEST["point_in_time_status"])

    def test_draft_claim_must_have_known_same_channel_source(self):
        for references in ([], [2], [True], [0, 0], [1]):
            with self.subTest(references=references):
                draft = copy.deepcopy(DRAFT)
                draft["sections"][0]["claims"][0]["input_ordinals"] = references
                with self.assertRaises(ValueError):
                    render_validated_draft(MANIFEST, draft)

    def test_shape_date_channel_and_size_are_closed(self):
        variants = []
        changed = copy.deepcopy(DRAFT)
        changed["date"] = "2026-09-19"
        variants.append(changed)
        changed = copy.deepcopy(DRAFT)
        changed["sections"].append(copy.deepcopy(changed["sections"][0]))
        variants.append(changed)
        changed = copy.deepcopy(DRAFT)
        changed["sections"][0]["claims"] *= 13
        variants.append(changed)
        changed = copy.deepcopy(DRAFT)
        changed["sections"][0]["claims"][0]["extra"] = "unreviewed"
        variants.append(changed)
        changed = copy.deepcopy(DRAFT)
        changed["sections"][0]["claims"][0]["text"] = "a" * 281
        variants.append(changed)
        for draft in variants:
            with self.subTest(draft=draft):
                with self.assertRaises(ValueError):
                    render_validated_draft(MANIFEST, draft)

    def test_arbitrary_links_and_control_text_are_rejected(self):
        for text in ("Go https://attacker.test", "line\nbreak", "\x00hidden"):
            with self.subTest(text=text):
                draft = copy.deepcopy(DRAFT)
                draft["sections"][0]["claims"][0]["text"] = text
                with self.assertRaises(ValueError):
                    render_validated_draft(MANIFEST, draft)
        manifest = copy.deepcopy(MANIFEST)
        manifest["items"][0]["url"] = "javascript:alert(1)"
        with self.assertRaises(ValueError):
            render_validated_draft(manifest, DRAFT)

    def test_markup_is_escaped_even_with_valid_citation(self):
        draft = copy.deepcopy(DRAFT)
        draft["sections"][0]["claims"][0]["text"] = "<script>alert(1)</script> **claim**"
        content, _, _ = render_validated_draft(MANIFEST, draft)
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)
        self.assertIn(r"\*\*claim\*\*", content)


if __name__ == "__main__":
    unittest.main()
