from __future__ import annotations

"""Prepare immutable, version-pinned analysis input manifests before model calls."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Mapping

from .database import get_db
from .event_relations import _stable_id
from .jobs import _current_lease, _validate_input_version
from .timeutil import format_utc, parse_utc, utc_now


TASK_TYPES = {
    "language", "translation", "relevance", "entity_linking", "summarization",
    "importance", "event_extraction", "event_linking", "tone", "impact",
    "macro_mapping", "report",
}
ROLES = {"primary", "supporting", "context", "contradicting"}
MANIFEST_VERSION = "analysis-input-manifest-v1"


class AnalysisRunError(RuntimeError):
    """An analysis run cannot be prepared without a stable, valid input closure."""


@dataclass(frozen=True, order=True)
class AnalysisInput:
    role: str
    document_version_id: str | None = None
    event_version_id: str | None = None
    evidence_id: str | None = None


@dataclass(frozen=True)
class AnalysisRun:
    id: str
    job_id: str
    subject_type: str
    subject_version_id: str
    task_type: str
    input_manifest_sha256: str
    prepared_at: str


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _clean(value: str, field: str, maximum: int = 500) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return cleaned


def _time(value: datetime | str | None) -> str:
    if value is None:
        return utc_now()
    if isinstance(value, str):
        return format_utc(parse_utc(value))
    return format_utc(value)


def _validate_subject(db, subject_type: str, subject_version_id: str) -> None:
    table = {"document": "document_versions", "event": "event_versions"}.get(subject_type)
    if table is None:
        raise AnalysisRunError("analysis subject must be a document or event version")
    if not db.execute(f"SELECT 1 FROM {table} WHERE id=?", (subject_version_id,)).fetchone():
        raise AnalysisRunError("analysis subject version does not exist")


def _validate_inputs(db, inputs: tuple[AnalysisInput, ...]) -> None:
    if not inputs:
        raise AnalysisRunError("analysis requires at least one version-pinned input")
    if len(inputs) != len(set(inputs)):
        raise AnalysisRunError("analysis input manifest contains duplicates")
    for item in inputs:
        if item.role not in ROLES:
            raise AnalysisRunError("unsupported analysis input role")
        if (item.document_version_id is None) == (item.event_version_id is None):
            raise AnalysisRunError("each analysis input must reference one version type")
        if item.document_version_id is not None:
            if not db.execute(
                "SELECT 1 FROM document_versions WHERE id=?", (item.document_version_id,)
            ).fetchone():
                raise AnalysisRunError("analysis document version does not exist")
            if item.evidence_id is not None and not db.execute(
                """SELECT 1 FROM document_version_inputs
                   WHERE version_id=? AND raw_record_id=?""",
                (item.document_version_id, item.evidence_id),
            ).fetchone():
                raise AnalysisRunError("analysis evidence does not belong to its document version")
        else:
            if item.evidence_id is not None:
                raise AnalysisRunError("raw evidence must be attached through a document version")
            if not db.execute(
                "SELECT 1 FROM event_versions WHERE id=?", (item.event_version_id,)
            ).fetchone():
                raise AnalysisRunError("analysis event version does not exist")


def prepare_analysis_run(
    *,
    job_id: str,
    lease_token: str,
    expected_input_version: str | None,
    subject_type: str,
    subject_version_id: str,
    task_type: str,
    output_schema_version: str,
    inputs: Iterable[AnalysisInput],
    provider: str,
    requested_model: str,
    prompt_template_id: str,
    prompt_sha256: str,
    rendered_input_ref: str,
    rendered_input_sha256: str,
    pipeline_version: str,
    parameters: Mapping[str, object],
    idempotency_key: str,
    now: datetime | str | None = None,
) -> AnalysisRun:
    """Freeze an analysis request while its durable-job lease is current."""
    current = _time(now)
    task = _clean(task_type, "task_type")
    if task not in TASK_TYPES:
        raise AnalysisRunError("unsupported analysis task type")
    subject = _clean(subject_type, "subject_type")
    subject_version = _clean(subject_version_id, "subject_version_id")
    fields = {
        "output_schema_version": _clean(output_schema_version, "output_schema_version"),
        "provider": _clean(provider, "provider"),
        "requested_model": _clean(requested_model, "requested_model"),
        "prompt_template_id": _clean(prompt_template_id, "prompt_template_id"),
        "rendered_input_ref": _clean(rendered_input_ref, "rendered_input_ref", 2_000),
        "pipeline_version": _clean(pipeline_version, "pipeline_version"),
    }
    for digest, name in (
        (prompt_sha256, "prompt_sha256"),
        (rendered_input_sha256, "rendered_input_sha256"),
    ):
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AnalysisRunError(f"{name} must be a lowercase SHA-256")
    clean_inputs = tuple(inputs)
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "subject": {"type": subject, "version_id": subject_version},
        "task_type": task,
        "output_schema_version": fields["output_schema_version"],
        "inputs": [asdict(item) for item in clean_inputs],
        "provider": fields["provider"], "requested_model": fields["requested_model"],
        "prompt_template_id": fields["prompt_template_id"],
        "prompt_sha256": prompt_sha256,
        "rendered_input_ref": fields["rendered_input_ref"],
        "rendered_input_sha256": rendered_input_sha256,
        "pipeline_version": fields["pipeline_version"],
        "parameters": dict(parameters),
    }
    manifest_json = _json(manifest)
    manifest_sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    request_sha = _sha({"idempotency_key": idempotency_key, "manifest": manifest})
    run_id = _stable_id("analysis_run", idempotency_key)

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
        if existing:
            if existing["request_sha256"] != request_sha:
                raise AnalysisRunError("analysis run retry inputs do not match")
            return AnalysisRun(
                existing["id"], existing["job_id"], existing["subject_type"],
                existing["subject_version_id"], existing["task_type"],
                existing["input_manifest_sha256"], existing["prepared_at"],
            )
        job = _current_lease(db, job_id, lease_token, current)
        _validate_input_version(db, job, expected_input_version)
        _validate_subject(db, subject, subject_version)
        _validate_inputs(db, clean_inputs)
        if not any(
            (subject == "document" and item.document_version_id == subject_version)
            or (subject == "event" and item.event_version_id == subject_version)
            for item in clean_inputs
        ):
            raise AnalysisRunError("the subject version must be included in the input manifest")
        db.execute(
            """INSERT INTO analysis_runs(
                   id,idempotency_key,request_sha256,job_id,subject_type,subject_version_id,
                   task_type,output_schema_version,provider,requested_model,
                   prompt_template_id,prompt_sha256,rendered_input_ref,rendered_input_sha256,
                   pipeline_version,parameters_json,input_manifest_json,
                   input_manifest_sha256,prepared_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, idempotency_key, request_sha, job_id, subject, subject_version,
                task, fields["output_schema_version"], fields["provider"],
                fields["requested_model"], fields["prompt_template_id"], prompt_sha256,
                fields["rendered_input_ref"], rendered_input_sha256,
                fields["pipeline_version"], _json(dict(parameters)), manifest_json,
                manifest_sha, current,
            ),
        )
        for ordinal, item in enumerate(clean_inputs):
            db.execute(
                """INSERT INTO analysis_inputs(
                       run_id,ordinal,document_version_id,event_version_id,evidence_id,role
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    run_id, ordinal, item.document_version_id, item.event_version_id,
                    item.evidence_id, item.role,
                ),
            )
    return AnalysisRun(run_id, job_id, subject, subject_version, task, manifest_sha, current)
