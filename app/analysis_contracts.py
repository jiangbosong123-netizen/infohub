from __future__ import annotations

"""Route immutable analysis outputs to their versioned task-specific validators."""

from .analysis_runs import AnalysisRunError
from .curation_contracts import validate_curation_data
from .tone_contracts import TONE_SCHEMA_VERSION, referenced_tone_entities, validate_tone_data


def validate_analysis_data(
    *,
    task_type: str,
    schema_version: str,
    status: str,
    data: object,
    allowed_evidence: set[str],
) -> dict:
    if task_type == "tone":
        if schema_version != TONE_SCHEMA_VERSION:
            raise AnalysisRunError("tone schema does not match its task type")
        return validate_tone_data(
            status=status, data=data, allowed_evidence=allowed_evidence
        )
    if schema_version == TONE_SCHEMA_VERSION:
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


__all__ = ["referenced_analysis_entities", "validate_analysis_data"]
