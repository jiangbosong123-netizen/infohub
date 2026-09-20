from __future__ import annotations

"""Record report-model inputs and outputs without invoking a provider."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Mapping

from .database import get_db
from .ingest import store_payload, verify_payload
from .report_drafts import render_validated_draft
from .report_inputs import _json, load_frozen_manifest
from .timeutil import format_utc, parse_utc


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError(f"{label} must contain 1 to 200 characters")
    return value.strip()


def _at(value: datetime | str | None) -> str:
    if value is None:
        return format_utc(datetime.now(timezone.utc))
    return format_utc(parse_utc(value) if isinstance(value, str) else value)


def prepare_report_generation(
    *, snapshot_id: str, provider: str, requested_model: str,
    prompt_template_id: str, prompt_template_text: str, rendered_prompt: str,
    parameters: Mapping[str, object], now: datetime | str | None = None,
) -> dict:
    """Pin a verified input and persist the exact rendered request in CAS."""
    load_frozen_manifest(snapshot_id)
    provider = _name(provider, "provider")
    requested_model = _name(requested_model, "requested_model")
    prompt_template_id = _name(prompt_template_id, "prompt_template_id")
    if not isinstance(prompt_template_text, str) or not prompt_template_text.strip():
        raise ValueError("prompt template text is required")
    if not isinstance(rendered_prompt, str) or not rendered_prompt.strip():
        raise ValueError("rendered prompt is required")
    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be a JSON object")
    parameters_json = _json(dict(parameters))
    prompt_sha = _sha_bytes(prompt_template_text.encode("utf-8"))
    rendered_bytes = rendered_prompt.encode("utf-8")
    rendered_sha, rendered_ref = store_payload(rendered_bytes)
    verify_payload(rendered_ref, rendered_sha)
    prepared_at = _at(now)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = db.execute(
            "SELECT dataset_id FROM report_input_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise ValueError("report input snapshot is missing")
        identity = _json({
            "dataset_id": snapshot["dataset_id"], "snapshot_id": snapshot_id,
            "provider": provider, "model": requested_model,
            "template_id": prompt_template_id, "template_sha256": prompt_sha,
            "rendered_sha256": rendered_sha, "parameters": json.loads(parameters_json),
        })
        run_id = "report_run_" + _sha_bytes(identity.encode("utf-8"))[:32]
        existing = db.execute("SELECT * FROM report_generation_runs WHERE id=?", (run_id,)).fetchone()
        if existing:
            if (existing["input_snapshot_id"] != snapshot_id or existing["rendered_prompt_ref"] != rendered_ref
                    or existing["parameters_json"] != parameters_json):
                raise ValueError("report generation retry inputs do not match")
            return {"status": "already_prepared", "run_id": run_id,
                    "rendered_prompt_sha256": rendered_sha}
        db.execute(
            """INSERT INTO report_generation_runs(
                 id,dataset_id,input_snapshot_id,provider,requested_model,
                 prompt_template_id,prompt_sha256,rendered_prompt_ref,
                 rendered_prompt_sha256,parameters_json,prepared_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, snapshot["dataset_id"], snapshot_id, provider, requested_model,
             prompt_template_id, prompt_sha, rendered_ref, rendered_sha,
             parameters_json, prepared_at),
        )
    return {"status": "prepared", "run_id": run_id,
            "rendered_prompt_sha256": rendered_sha}


def record_report_response(
    *, run_id: str, response: bytes | str, resolved_model: str,
    started_at: datetime | str, finished_at: datetime | str,
    provider_request_id: str | None = None, input_tokens: int | None = None,
    output_tokens: int | None = None, cost_microusd: int | None = None,
    now: datetime | str | None = None,
) -> dict:
    """Persist raw bytes, then validate a closed cited draft; never publish it."""
    model = _name(resolved_model, "resolved_model")
    if provider_request_id is not None:
        provider_request_id = _name(provider_request_id, "provider_request_id")
    for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens),
                        ("cost_microusd", cost_microusd)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a nonnegative integer")
    start, finish = _at(started_at), _at(finished_at)
    if finish < start:
        raise ValueError("report attempt finished before it started")
    with get_db() as db:
        run = db.execute("SELECT * FROM report_generation_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError("report generation run is missing")
    verify_payload(run["rendered_prompt_ref"], run["rendered_prompt_sha256"])
    manifest = load_frozen_manifest(run["input_snapshot_id"])
    raw = response.encode("utf-8") if isinstance(response, str) else response
    if not isinstance(raw, bytes):
        raise ValueError("response must be UTF-8 text or bytes")
    response_sha, response_ref = store_payload(raw)
    verify_payload(response_ref, response_sha)
    draft_json = None
    try:
        draft = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        validation = {"schema_version": "infohub.report-draft-validation/1.0",
                      "status": "invalid_draft", "error_code": "invalid_json"}
    else:
        try:
            _, citations, coverage = render_validated_draft(manifest, draft)
        except (ValueError, KeyError, TypeError):
            validation = {"schema_version": "infohub.report-draft-validation/1.0",
                          "status": "invalid_draft", "error_code": "invalid_contract"}
        else:
            draft_json = _json(draft)
            validation = {"schema_version": "infohub.report-draft-validation/1.0",
                          "status": "valid_draft", "citation_count": len(citations),
                          "claim_entailment_status": coverage["claim_entailment_status"]}
    usage_status = "reported" if any(
        value is not None for value in (input_tokens, output_tokens, cost_microusd)
    ) else "unknown"
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """SELECT id,status FROM report_generation_attempts
               WHERE run_id=? AND raw_response_sha256=? AND resolved_model=?
                 AND COALESCE(provider_request_id,'')=COALESCE(?,'')
               ORDER BY attempt_number LIMIT 1""",
            (run_id, response_sha, model, provider_request_id),
        ).fetchone()
        if existing:
            return {"status": existing["status"], "attempt_id": existing["id"],
                    "already_recorded": True}
        number = db.execute(
            "SELECT COALESCE(MAX(attempt_number),0)+1 FROM report_generation_attempts WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        if number > 4:
            raise ValueError("report generation attempt limit reached")
        attempt_id = "report_attempt_" + _sha_bytes(f"{run_id}:{number}".encode("utf-8"))[:32]
        db.execute(
            """INSERT INTO report_generation_attempts(
                 id,run_id,attempt_number,status,resolved_model,provider_request_id,
                 raw_response_ref,raw_response_sha256,validated_draft_json,
                 validation_report_json,input_tokens,output_tokens,cost_microusd,
                 usage_status,started_at,finished_at,recorded_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (attempt_id, run_id, number, validation["status"], model, provider_request_id,
             response_ref, response_sha, draft_json, _json(validation), input_tokens,
             output_tokens, cost_microusd, usage_status, start, finish, _at(now)),
        )
    return {"status": validation["status"], "attempt_id": attempt_id,
            "already_recorded": False}
