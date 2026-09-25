from __future__ import annotations

"""Read current document curation publications for the legacy portal views."""

import json
import sqlite3
from collections.abc import Iterable

from .analysis_runs import AnalysisRunError
from .curation_contracts import CURATION_SCHEMAS, PUBLISHABLE_STATUSES, validate_curation_data


def published_curation(db: sqlite3.Connection, item_ids: Iterable[int]) -> dict[int, dict[str, dict]]:
    """Read only pointers for the document's current version, in one bounded query."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return {}
    if len(ids) > 500:
        raise ValueError("curation projection batch exceeds 500 items")
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT d.legacy_item_id, d.current_version_id, p.task_type,
                   v.subject_type AS version_subject_type, v.task_type AS version_task_type,
                   v.subject_version_id, v.review_status, v.evidence_status,
                   r.schema_version, r.result_status, r.validated_output_json
            FROM documents d
            JOIN analysis_publications p ON p.subject_type='document'
                AND p.subject_version_id=d.current_version_id
            JOIN analysis_publication_versions v ON v.id=p.current_publication_id
            JOIN analysis_results r ON r.id=v.result_id
            WHERE d.legacy_item_id IN ({marks}) AND d.status='active'""",
        ids,
    )
    projected: dict[int, dict[str, dict]] = {}
    for row in rows:
        task = row["task_type"]
        if task not in CURATION_SCHEMAS:
            continue
        item_id = row["legacy_item_id"]
        # A pointer to an invalid or rejected publication must not revive stale
        # legacy AI text. Record the task even when its payload cannot display.
        projected.setdefault(item_id, {})[task] = {"status": "unavailable"}
        if (
            row["schema_version"] != CURATION_SCHEMAS[task]
            or row["version_subject_type"] != "document"
            or row["version_task_type"] != task
            or row["subject_version_id"] != row["current_version_id"]
            or row["review_status"] == "rejected"
            or row["evidence_status"] not in {"supported", "partial"}
            or row["result_status"] not in PUBLISHABLE_STATUSES
        ):
            continue
        try:
            output = json.loads(row["validated_output_json"])
            if (
                output["schema_version"] != row["schema_version"]
                or output["subject"] != {"type": "document", "version_id": row["current_version_id"]}
                or output["status"] != row["result_status"]
            ):
                continue
            if not isinstance(output["evidence_ids"], list) or any(
                not isinstance(value, str) for value in output["evidence_ids"]
            ):
                continue
            data = validate_curation_data(
                task_type=task, schema_version=row["schema_version"],
                status=row["result_status"], data=output["data"],
                allowed_evidence=set(output["evidence_ids"]),
            )
        except (AnalysisRunError, KeyError, TypeError, ValueError):
            continue
        projected[item_id][task] = {
            "status": row["result_status"],
            "review_status": row["review_status"],
            "data": data,
        }
    return projected


def display_curation(row: dict, tasks: dict[str, dict] | None) -> dict:
    """Overlay current publications while retaining legacy values without a pointer."""
    item = dict(row)
    item["curation_needs_review"] = False
    if not tasks:
        return item
    for task, publication in tasks.items():
        status = publication["status"]
        if status not in PUBLISHABLE_STATUSES:
            if task == "translation":
                item["title_zh"] = ""
            elif task == "summarization":
                item["summary"] = ""
            elif task == "relevance":
                item["ai_cat"] = ""
            elif task == "importance":
                item["score"] = None
                item["reason"] = ""
            continue
        data = publication["data"]
        item["curation_needs_review"] |= (
            status == "needs_review" or publication["review_status"] == "unreviewed"
        )
        if task == "translation":
            item["title_zh"] = data["translated_title"]
        elif task == "summarization":
            item["summary"] = data["summary"]
        elif task == "relevance":
            item["ai_cat"] = data["ai_category"] or ""
        elif task == "importance":
            item["score"] = data["score"]
            item["reason"] = data["rationale"]
    return item
