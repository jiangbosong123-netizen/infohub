from __future__ import annotations

"""Offline, leased import of frozen legacy curation snapshots into analysis publications."""

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .analysis_attempts import AttemptRecord, authorize_attempt, record_attempt, register_budget_policy
from .analysis_results import publish_analysis_result
from .analysis_runs import AnalysisInput, AnalysisRunError, prepare_analysis_run
from .curation_contracts import CURATION_PIPELINE_VERSION, CURATION_SCHEMAS, adapt_legacy_curation
from .database import get_db
from .documents import _clean_text
from .ingest import verify_payload
from .jobs import JobRecord, LeaseLostError, claim_job, enqueue_job, fail_job
from .publication import publish_job_result
from .timeutil import utc_now

JOB_KIND = "legacy_curation_import"
PROVIDER = "legacy_import_offline"
MODEL = "historical_unknown"
PROMPT_ID = "historical_unknown"
PROMPT_SHA256 = hashlib.sha256(PROMPT_ID.encode("utf-8")).hexdigest()


class LegacyCurationImportError(AnalysisRunError):
    pass


@dataclass(frozen=True)
class ImportResult:
    job_id: str
    task_type: str
    status: str
    publication_id: str | None


def _candidate(db, legacy_item_id: int):
    return db.execute(
        """SELECT document.current_version_id AS version_id,
                  input.raw_record_id, raw.payload_ref, raw.payload_sha256
           FROM documents AS document
           JOIN document_version_inputs AS input
             ON input.version_id=document.current_version_id AND input.role='primary'
           JOIN raw_records AS raw ON raw.id=input.raw_record_id
           WHERE document.legacy_item_id=? AND raw.payload_kind='legacy_excerpt'""",
        (legacy_item_id,),
    ).fetchone()


def enqueue_legacy_curation_item(legacy_item_id: int) -> tuple[JobRecord, ...]:
    """Enqueue four task-specific imports only when P07 left a frozen legacy snapshot."""
    if type(legacy_item_id) is not int or legacy_item_id < 1:
        raise ValueError("legacy_item_id must be a positive integer")
    with get_db() as db:
        candidate = _candidate(db, legacy_item_id)
    if candidate is None:
        raise LegacyCurationImportError("legacy item has no current frozen backfill version")
    version = candidate["version_id"]
    return tuple(
        enqueue_job(
            kind=JOB_KIND,
            idempotency_key=f"legacy-curation-v1:{version}:{task}",
            subject_id=version,
            input_version=version,
            payload={"task_type": task, "legacy_item_id": legacy_item_id,
                     "raw_record_id": candidate["raw_record_id"]},
            max_attempts=3,
        )
        for task in CURATION_SCHEMAS
    )


def enqueue_legacy_curation_batch(*, after_item_id: int = 0, limit: int = 100) -> dict:
    """Scan a bounded ID page. The returned cursor is safe to resume/replay."""
    if type(after_item_id) is not int or after_item_id < 0:
        raise ValueError("after_item_id must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with get_db() as db:
        rows = db.execute(
            """SELECT document.legacy_item_id
               FROM documents AS document
               JOIN document_version_inputs AS input
                 ON input.version_id=document.current_version_id AND input.role='primary'
               JOIN raw_records AS raw ON raw.id=input.raw_record_id
               WHERE document.legacy_item_id>? AND raw.payload_kind='legacy_excerpt'
               ORDER BY document.legacy_item_id LIMIT ?""",
            (after_item_id, limit),
        ).fetchall()
    for row in rows:
        enqueue_legacy_curation_item(row[0])
    return {"items_seen": len(rows), "jobs_ensured": len(rows) * len(CURATION_SCHEMAS),
            "next_after_item_id": rows[-1][0] if rows else after_item_id}


def _load_frozen_snapshot(job: JobRecord) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if job.kind != JOB_KIND or not job.lease_token or job.state != "running":
        raise LegacyCurationImportError("a current legacy-import lease is required")
    task = job.payload.get("task_type")
    if task not in CURATION_SCHEMAS:
        raise LegacyCurationImportError("unsupported legacy curation task")
    with get_db() as db:
        candidate = _candidate(db, job.payload.get("legacy_item_id"))
        if (
            candidate is None or candidate["version_id"] != job.input_version
            or candidate["raw_record_id"] != job.payload.get("raw_record_id")
        ):
            raise LegacyCurationImportError("frozen legacy input changed before import")
        version = db.execute(
            "SELECT title_original,text FROM document_versions WHERE id=?",
            (job.input_version,),
        ).fetchone()
    path = verify_payload(candidate["payload_ref"], candidate["payload_sha256"])
    payload = json.loads(path.read_text("utf-8"))
    snapshot = payload.get("extra", {}).get("legacy_snapshot")
    if not isinstance(snapshot, dict) or not version:
        raise LegacyCurationImportError("frozen legacy snapshot is missing")
    original_text = snapshot.get("raw_summary")
    if original_text is None:
        original_text = snapshot.get("summary")
    # Document projection collapses whitespace before freezing the version.
    # Compare the same normalization while still requiring the exact CAS-backed
    # legacy snapshot as the input to the imported publication.
    if (version["title_original"] != _clean_text(snapshot.get("title"))
            or version["text"] != _clean_text(original_text)):
        raise LegacyCurationImportError("frozen legacy snapshot does not match its document version")
    return candidate, snapshot


def import_claimed_legacy_curation(job: JobRecord) -> ImportResult:
    """Publish exactly one frozen task; publication also completes the leased job."""
    candidate, snapshot = _load_frozen_snapshot(job)
    task = str(job.payload["task_type"])
    with get_db() as db:
        existing = db.execute(
            """SELECT current_publication_id FROM analysis_publications
               WHERE subject_type='document' AND subject_version_id=? AND task_type=?""",
            (job.input_version, task),
        ).fetchone()
    if existing:
        publish_job_result(job_id=job.id, lease_token=job.lease_token or "",
                           expected_input_version=job.input_version)
        return ImportResult(job.id, task, "already_published", existing[0])

    evidence_id = candidate["raw_record_id"]
    envelope = adapt_legacy_curation(
        snapshot, subject_version_id=job.input_version or "", evidence_id=evidence_id,
    )[task]
    # No provider call occurs. A zero-cost policy still creates an explicit,
    # auditable authorization and prevents accidental use of a paid provider.
    register_budget_policy(
        provider=PROVIDER, daily_limit_microusd=0, per_attempt_limit_microusd=0,
        effective_from="1970-01-01T00:00:00Z", idempotency_key="legacy-import-offline-zero-v1",
    )
    key = f"legacy-curation-v1:{job.input_version}:{task}"
    run = prepare_analysis_run(
        job_id=job.id, lease_token=job.lease_token or "",
        expected_input_version=job.input_version,
        subject_type="document", subject_version_id=job.input_version or "",
        task_type=task, output_schema_version=CURATION_SCHEMAS[task],
        inputs=(AnalysisInput("primary", job.input_version, None, evidence_id),),
        provider=PROVIDER, requested_model=MODEL,
        prompt_template_id=PROMPT_ID, prompt_sha256=PROMPT_SHA256,
        rendered_input_ref=candidate["payload_ref"],
        rendered_input_sha256=candidate["payload_sha256"],
        pipeline_version=CURATION_PIPELINE_VERSION,
        parameters={"mode": "historical_snapshot_import", "original_model": "unknown",
                    "original_prompt": "unknown", "provider_call": False},
        idempotency_key=key,
    )
    authorization = authorize_attempt(
        run_id=run.id, job_id=job.id, lease_token=job.lease_token or "",
        expected_input_version=job.input_version, attempt_kind="primary",
        reserved_cost_microusd=0, idempotency_key=key,
    )
    if authorization.decision != "allowed":
        raise LegacyCurationImportError("offline import authorization was blocked")
    attempt_status = "refused" if envelope["status"] == "refused" else "succeeded"
    with get_db() as db:
        recorded = db.execute(
            "SELECT * FROM analysis_attempts WHERE authorization_id=?",
            (authorization.id,),
        ).fetchone()
    if recorded:
        if recorded["run_id"] != run.id or recorded["status"] != attempt_status:
            raise LegacyCurationImportError("prior offline attempt conflicts with frozen input")
        attempt = AttemptRecord(
            recorded["id"], recorded["run_id"], recorded["attempt_number"],
            recorded["status"], recorded["cost_microusd"],
        )
    else:
        now = utc_now()
        attempt = record_attempt(
            authorization_id=authorization.id, job_id=job.id,
            lease_token=job.lease_token or "", expected_input_version=job.input_version,
            status=attempt_status, started_at=now, finished_at=now,
            resolved_model=MODEL, usage_status="unknown", raw_response_ref=None,
        )
    evidence_status = {
        "needs_review": "partial", "valid": "partial",
        "insufficient_evidence": "insufficient", "refused": "refused",
    }[envelope["status"]]
    result = publish_analysis_result(
        job_id=job.id, lease_token=job.lease_token or "",
        expected_input_version=job.input_version, run_id=run.id,
        attempt_id=attempt.id, validated_output=envelope,
        review_status="unreviewed", evidence_status=evidence_status,
        idempotency_key=key,
    )
    return ImportResult(job.id, task, envelope["status"], result.changes[0].version_id)


def process_one_legacy_curation_import(*, worker_id: str) -> ImportResult | None:
    job = claim_job(worker_id=worker_id, kinds=(JOB_KIND,), lease_seconds=300)
    if job is None:
        return None
    try:
        return import_claimed_legacy_curation(job)
    except LeaseLostError:
        raise
    except Exception as exc:
        failed = fail_job(
            job.id, job.lease_token or "", error_code=type(exc).__name__[:100],
            error_detail=str(exc)[:500],
        )
        return ImportResult(job.id, str(job.payload.get("task_type")), failed.state, None)
