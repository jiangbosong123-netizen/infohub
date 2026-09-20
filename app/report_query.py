from __future__ import annotations

"""Guarded portal reads for published calendar-day report versions."""

import json
import sqlite3

from . import config
from .report_inputs import _sha, load_frozen_manifest
from .report_versions import _source_url


def published_calendar_dates(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT s.report_date AS date,v.version,v.mode,v.available_at AS created_at
           FROM report_publications p
           JOIN report_versions v ON v.id=p.current_version_id
             AND v.dataset_id=p.dataset_id AND v.report_key=p.report_key
           JOIN report_input_snapshots s ON s.id=v.input_snapshot_id
           JOIN dataset_state ds ON ds.singleton=1 AND ds.dataset_id=p.dataset_id
           WHERE s.report_type='calendar_daily' AND s.timezone=?
             AND v.mode IN ('llm','structured_fallback')
           ORDER BY s.report_date DESC""",
        (str(config.APP_TZ),),
    ).fetchall()
    return [dict(row) for row in rows]


def published_calendar_report(db: sqlite3.Connection, date: str) -> dict | None:
    key = f"calendar_daily:{date}:{config.APP_TZ}"
    row = db.execute(
        """SELECT v.*,s.report_date,s.as_of,s.timezone
           FROM report_publications p
           JOIN report_versions v ON v.id=p.current_version_id
             AND v.dataset_id=p.dataset_id AND v.report_key=p.report_key
           JOIN report_input_snapshots s ON s.id=v.input_snapshot_id
           JOIN dataset_state ds ON ds.singleton=1 AND ds.dataset_id=p.dataset_id
           WHERE p.report_key=? AND s.report_date=? AND s.timezone=?
             AND v.mode IN ('llm','structured_fallback')""",
        (key, date, str(config.APP_TZ)),
    ).fetchone()
    if row is None:
        return None
    manifest = load_frozen_manifest(row["input_snapshot_id"])
    if _sha(row["content"]) != row["content_sha256"]:
        raise ValueError("published report content digest mismatch")
    citations = json.loads(row["citations_json"])
    coverage = json.loads(row["coverage_json"])
    if not isinstance(citations, list) or not citations or not isinstance(coverage, dict):
        raise ValueError("published report citations or coverage are invalid")
    if coverage.get("schema_version") != "infohub.report-coverage/1.0":
        raise ValueError("published report coverage schema is unsupported")
    if coverage.get("point_in_time_status") != manifest.get("point_in_time_status"):
        raise ValueError("published report provenance status mismatch")
    for number, citation in enumerate(citations, 1):
        if not isinstance(citation, dict) or citation.get("number") != number:
            raise ValueError("published report citation order mismatch")
        ordinal = citation.get("input_ordinal")
        if not isinstance(ordinal, int) or not 0 <= ordinal < len(manifest["items"]):
            raise ValueError("published report citation input is missing")
        material = manifest["items"][ordinal]
        if (citation.get("legacy_item_id") != material["item_id"]
                or citation.get("document_version_id") != material["document_version_id"]
                or citation.get("source_url") != material["url"]):
            raise ValueError("published report citation source mismatch")
        _source_url(citation["source_url"])
        if row["mode"] == "structured_fallback" and f"【{number}】" not in row["content"]:
            raise ValueError("published report citation marker missing")
    return {"date": date, "content": row["content"], "mode": row["mode"],
            "version": row["version"], "as_of": row["as_of"],
            "citation_count": len(citations), "coverage": coverage}
