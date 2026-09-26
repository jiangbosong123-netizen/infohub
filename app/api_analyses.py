from __future__ import annotations

"""Typed reads for validated, published immutable analysis results."""

import hashlib
import json
import sqlite3
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .api_auth import ApiPrincipal
from .timeutil import utc_now


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisSubjectRef(_StrictModel):
    type: Literal["document", "event"]
    version_id: str = Field(min_length=1, max_length=128)


class AnalysisInputRef(_StrictModel):
    ordinal: int = Field(ge=0)
    role: Literal["primary", "supporting", "context", "contradicting"]
    subject: AnalysisSubjectRef
    evidence_id: str | None = Field(default=None, max_length=128)


class AnalysisView(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    view_version_id: str = Field(min_length=1, max_length=128)
    view_version: int = Field(ge=1)
    state_as_of: str
    task_type: Literal[
        "language", "translation", "relevance", "entity_linking", "summarization",
        "importance", "event_extraction", "event_linking", "tone", "impact",
        "macro_mapping", "report",
    ]
    schema_version: str = Field(min_length=1, max_length=128)
    subject_ref: AnalysisSubjectRef
    input_refs: list[AnalysisInputRef]
    input_hash: str = Field(min_length=64, max_length=64)
    pipeline_version: str = Field(min_length=1, max_length=500)
    provider: str = Field(min_length=1, max_length=500)
    model_requested: str = Field(min_length=1, max_length=500)
    model_resolved: str | None = Field(default=None, max_length=500)
    prompt_hash: str = Field(min_length=64, max_length=64)
    parameters_hash: str = Field(min_length=64, max_length=64)
    analyzed_at: str
    available_at: str
    validation_status: Literal["passed"] = "passed"
    result_status: Literal["valid", "needs_review", "insufficient_evidence", "refused"]
    review_status: Literal["unreviewed", "accepted", "rejected", "corrected"]
    stale: bool
    output: dict[str, JsonValue]
    evidence_refs: list[str]
    evidence_status: Literal["supported", "partial", "insufficient", "refused"]
    processing_state: Literal["complete", "partial", "insufficient_evidence"]


class AnalysisResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: AnalysisView


class AnalysisUnavailable(RuntimeError):
    pass


class AnalysisNotFound(LookupError):
    pass


def _json_object(value: str, field: str) -> dict:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnalysisUnavailable(f"published analysis {field} is invalid") from exc
    if not isinstance(decoded, dict):
        raise AnalysisUnavailable(f"published analysis {field} is invalid")
    return decoded


def get_analysis(
    db: sqlite3.Connection, *, request_id: str, analysis_id: str,
) -> AnalysisResponse:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise AnalysisUnavailable("dataset identity is unavailable")
    row = db.execute(
        """SELECT result.*,publication.id AS publication_id,
                  publication.version AS publication_version,publication.review_status,
                  publication.evidence_status,
                  publication.available_at AS publication_available_at,
                  run.subject_type,run.subject_version_id,run.task_type,
                  run.provider,run.requested_model,run.prompt_sha256,
                  run.pipeline_version,run.parameters_json,run.input_manifest_sha256,
                  attempt.resolved_model,attempt.finished_at,attempt.status AS attempt_status,
                  current.current_publication_id
           FROM analysis_results AS result
           JOIN analysis_runs AS run ON run.id=result.run_id
           JOIN analysis_attempts AS attempt ON attempt.id=result.attempt_id
           JOIN analysis_publication_versions AS publication ON publication.result_id=result.id
           JOIN analysis_publications AS current
             ON current.subject_type=publication.subject_type
            AND current.subject_version_id=publication.subject_version_id
            AND current.task_type=publication.task_type
           WHERE result.id=?
           ORDER BY publication.version DESC,publication.id DESC LIMIT 1""",
        (analysis_id,),
    ).fetchone()
    if row is None:
        raise AnalysisNotFound("analysis result is not published")
    output = _json_object(row["validated_output_json"], "output")
    if (
        output.get("schema_version") != row["schema_version"]
        or output.get("subject") != {
            "type": row["subject_type"], "version_id": row["subject_version_id"],
        }
        or output.get("status") != row["result_status"]
        or row["attempt_status"] not in {"succeeded", "refused"}
    ):
        raise AnalysisUnavailable("published analysis output does not match its ledger")
    report = _json_object(row["validation_report_json"], "validation report")
    if report.get("status") != "passed":
        raise AnalysisUnavailable("published analysis validation did not pass")
    evidence_refs = report.get("referenced_evidence_ids")
    if not isinstance(evidence_refs, list) or any(
        not isinstance(value, str) or not value for value in evidence_refs
    ):
        raise AnalysisUnavailable("published analysis evidence references are invalid")
    parameters = _json_object(row["parameters_json"], "parameters")
    input_rows = db.execute(
        """SELECT ordinal,document_version_id,event_version_id,evidence_id,role
           FROM analysis_inputs WHERE run_id=? ORDER BY ordinal""",
        (row["run_id"],),
    ).fetchall()
    if not input_rows:
        raise AnalysisUnavailable("published analysis inputs are unavailable")
    inputs = []
    for item in input_rows:
        subject_type = "document" if item["document_version_id"] is not None else "event"
        version_id = item["document_version_id"] or item["event_version_id"]
        if not version_id:
            raise AnalysisUnavailable("published analysis input is invalid")
        inputs.append(AnalysisInputRef(
            ordinal=item["ordinal"], role=item["role"],
            subject=AnalysisSubjectRef(type=subject_type, version_id=version_id),
            evidence_id=item["evidence_id"],
        ))
    processing_state = {
        "valid": "complete",
        "needs_review": "partial",
        "insufficient_evidence": "insufficient_evidence",
        "refused": "insufficient_evidence",
    }.get(row["result_status"])
    if processing_state is None:
        raise AnalysisUnavailable("published analysis result status is invalid")
    view = AnalysisView(
        id=row["id"], view_version_id=row["publication_id"],
        view_version=row["publication_version"], state_as_of=row["publication_available_at"],
        task_type=row["task_type"], schema_version=row["schema_version"],
        subject_ref=AnalysisSubjectRef(
            type=row["subject_type"], version_id=row["subject_version_id"],
        ),
        input_refs=inputs, input_hash=row["input_manifest_sha256"],
        pipeline_version=row["pipeline_version"], provider=row["provider"],
        model_requested=row["requested_model"], model_resolved=row["resolved_model"],
        prompt_hash=row["prompt_sha256"],
        parameters_hash=hashlib.sha256(rfc8785.dumps(parameters)).hexdigest(),
        analyzed_at=row["finished_at"], available_at=row["available_at"],
        result_status=row["result_status"], review_status=row["review_status"],
        stale=row["current_publication_id"] != row["publication_id"], output=output,
        evidence_refs=evidence_refs, evidence_status=row["evidence_status"],
        processing_state=processing_state,
    )
    return AnalysisResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), data=view,
    )


def analysis_etag(response: AnalysisResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'


__all__ = [
    "AnalysisNotFound", "AnalysisResponse", "AnalysisUnavailable", "analysis_etag",
    "get_analysis",
]
