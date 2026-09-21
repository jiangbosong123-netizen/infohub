from __future__ import annotations

"""Read-only gate for the initial, offline legacy curation import."""

from collections import Counter
from contextlib import closing
from pathlib import Path

from .db_admin import _connect_readonly, database_state
from .legacy_curation_import import JOB_KIND, MODEL, PROMPT_ID, PROVIDER

TASKS = {"translation", "relevance", "summarization", "importance"}


def audit_curation_import(path: str | Path) -> dict:
    target = Path(path).expanduser().resolve(strict=True)
    with closing(_connect_readonly(target)) as db:
        db.execute("BEGIN")
        state, version = database_state(db)
        if state != "current":
            return {"status": "unavailable", "schema_state": state,
                    "schema_version": version, "reason": "current_schema_required"}
        rows = db.execute("""SELECT j.state,j.subject_id,j.input_version,
                   json_extract(j.payload_json,'$.task_type') AS task_type,
                   run.id AS run_id,run.provider,run.requested_model,
                   run.prompt_template_id,run.subject_type,run.subject_version_id,
                   run.task_type AS run_task,
                   input.role AS input_role,input.document_version_id AS input_document_version,
                   input.evidence_id,input_raw.payload_kind,
                   auth.decision,auth.reserved_cost_microusd,
                   attempt.status AS attempt_status,attempt.cost_microusd,
                   publication.current_publication_id,pv.result_id,pv.review_status,
                   pv.evidence_status,result.run_id AS result_run_id,
                   result.result_status
            FROM jobs j
            LEFT JOIN analysis_runs run ON run.job_id=j.id
            LEFT JOIN analysis_inputs input ON input.run_id=run.id AND input.ordinal=0
            LEFT JOIN raw_records input_raw ON input_raw.id=input.evidence_id
            LEFT JOIN analysis_attempt_authorizations auth
              ON auth.run_id=run.id AND auth.attempt_number=1
            LEFT JOIN analysis_attempts attempt ON attempt.authorization_id=auth.id
            LEFT JOIN analysis_publications publication
              ON publication.subject_type='document'
             AND publication.subject_version_id=j.input_version
             AND publication.task_type=json_extract(j.payload_json,'$.task_type')
            LEFT JOIN analysis_publication_versions pv
              ON pv.id=publication.current_publication_id
            LEFT JOIN analysis_results result ON result.id=pv.result_id
            WHERE j.kind=? ORDER BY j.id""", (JOB_KIND,))
        states: Counter[str] = Counter()
        tasks: Counter[str] = Counter()
        results: Counter[str] = Counter()
        violations: Counter[str] = Counter()
        documents: set[str] = set()
        jobs = 0
        for row in rows:
            jobs += 1
            states[str(row["state"])] += 1
            task = row["task_type"]
            tasks[str(task)] += 1
            if row["input_version"]:
                documents.add(row["input_version"])
            if task not in TASKS or not row["input_version"] or row["subject_id"] != row["input_version"]:
                violations["invalid_job_subject"] += 1
            if row["state"] != "succeeded":
                continue
            if (not row["run_id"] or row["provider"] != PROVIDER
                    or row["requested_model"] != MODEL or row["prompt_template_id"] != PROMPT_ID
                    or row["subject_type"] != "document" or row["subject_version_id"] != row["input_version"]
                    or row["run_task"] != task):
                violations["invalid_run_provenance"] += 1
            if (row["input_role"] != "primary" or row["input_document_version"] != row["input_version"]
                    or not row["evidence_id"] or row["payload_kind"] != "legacy_excerpt"):
                violations["invalid_frozen_input"] += 1
            if (row["decision"] != "allowed" or row["reserved_cost_microusd"] != 0
                    or row["attempt_status"] not in {"succeeded", "refused"}
                    or row["cost_microusd"] is not None):
                violations["invalid_offline_attempt"] += 1
            if (not row["current_publication_id"] or not row["result_id"]
                    or row["result_run_id"] != row["run_id"]):
                violations["missing_or_wrong_publication"] += 1
            if (row["review_status"] != "unreviewed"
                    or row["result_status"] not in {"needs_review", "insufficient_evidence", "refused"}
                    or row["evidence_status"] not in {"partial", "insufficient", "refused"}):
                violations["invalid_initial_review_state"] += 1
            if row["result_status"]:
                results[str(row["result_status"])] += 1
        status = "invalid" if violations else ("ok" if jobs and states == {"succeeded": jobs}
                                                 else "incomplete")
        return {"status": status, "schema_version": version,
                "jobs": jobs, "documents": len(documents),
                "job_states": dict(sorted(states.items())),
                "task_counts": dict(sorted(tasks.items())),
                "result_statuses": dict(sorted(results.items())),
                "violations": dict(sorted(violations.items())),
                "scope": "Initial offline import only; unlabeled results are not NLP quality evidence."}
