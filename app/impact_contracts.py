from __future__ import annotations

"""Strict, review-only contract for evidence-grounded event impact results."""

from collections.abc import Mapping

from .analysis_runs import AnalysisRunError

IMPACT_SCHEMA_VERSION = "impact/1.0"
IMPACT_VALIDATOR_VERSION = "impact-event-evidence-validator-v1"

TARGET_TYPES = {
    "organization", "security", "person", "product", "model", "industry",
    "region", "macro_concept",
}
ASPECTS = {
    "revenue", "operating_cost", "margin", "capex", "funding", "supply",
    "demand", "regulatory_constraint", "employment", "other",
}
DIRECTIONS = {"positive", "negative", "neutral", "mixed", "unknown"}
HORIZONS = {
    "immediate": (0, 7),
    "quarter": (8, 90),
    "long_term": (91, 730),
    "unspecified": (None, None),
}


def _exact_keys(
    data: Mapping[str, object], required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required - set(data)
    unknown = set(data) - required - optional
    if missing:
        raise AnalysisRunError(f"impact data is missing fields: {sorted(missing)}")
    if unknown:
        raise AnalysisRunError(f"impact data has unknown fields: {sorted(unknown)}")


def _text(
    value: object, field: str, maximum: int, *, nullable: bool = False
) -> str | None:
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


def _target(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("impact target must be an object")
    _exact_keys(value, {"entity_id", "type"})
    target_type = _text(value["type"], "impact target type", 40)
    if target_type not in TARGET_TYPES:
        raise AnalysisRunError("impact target type is unsupported")
    return {
        "entity_id": _text(value["entity_id"], "impact target entity_id", 128),
        "type": target_type,
    }


def _horizon(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("impact horizon must be an object")
    _exact_keys(value, {"bucket", "min_days", "max_days"})
    bucket = _text(value["bucket"], "impact horizon bucket", 40)
    expected = HORIZONS.get(bucket)
    if expected is None:
        raise AnalysisRunError("impact horizon bucket is unsupported")
    if any(isinstance(item, bool) for item in (value["min_days"], value["max_days"])):
        raise AnalysisRunError("impact horizon boundaries must be integers or null")
    if (value["min_days"], value["max_days"]) != expected:
        raise AnalysisRunError("impact horizon boundaries do not match the declared bucket")
    return {"bucket": bucket, "min_days": expected[0], "max_days": expected[1]}


def _evidence_ids(
    value: object, field: str, allowed_evidence: set[str], *, required: bool
) -> list[str]:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 128
            for item in value
        )
    ):
        raise AnalysisRunError(f"{field} must be a string list")
    if required and not value:
        raise AnalysisRunError(f"{field} must not be empty")
    if len(value) != len(set(value)):
        raise AnalysisRunError(f"{field} contains duplicate evidence IDs")
    unknown = set(value) - allowed_evidence
    if unknown:
        raise AnalysisRunError(f"{field} references unknown evidence IDs: {sorted(unknown)[:5]}")
    return list(value)


def _confidence(value: Mapping[str, object]) -> dict:
    raw = _probability(value["raw_confidence"], "impact raw_confidence", nullable=True)
    calibrated = _probability(
        value["calibrated_confidence"], "impact calibrated_confidence", nullable=True
    )
    calibration = _text(
        value["calibration_version"], "impact calibration_version", 100, nullable=True
    )
    uncertainty = _text(
        value["uncertainty_reason"], "impact uncertainty_reason", 500, nullable=True
    )
    if (calibrated is None) != (calibration is None):
        raise AnalysisRunError(
            "impact calibrated_confidence and calibration_version must be declared together"
        )
    if calibrated is not None:
        raise AnalysisRunError("calibrated impact confidence is unavailable before admission")
    return {
        "raw_confidence": raw,
        "calibrated_confidence": None,
        "calibration_version": None,
        "uncertainty_reason": uncertainty,
    }


def _assessment(value: object, allowed_evidence: set[str]) -> dict:
    if not isinstance(value, Mapping):
        raise AnalysisRunError("impact assessment must be an object")
    _exact_keys(value, {
        "target", "aspect", "horizon", "direction", "intensity", "evidence_ids",
        "contradicting_evidence_ids", "mechanism", "assumptions", "raw_confidence",
        "calibrated_confidence", "calibration_version", "uncertainty_reason",
    })
    target = _target(value["target"])
    aspect = _text(value["aspect"], "impact aspect", 80)
    if aspect not in ASPECTS:
        raise AnalysisRunError("impact aspect is not in the declared vocabulary")
    direction = _text(value["direction"], "impact direction", 20)
    if direction not in DIRECTIONS:
        raise AnalysisRunError("impact direction is unsupported")
    intensity = _probability(value["intensity"], "impact intensity", nullable=True)
    supporting = _evidence_ids(
        value["evidence_ids"], "impact evidence_ids", allowed_evidence, required=True
    )
    contradicting = _evidence_ids(
        value["contradicting_evidence_ids"], "impact contradicting_evidence_ids",
        allowed_evidence, required=False,
    )
    if set(supporting) & set(contradicting):
        raise AnalysisRunError("impact supporting and contradicting evidence must be disjoint")
    assumptions = value["assumptions"]
    if (
        not isinstance(assumptions, list) or len(assumptions) > 20
        or any(not isinstance(item, str) for item in assumptions)
    ):
        raise AnalysisRunError("impact assumptions must be a bounded string list")
    clean_assumptions = [
        _text(item, "impact assumption", 500) for item in assumptions
    ]
    if len(clean_assumptions) != len(set(clean_assumptions)):
        raise AnalysisRunError("impact assumptions must not contain duplicates")
    confidence = _confidence(value)
    if direction == "unknown":
        if intensity is not None:
            raise AnalysisRunError("unknown impact direction requires null intensity")
        if confidence["uncertainty_reason"] is None:
            raise AnalysisRunError("unknown impact direction requires an uncertainty_reason")
    elif intensity is None:
        raise AnalysisRunError("known impact direction requires intensity")
    return {
        "target": target,
        "aspect": aspect,
        "horizon": _horizon(value["horizon"]),
        "direction": direction,
        "intensity": intensity,
        "evidence_ids": supporting,
        "contradicting_evidence_ids": contradicting,
        "mechanism": _text(value["mechanism"], "impact mechanism", 1_000),
        "assumptions": clean_assumptions,
        **confidence,
    }


def validate_impact_data(
    *, schema_version: str, status: str, data: object, allowed_evidence: set[str]
) -> dict:
    if schema_version != IMPACT_SCHEMA_VERSION:
        raise AnalysisRunError("impact schema does not match its task type")
    if not isinstance(data, Mapping):
        raise AnalysisRunError("impact data must be an object")
    if status not in {"valid", "needs_review", "insufficient_evidence", "refused"}:
        raise AnalysisRunError("impact output has an unsupported status")
    if status in {"insufficient_evidence", "refused"}:
        _exact_keys(data, {"reason_code"})
        return {"reason_code": _text(data["reason_code"], "impact reason_code", 100)}
    if status == "valid":
        raise AnalysisRunError("impact output cannot be valid before quality admission")
    _exact_keys(data, {"assessments"})
    assessments = data["assessments"]
    if not isinstance(assessments, list) or not assessments:
        raise AnalysisRunError("impact assessments must be a non-empty list")
    clean = [_assessment(item, allowed_evidence) for item in assessments]
    keys = [
        (item["target"]["entity_id"], item["aspect"], item["horizon"]["bucket"])
        for item in clean
    ]
    if len(keys) != len(set(keys)):
        raise AnalysisRunError("impact assessments duplicate a target/aspect/horizon")
    return {"assessments": clean}


def validate_impact_envelope(output: dict) -> dict:
    _exact_keys(output, {"schema_version", "subject", "status", "evidence_ids", "data"})
    evidence_ids = output["evidence_ids"]
    if (
        not isinstance(evidence_ids, list)
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 128
            for item in evidence_ids
        )
    ):
        raise AnalysisRunError("impact output evidence_ids must be a string list")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise AnalysisRunError("impact output evidence_ids contains duplicate evidence IDs")
    if output["status"] == "needs_review":
        referenced = {
            evidence_id
            for assessment in output["data"]["assessments"]
            for field in ("evidence_ids", "contradicting_evidence_ids")
            for evidence_id in assessment[field]
        }
        if not evidence_ids or set(evidence_ids) != referenced:
            raise AnalysisRunError(
                "impact output evidence_ids must exactly list assessment evidence"
            )
    return output


def referenced_impact_entities(data: dict) -> list[tuple[str, str, str]]:
    assessments = data.get("assessments")
    if not isinstance(assessments, list):
        return []
    return [
        (item["target"]["entity_id"], item["target"]["type"], "impact target entity_id")
        for item in assessments
        if isinstance(item, dict) and isinstance(item.get("target"), dict)
    ]


def verify_impact_evidence(db, *, event_version_id: str, data: dict) -> dict:
    assessments = data.get("assessments")
    if not isinstance(assessments, list):
        return {"validator_version": IMPACT_VALIDATOR_VERSION, "status": "not_applicable"}
    supporting = {
        evidence_id for item in assessments for evidence_id in item["evidence_ids"]
    }
    contradicting = {
        evidence_id
        for item in assessments
        for evidence_id in item["contradicting_evidence_ids"]
    }
    rows = db.execute(
        """SELECT evidence.evidence_id,evidence.role,raw.payload_kind,raw.truncated
           FROM event_evidence AS evidence
           JOIN raw_records AS raw ON raw.id=evidence.evidence_id
           WHERE evidence.event_version_id=?""",
        (event_version_id,),
    ).fetchall()
    roles: dict[str, set[str]] = {}
    usable: dict[str, bool] = {}
    for row in rows:
        roles.setdefault(row["evidence_id"], set()).add(row["role"])
        usable[row["evidence_id"]] = (
            row["payload_kind"] != "generated_metadata" and row["truncated"] == 0
        )
    invalid_support = sorted(
        item for item in supporting
        if "supports" not in roles.get(item, set()) or not usable.get(item, False)
    )
    invalid_contradiction = sorted(
        item for item in contradicting
        if "contradicts" not in roles.get(item, set()) or not usable.get(item, False)
    )
    if invalid_support:
        raise AnalysisRunError(
            f"impact support is not direct event evidence: {invalid_support[:5]}"
        )
    if invalid_contradiction:
        raise AnalysisRunError(
            f"impact contradiction is not linked event evidence: {invalid_contradiction[:5]}"
        )
    return {
        "validator_version": IMPACT_VALIDATOR_VERSION,
        "status": "passed",
        "event_version_id": event_version_id,
        "supporting_evidence_ids": sorted(supporting),
        "contradicting_evidence_ids": sorted(contradicting),
    }


__all__ = [
    "IMPACT_SCHEMA_VERSION", "referenced_impact_entities", "validate_impact_data",
    "validate_impact_envelope", "verify_impact_evidence",
]
