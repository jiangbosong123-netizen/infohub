from __future__ import annotations

"""Route immutable analysis outputs to their versioned task-specific validators."""

from .analysis_runs import AnalysisRunError
from .curation_contracts import validate_curation_data
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
    if schema_version in TONE_SCHEMA_VERSIONS:
        raise AnalysisRunError("tone schema does not match its task type")
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
    return []


def verify_analysis_evidence(
    db, *, task_type: str, schema_version: str, data: dict
) -> dict:
    if task_type == "tone":
        return verify_tone_quotes(db, schema_version=schema_version, data=data)
    return {"validator_version": None, "status": "not_applicable"}


__all__ = [
    "referenced_analysis_entities", "validate_analysis_data", "verify_analysis_evidence",
]
