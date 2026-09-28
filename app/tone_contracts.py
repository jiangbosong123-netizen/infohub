from __future__ import annotations

"""Strict, pre-release contract for evidence-grounded document tone results."""

from collections.abc import Mapping

from .analysis_runs import AnalysisRunError

TONE_SCHEMA_V1 = "infohub.tone/1.0"
TONE_SCHEMA_VERSION = "infohub.tone/1.1"
TONE_SCHEMA_VERSIONS = {TONE_SCHEMA_V1, TONE_SCHEMA_VERSION}
TONE_VOCABULARY_VERSION = "tone-vocabulary-v1"

POLARITIES = {"positive", "negative", "neutral", "mixed", "unknown"}
SPEAKER_KINDS = {
    "author",
    "interviewee",
    "quoted_person",
    "quoted_organization",
    "unknown",
}
TARGET_TYPES = {
    "organization",
    "security",
    "person",
    "product",
    "model",
    "industry",
    "region",
    "macro_concept",
}
ASPECTS = {
    "product_capability",
    "business_outlook",
    "policy_stance",
    "valuation_view",
    "market_position",
    "management_quality",
    "social_impact",
    "other",
}


def _exact_keys(
    data: Mapping[str, object], required: set[str], optional: set[str] = set()
) -> None:
    missing = required - set(data)
    unknown = set(data) - required - optional
    if missing:
        raise AnalysisRunError(f"tone data is missing fields: {sorted(missing)}")
    if unknown:
        raise AnalysisRunError(f"tone data has unknown fields: {sorted(unknown)}")


def _text(value: object, field: str, maximum: int, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise AnalysisRunError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise AnalysisRunError(f"{field} is required")
    if len(cleaned) > maximum:
        raise AnalysisRunError(f"{field} exceeds {maximum} characters")
    return cleaned


def _probability(value: object, field: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise AnalysisRunError(f"{field} must be between 0 and 1")
    return float(value)


def _verbatim(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AnalysisRunError(f"{field} must be a string")
    if not value.strip():
        raise AnalysisRunError(f"{field} is required")
    if len(value) > maximum:
        raise AnalysisRunError(f"{field} exceeds {maximum} characters")
    return value


def _speaker(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("tone speaker must be an object")
    _exact_keys(value, {"kind", "entity_id", "label"})
    kind = _text(value["kind"], "tone speaker kind", 40)
    if kind not in SPEAKER_KINDS:
        raise AnalysisRunError("tone speaker kind is unsupported")
    entity_id = _text(value["entity_id"], "tone speaker entity_id", 128, nullable=True)
    label = _text(value["label"], "tone speaker label", 200, nullable=True)
    if kind == "unknown" and entity_id is not None:
        raise AnalysisRunError("unknown tone speaker cannot declare an entity_id")
    if kind != "unknown" and entity_id is None and label is None:
        raise AnalysisRunError("identified tone speaker requires an entity_id or label")
    return {"kind": kind, "entity_id": entity_id, "label": label}


def _target(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AnalysisRunError("tone target must be an object or null")
    _exact_keys(value, {"entity_id", "type"})
    target_type = _text(value["type"], "tone target type", 40)
    if target_type not in TARGET_TYPES:
        raise AnalysisRunError("tone target type is unsupported")
    return {
        "entity_id": _text(value["entity_id"], "tone target entity_id", 128),
        "type": target_type,
    }


def _offsets(value: Mapping[str, object], quote: str) -> tuple[int, int]:
    start = value["start_offset"]
    end = value["end_offset"]
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise AnalysisRunError("tone evidence start_offset must be a non-negative integer")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise AnalysisRunError("tone evidence end_offset must be greater than start_offset")
    if end - start != len(quote):
        raise AnalysisRunError("tone evidence offsets must match quote length")
    return start, end


def _json_locator(value: object, quote: str) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("tone evidence locator must be an object")
    _exact_keys(
        value, {"type", "json_pointer", "start_offset", "end_offset", "offset_unit"}
    )
    if value["type"] != "json_pointer":
        raise AnalysisRunError("tone evidence locator type must be json_pointer")
    if value["offset_unit"] != "unicode_code_point":
        raise AnalysisRunError("tone evidence offset_unit must be unicode_code_point")
    pointer = _text(value["json_pointer"], "tone evidence json_pointer", 1_000)
    if not pointer.startswith("/"):
        raise AnalysisRunError("tone evidence json_pointer must start with /")
    start, end = _offsets(value, quote)
    return {
        "type": "json_pointer",
        "json_pointer": pointer,
        "start_offset": start,
        "end_offset": end,
        "offset_unit": "unicode_code_point",
    }


def _evidence(value: object, allowed_evidence: set[str], schema_version: str) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("tone evidence span must be an object")
    if schema_version == TONE_SCHEMA_V1:
        _exact_keys(value, {"evidence_id", "quote", "start_offset", "end_offset"})
    else:
        _exact_keys(value, {"evidence_id", "quote", "locator"})
    evidence_id = _text(value["evidence_id"], "tone evidence_id", 128)
    if evidence_id not in allowed_evidence:
        raise AnalysisRunError(f"tone assessment references unknown evidence ID: {evidence_id}")
    quote = _verbatim(value["quote"], "tone evidence quote", 2_000)
    if schema_version == TONE_SCHEMA_V1:
        start, end = _offsets(value, quote)
        return {
            "evidence_id": evidence_id,
            "quote": quote,
            "start_offset": start,
            "end_offset": end,
        }
    return {
        "evidence_id": evidence_id,
        "quote": quote,
        "locator": _json_locator(value["locator"], quote),
    }


def _confidence(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("tone confidence must be an object")
    _exact_keys(
        value,
        {"raw_confidence", "calibrated_confidence", "calibration_version", "uncertainty_reason"},
    )
    raw = _probability(value["raw_confidence"], "tone raw_confidence", nullable=True)
    calibrated = _probability(
        value["calibrated_confidence"], "tone calibrated_confidence", nullable=True
    )
    calibration_version = _text(
        value["calibration_version"], "tone calibration_version", 100, nullable=True
    )
    uncertainty_reason = _text(
        value["uncertainty_reason"], "tone uncertainty_reason", 500, nullable=True
    )
    if (calibrated is None) != (calibration_version is None):
        raise AnalysisRunError(
            "tone calibrated_confidence and calibration_version must be declared together"
        )
    if calibrated is not None:
        raise AnalysisRunError("calibrated tone confidence is unavailable before calibration admission")
    return {
        "raw_confidence": raw,
        "calibrated_confidence": None,
        "calibration_version": None,
        "uncertainty_reason": uncertainty_reason,
    }


def _assessment(value: object, allowed_evidence: set[str], schema_version: str) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("tone assessment must be an object")
    _exact_keys(
        value,
        {"speaker", "target", "aspect", "polarity", "intensity", "evidence", "confidence"},
    )
    aspect = _text(value["aspect"], "tone aspect", 80)
    if aspect not in ASPECTS:
        raise AnalysisRunError("tone aspect is not in the declared vocabulary")
    polarity = _text(value["polarity"], "tone polarity", 20)
    if polarity not in POLARITIES:
        raise AnalysisRunError("tone polarity is unsupported")
    intensity = _probability(value["intensity"], "tone intensity", nullable=True)
    confidence = _confidence(value["confidence"])
    if polarity == "unknown":
        if intensity is not None:
            raise AnalysisRunError("unknown tone polarity requires null intensity")
        if confidence["uncertainty_reason"] is None:
            raise AnalysisRunError("unknown tone polarity requires an uncertainty_reason")
    elif intensity is None:
        raise AnalysisRunError("known tone polarity requires intensity")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise AnalysisRunError("tone assessment evidence must be a non-empty list")
    clean_evidence = [_evidence(item, allowed_evidence, schema_version) for item in evidence]
    keys = [
        (
            item["evidence_id"],
            item.get("locator", {}).get("json_pointer"),
            item.get("start_offset", item.get("locator", {}).get("start_offset")),
            item.get("end_offset", item.get("locator", {}).get("end_offset")),
        )
        for item in clean_evidence
    ]
    if len(keys) != len(set(keys)):
        raise AnalysisRunError("tone assessment contains duplicate evidence spans")
    target = _target(value["target"])
    if target is None and polarity != "unknown":
        raise AnalysisRunError("tone assessment without a target requires unknown polarity")
    return {
        "speaker": _speaker(value["speaker"]),
        "target": target,
        "aspect": aspect,
        "polarity": polarity,
        "intensity": intensity,
        "evidence": clean_evidence,
        "confidence": confidence,
    }


def validate_tone_data(
    *, schema_version: str, status: str, data: object, allowed_evidence: set[str]
) -> dict:
    """Validate tone output while the task remains review-only and uncalibrated."""
    if not isinstance(data, Mapping):
        raise AnalysisRunError("tone data must be an object")
    if status not in {"valid", "needs_review", "insufficient_evidence", "refused"}:
        raise AnalysisRunError("tone output has an unsupported status")
    if status not in {"valid", "needs_review"}:
        _exact_keys(data, {"reason_code"})
        return {"reason_code": _text(data["reason_code"], "tone reason_code", 100)}
    if status == "valid":
        raise AnalysisRunError("tone output cannot be valid before quote and quality admission")
    _exact_keys(data, {"vocabulary_version", "assessments"})
    if data["vocabulary_version"] != TONE_VOCABULARY_VERSION:
        raise AnalysisRunError("tone vocabulary_version is unsupported")
    assessments = data["assessments"]
    if not isinstance(assessments, list) or not assessments:
        raise AnalysisRunError("tone assessments must be a non-empty list")
    clean = [_assessment(item, allowed_evidence, schema_version) for item in assessments]
    return {"vocabulary_version": TONE_VOCABULARY_VERSION, "assessments": clean}


def referenced_tone_entities(data: Mapping[str, object]) -> list[tuple[str, str | None, str]]:
    """Return entity references from already validated review data."""
    references: list[tuple[str, str | None, str]] = []
    assessments = data.get("assessments")
    if not isinstance(assessments, list):
        return references
    speaker_types = {
        "interviewee": "person",
        "quoted_person": "person",
        "quoted_organization": "organization",
    }
    for index, assessment in enumerate(assessments):
        if not isinstance(assessment, Mapping):
            continue
        speaker = assessment.get("speaker")
        if isinstance(speaker, Mapping) and isinstance(speaker.get("entity_id"), str):
            references.append((
                speaker["entity_id"], speaker_types.get(speaker.get("kind")),
                f"assessments[{index}].speaker.entity_id",
            ))
        target = assessment.get("target")
        if isinstance(target, Mapping) and isinstance(target.get("entity_id"), str):
            references.append((
                target["entity_id"],
                target.get("type") if isinstance(target.get("type"), str) else None,
                f"assessments[{index}].target.entity_id",
            ))
    return references
