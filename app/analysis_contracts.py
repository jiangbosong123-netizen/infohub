from __future__ import annotations

"""Route immutable analysis outputs to their versioned task-specific validators."""

from .analysis_runs import AnalysisRunError
from .curation_contracts import validate_curation_data
from .impact_contracts import (
    IMPACT_SCHEMA_VERSION,
    referenced_impact_entities,
    validate_impact_data,
    verify_impact_evidence,
)
from .tone_contracts import (
    TONE_SCHEMA_VERSIONS,
    referenced_tone_entities,
    validate_tone_data,
)
from .tone_evidence import verify_tone_quotes


def validate_analysis_data(
    *,
    task_type: str,
    schema_version: str,
    status: str,
    data: object,
    allowed_evidence: set[str],
    tone_calibration_version: str | None = None,
) -> dict:
    if task_type == "tone":
        if schema_version not in TONE_SCHEMA_VERSIONS:
            raise AnalysisRunError("tone schema does not match its task type")
        return validate_tone_data(
            schema_version=schema_version, status=status, data=data,
            allowed_evidence=allowed_evidence,
            admitted_calibration_version=tone_calibration_version,
        )
    if task_type == "impact":
        return validate_impact_data(
            schema_version=schema_version, status=status, data=data,
            allowed_evidence=allowed_evidence,
        )
    if schema_version in TONE_SCHEMA_VERSIONS:
        raise AnalysisRunError("tone schema does not match its task type")
    if schema_version == IMPACT_SCHEMA_VERSION:
        raise AnalysisRunError("impact schema does not match its task type")
    return validate_curation_data(
        task_type=task_type,
        schema_version=schema_version,
        status=status,
        data=data,
        allowed_evidence=allowed_evidence,
    )


def referenced_analysis_entities(
    *, task_type: str, data: dict
) -> list[tuple[str, str | None, str]]:
    if task_type == "tone":
        return referenced_tone_entities(data)
    if task_type == "impact":
        return referenced_impact_entities(data)
    return []


def verify_analysis_evidence(
    db, *, task_type: str, schema_version: str, subject_type: str,
    subject_version_id: str, data: dict,
) -> dict:
    if task_type == "tone":
        return verify_tone_quotes(db, schema_version=schema_version, data=data)
    if task_type == "impact":
        if subject_type != "event":
            raise AnalysisRunError("impact evidence requires an event version subject")
        return verify_impact_evidence(
            db, event_version_id=subject_version_id, data=data,
        )
    return {"validator_version": None, "status": "not_applicable"}


__all__ = [
    "referenced_analysis_entities", "validate_analysis_data", "verify_analysis_evidence",
]
