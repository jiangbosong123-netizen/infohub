from __future__ import annotations

"""Maintenance-only human review over an immutable, CAS-verified report draft."""

import hashlib
import json
from datetime import datetime, timezone

from .database import get_db
from .ingest import verify_payload
from .report_drafts import render_validated_draft
from .report_inputs import _json, load_frozen_manifest
from .timeutil import format_utc


def review_preview(attempt_id: str) -> dict:
    """Return exact claims and frozen sources for a human to inspect externally."""
    with get_db() as db:
        row = db.execute(
            """SELECT a.*,r.input_snapshot_id,r.rendered_prompt_ref,
                      r.rendered_prompt_sha256,r.provider,r.requested_model
               FROM report_generation_attempts a
               JOIN report_generation_runs r ON r.id=a.run_id WHERE a.id=?""",
            (attempt_id,),
        ).fetchone()
    if row is None:
        raise ValueError("report generation attempt is missing")
    verify_payload(row["rendered_prompt_ref"], row["rendered_prompt_sha256"])
    if not row["raw_response_ref"] or not row["raw_response_sha256"]:
        raise ValueError("report generation response is missing")
    raw = verify_payload(row["raw_response_ref"], row["raw_response_sha256"]).read_bytes()
    manifest = load_frozen_manifest(row["input_snapshot_id"])
    preview = {
        "attempt_id": attempt_id, "status": row["status"],
        "provider": row["provider"], "requested_model": row["requested_model"],
        "resolved_model": row["resolved_model"], "date": manifest["date"],
        "input_snapshot_id": row["input_snapshot_id"], "as_of": manifest["as_of"],
        "point_in_time_status": manifest["point_in_time_status"],
        "claims": [],
    }
    if row["status"] != "valid_draft":
        preview["review_digest"] = row["raw_response_sha256"]
        return preview
    try:
        draft = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("validated report draft does not match raw response") from exc
    serialized = _json(draft)
    if serialized != row["validated_draft_json"]:
        raise ValueError("validated report draft does not match raw response")
    _, citations, _ = render_validated_draft(manifest, draft)
    validation = json.loads(row["validation_report_json"])
    if (validation.get("status") != "valid_draft"
            or validation.get("citation_count") != len(citations)):
        raise ValueError("report draft validation record does not match")
    preview["review_digest"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    for section in draft["sections"]:
        for claim in section["claims"]:
            preview["claims"].append({
                "channel": section["channel"], "text": claim["text"],
                "sources": [{
                    "input_ordinal": ordinal,
                    "title": manifest["items"][ordinal]["title_display"]
                             or manifest["items"][ordinal]["title_original"],
                    "source_name": manifest["items"][ordinal]["source_name"],
                    "source_url": manifest["items"][ordinal]["url"],
                } for ordinal in claim["input_ordinals"]],
            })
    return preview


def record_manual_review(
    *, attempt_id: str, decision: str, expected_digest: str,
    reviewer_id: str, reason: str,
) -> dict:
    """Record one explicit, self-attested operator decision; never auto-approve."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("review decision must be approved or rejected")
    reviewer_id = reviewer_id.strip() if isinstance(reviewer_id, str) else ""
    reason = reason.strip() if isinstance(reason, str) else ""
    if not 1 <= len(reviewer_id) <= 200 or not 1 <= len(reason) <= 1000:
        raise ValueError("reviewer and reason are required within length limits")
    preview = review_preview(attempt_id)
    if expected_digest != preview["review_digest"]:
        raise ValueError("report draft changed since preview")
    if decision == "approved" and preview["status"] != "valid_draft":
        raise ValueError("invalid report draft cannot be approved")
    review_id = "report_review_" + hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM report_generation_reviews WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing:
            if (existing["decision"], existing["reviewer_id"], existing["reason"],
                    existing["draft_sha256"]) != (decision, reviewer_id, reason, expected_digest):
                raise ValueError("report draft already has a different review decision")
            return {"status": "already_reviewed", "review_id": existing["id"],
                    "decision": decision}
        # The attempt and its hashes are immutable; verify the response CAS again
        # while holding the publication-side transaction.
        attempt = db.execute(
            """SELECT a.raw_response_ref,a.raw_response_sha256,
                      r.rendered_prompt_ref,r.rendered_prompt_sha256
               FROM report_generation_attempts a
               JOIN report_generation_runs r ON r.id=a.run_id WHERE a.id=?""",
            (attempt_id,),
        ).fetchone()
        verify_payload(attempt["rendered_prompt_ref"], attempt["rendered_prompt_sha256"])
        verify_payload(attempt["raw_response_ref"], attempt["raw_response_sha256"])
        db.execute(
            """INSERT INTO report_generation_reviews(
                 id,attempt_id,decision,review_type,reviewer_id,reason,draft_sha256,reviewed_at)
               VALUES(?,?,?,'manual_source_check',?,?,?,?)""",
            (review_id, attempt_id, decision, reviewer_id, reason,
             expected_digest, format_utc(datetime.now(timezone.utc))),
        )
    return {"status": "recorded", "review_id": review_id, "decision": decision}
