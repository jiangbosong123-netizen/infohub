from __future__ import annotations

"""Publish a manually approved, source-indexed report draft as an immutable version."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from .database import get_db
from .ingest import verify_payload
from .report_drafts import render_validated_draft
from .report_inputs import _json, _sha, load_frozen_manifest
from .report_review import review_preview
from .timeutil import format_utc


def publish_reviewed_report(review_id: str) -> dict:
    """Manually promote one approved draft without displacing legacy or LLM work."""
    with get_db() as db:
        row = db.execute(
            """SELECT review.*,a.status AS attempt_status,a.validated_draft_json,
                      a.raw_response_ref,a.raw_response_sha256,a.finished_at,
                      r.input_snapshot_id,r.dataset_id,r.provider,r.requested_model,
                      r.prompt_template_id,r.prompt_sha256,r.rendered_prompt_ref,
                      r.rendered_prompt_sha256,s.report_key,s.report_date,s.as_of
               FROM report_generation_reviews review
               JOIN report_generation_attempts a ON a.id=review.attempt_id
               JOIN report_generation_runs r ON r.id=a.run_id
               JOIN report_input_snapshots s ON s.id=r.input_snapshot_id
               WHERE review.id=?""", (review_id,)
        ).fetchone()
    if row is None:
        raise ValueError("report review is missing")
    if row["decision"] != "approved" or row["attempt_status"] != "valid_draft":
        raise ValueError("report draft has no valid approval")
    preview = review_preview(row["attempt_id"])
    if row["draft_sha256"] != preview["review_digest"]:
        raise ValueError("report review digest does not match the validated draft")
    manifest = load_frozen_manifest(row["input_snapshot_id"])
    draft = json.loads(row["validated_draft_json"])
    content, citations, coverage = render_validated_draft(manifest, draft)
    coverage.update({
        "review_status": "approved", "review_id": review_id,
        "reviewed_at": row["reviewed_at"], "generation_attempt_id": row["attempt_id"],
    })
    available_at = format_utc(datetime.now(timezone.utc))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        # The rows are immutable; reverify CAS while holding the publication transaction.
        verify_payload(row["rendered_prompt_ref"], row["rendered_prompt_sha256"])
        verify_payload(row["raw_response_ref"], row["raw_response_sha256"])
        existing = db.execute(
            "SELECT id,version FROM report_versions WHERE generation_attempt_id=?",
            (row["attempt_id"],),
        ).fetchone()
        if existing:
            return {"status": "already_exists", "version_id": existing["id"],
                    "version": existing["version"]}
        if db.execute("SELECT 1 FROM daily_reports WHERE date=?", (row["report_date"],)).fetchone():
            return {"status": "legacy_preserved"}
        current = db.execute(
            """SELECT v.id,v.mode FROM report_publications p
               JOIN report_versions v ON v.id=p.current_version_id
               WHERE p.dataset_id=? AND p.report_key=?""",
            (row["dataset_id"], row["report_key"]),
        ).fetchone()
        if current and current["mode"] == "llm":
            return {"status": "preserved_llm", "version_id": current["id"]}
        latest = db.execute(
            """SELECT v.id,v.version,v.mode,s.as_of FROM report_versions v
               JOIN report_input_snapshots s ON s.id=v.input_snapshot_id
               WHERE v.dataset_id=? AND v.report_key=?
               ORDER BY v.version DESC LIMIT 1""",
            (row["dataset_id"], row["report_key"]),
        ).fetchone()
        if latest and latest["mode"] == "llm":
            return {"status": "preserved_llm", "version_id": latest["id"]}
        if latest and row["as_of"] < latest["as_of"]:
            return {"status": "stale_input", "version_id": latest["id"]}
        version = 1 if latest is None else latest["version"] + 1
        version_id = str(uuid4())
        db.execute(
            """INSERT INTO report_versions(
                 id,dataset_id,report_key,version,input_snapshot_id,mode,content,
                 content_sha256,citations_json,coverage_json,provider,model,
                 prompt_template_id,prompt_sha256,generated_at,available_at,
                 supersedes_version_id,generation_attempt_id)
               VALUES(?,?,?,?,?,'llm',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version_id, row["dataset_id"], row["report_key"], version,
             row["input_snapshot_id"], content, _sha(content), _json(citations),
             _json(coverage), row["provider"], row["requested_model"],
             row["prompt_template_id"], row["prompt_sha256"], row["finished_at"],
             available_at, latest["id"] if latest else None, row["attempt_id"]),
        )
        db.execute(
            """INSERT INTO report_publications(dataset_id,report_key,current_version_id,published_at)
               VALUES(?,?,?,?) ON CONFLICT(dataset_id,report_key) DO UPDATE SET
                 current_version_id=excluded.current_version_id,published_at=excluded.published_at""",
            (row["dataset_id"], row["report_key"], version_id, available_at),
        )
    return {"status": "published", "version_id": version_id,
            "version": version, "citation_count": len(citations)}
