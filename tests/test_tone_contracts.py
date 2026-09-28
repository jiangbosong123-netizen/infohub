import unittest

from app.analysis_results import _validate_output
from app.analysis_runs import AnalysisRunError
from app.analysis_contracts import validate_analysis_data
from app.tone_contracts import TONE_SCHEMA_V1, TONE_SCHEMA_VERSION, TONE_VOCABULARY_VERSION


def tone_data(**assessment_changes):
    assessment = {
        "speaker": {"kind": "quoted_person", "entity_id": "person:analyst", "label": "Analyst"},
        "target": {"entity_id": "organization:example", "type": "organization"},
        "aspect": "business_outlook",
        "polarity": "positive",
        "intensity": 0.7,
        "evidence": [{
            "evidence_id": "raw-1",
            "quote": "Demand improved.",
            "locator": {
                "type": "json_pointer",
                "json_pointer": "/source_record/summary",
                "start_offset": 10,
                "end_offset": 26,
                "offset_unit": "unicode_code_point",
            },
        }],
        "confidence": {
            "raw_confidence": 0.8,
            "calibrated_confidence": None,
            "calibration_version": None,
            "uncertainty_reason": None,
        },
    }
    assessment.update(assessment_changes)
    return {"vocabulary_version": TONE_VOCABULARY_VERSION, "assessments": [assessment]}


class ToneContractTests(unittest.TestCase):
    def test_review_only_output_is_strict_and_evidence_grounded(self):
        clean = validate_analysis_data(
            task_type="tone",
            schema_version=TONE_SCHEMA_VERSION,
            status="needs_review",
            data=tone_data(),
            allowed_evidence={"raw-1"},
        )
        assessment = clean["assessments"][0]
        self.assertEqual(assessment["polarity"], "positive")
        self.assertEqual(assessment["evidence"][0]["locator"]["start_offset"], 10)
        self.assertEqual(assessment["confidence"]["raw_confidence"], 0.8)

    def test_verbatim_quote_whitespace_is_preserved(self):
        evidence = [{
            "evidence_id": "raw-1", "quote": " Demand improved. ",
            "locator": {
                "type": "json_pointer", "json_pointer": "/source_record/summary",
                "start_offset": 9, "end_offset": 27,
                "offset_unit": "unicode_code_point",
            },
        }]
        clean = validate_analysis_data(
            task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
            data=tone_data(evidence=evidence), allowed_evidence={"raw-1"},
        )
        self.assertEqual(clean["assessments"][0]["evidence"][0]["quote"], " Demand improved. ")

    def test_quality_gate_blocks_valid_and_calibrated_claims(self):
        with self.assertRaisesRegex(AnalysisRunError, "quote and quality admission"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="valid",
                data=tone_data(), allowed_evidence={"raw-1"},
            )
        confidence = {
            "raw_confidence": 0.8,
            "calibrated_confidence": 0.75,
            "calibration_version": "calibration-v1",
            "uncertainty_reason": None,
        }
        with self.assertRaisesRegex(AnalysisRunError, "unavailable before calibration admission"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(confidence=confidence), allowed_evidence={"raw-1"},
            )

    def test_unknown_is_not_neutral_and_requires_an_explanation(self):
        unknown_confidence = {
            "raw_confidence": None,
            "calibrated_confidence": None,
            "calibration_version": None,
            "uncertainty_reason": "Speaker target is ambiguous.",
        }
        clean = validate_analysis_data(
            task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
            data=tone_data(
                polarity="unknown", intensity=None, confidence=unknown_confidence,
                target=None,
            ), allowed_evidence={"raw-1"},
        )
        self.assertEqual(clean["assessments"][0]["polarity"], "unknown")
        with self.assertRaisesRegex(AnalysisRunError, "uncertainty_reason"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(
                    polarity="unknown", intensity=None,
                    confidence={**unknown_confidence, "uncertainty_reason": None},
                ), allowed_evidence={"raw-1"},
            )

    def test_unknown_evidence_bad_offsets_duplicates_and_extra_fields_are_rejected(self):
        with self.assertRaisesRegex(AnalysisRunError, "unknown evidence ID"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(evidence=[{
                    "evidence_id": "raw-2", "quote": "A", "locator": {
                        "type": "json_pointer", "json_pointer": "/source_record/summary",
                        "start_offset": 0, "end_offset": 1,
                        "offset_unit": "unicode_code_point",
                    },
                }]), allowed_evidence={"raw-1"},
            )
        bad_span = [{
            "evidence_id": "raw-1", "quote": "Demand improved.",
            "locator": {
                "type": "json_pointer", "json_pointer": "/source_record/summary",
                "start_offset": 10, "end_offset": 25,
                "offset_unit": "unicode_code_point",
            },
        }]
        with self.assertRaisesRegex(AnalysisRunError, "match quote length"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(evidence=bad_span), allowed_evidence={"raw-1"},
            )
        duplicate = tone_data()["assessments"][0]["evidence"] * 2
        with self.assertRaisesRegex(AnalysisRunError, "duplicate evidence spans"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(evidence=duplicate), allowed_evidence={"raw-1"},
            )
        with self.assertRaisesRegex(AnalysisRunError, "unknown fields"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(model_guess=True), allowed_evidence={"raw-1"},
            )

    def test_nonpublishable_output_has_only_a_reason_code(self):
        clean = validate_analysis_data(
            task_type="tone", schema_version=TONE_SCHEMA_VERSION,
            status="insufficient_evidence", data={"reason_code": "no_verbatim_text"},
            allowed_evidence={"raw-1"},
        )
        self.assertEqual(clean, {"reason_code": "no_verbatim_text"})
        with self.assertRaisesRegex(AnalysisRunError, "unknown fields"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION,
                status="insufficient_evidence",
                data={"reason_code": "no_verbatim_text", "assessments": []},
                allowed_evidence={"raw-1"},
            )
        with self.assertRaisesRegex(AnalysisRunError, "unsupported status"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION,
                status="invented", data={"reason_code": "bad_status"},
                allowed_evidence={"raw-1"},
            )

    def test_registered_tone_schema_cannot_be_used_by_another_task(self):
        with self.assertRaisesRegex(AnalysisRunError, "does not match"):
            validate_analysis_data(
                task_type="impact", schema_version=TONE_SCHEMA_VERSION,
                status="needs_review", data=tone_data(), allowed_evidence={"raw-1"},
            )

    def test_v1_review_shape_remains_readable_but_cannot_be_prepared_for_new_runs(self):
        legacy = tone_data()
        legacy["assessments"][0]["evidence"] = [{
            "evidence_id": "raw-1", "quote": "Demand improved.",
            "start_offset": 10, "end_offset": 26,
        }]
        clean = validate_analysis_data(
            task_type="tone", schema_version=TONE_SCHEMA_V1, status="needs_review",
            data=legacy, allowed_evidence={"raw-1"},
        )
        self.assertEqual(clean["assessments"][0]["evidence"][0]["start_offset"], 10)
        with self.assertRaisesRegex(AnalysisRunError, "tone schema does not match"):
            validate_analysis_data(
                task_type="tone", schema_version="tone-draft/0.1",
                status="needs_review", data=tone_data(), allowed_evidence={"raw-1"},
            )

    def test_missing_target_cannot_carry_a_directional_claim(self):
        with self.assertRaisesRegex(AnalysisRunError, "without a target requires unknown"):
            validate_analysis_data(
                task_type="tone", schema_version=TONE_SCHEMA_VERSION, status="needs_review",
                data=tone_data(target=None), allowed_evidence={"raw-1"},
            )

    def test_analysis_envelope_accepts_review_only_typed_tone(self):
        run = {
            "output_schema_version": TONE_SCHEMA_VERSION,
            "subject_type": "document",
            "subject_version_id": "doc-v1",
            "task_type": "tone",
        }
        output = {
            "schema_version": TONE_SCHEMA_VERSION,
            "subject": {"type": "document", "version_id": "doc-v1"},
            "status": "needs_review",
            "evidence_ids": ["raw-1"],
            "data": tone_data(),
        }
        clean, evidence, status = _validate_output(run, output, {"raw-1"})
        self.assertEqual((evidence, status), (["raw-1"], "needs_review"))
        self.assertEqual(clean["data"]["vocabulary_version"], TONE_VOCABULARY_VERSION)

    def test_tone_subject_must_be_a_document_version(self):
        run = {
            "output_schema_version": TONE_SCHEMA_VERSION,
            "subject_type": "event",
            "subject_version_id": "event-v1",
            "task_type": "tone",
        }
        output = {
            "schema_version": TONE_SCHEMA_VERSION,
            "subject": {"type": "event", "version_id": "event-v1"},
            "status": "needs_review",
            "evidence_ids": ["raw-1"],
            "data": tone_data(),
        }
        with self.assertRaisesRegex(AnalysisRunError, "document version subject"):
            _validate_output(run, output, {"raw-1"})


if __name__ == "__main__":
    unittest.main()
