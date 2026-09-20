from __future__ import annotations

"""Publish a cited, deterministic fallback from an immutable report input."""

import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from .database import get_db
from .report_inputs import _json, _sha, load_frozen_manifest
from .timeutil import format_utc, parse_utc

CHANNELS = (("stock", "股市 · 科技企业"), ("ai", "AI"), ("robot", "机器人"))
MAX_PER_CHANNEL = 20


def _label(value: object) -> str:
    escaped = html.escape(str(value or ""), quote=False)
    return re.sub(r"([\\`*_[\]{}()#+!|])", r"\\\1", escaped)


def _source_url(value: object) -> str:
    if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 32 or ch in "<>\\" for ch in value):
        raise ValueError("report source URL is unsafe")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError("report source URL must be HTTP(S)")
    return value


def structured_report(manifest: dict) -> tuple[str, list[dict], dict]:
    """Build prose and machine citations exclusively from a validated snapshot."""
    if manifest.get("schema_version") != "infohub.report-input/1.0":
        raise ValueError("unsupported report input schema")
    if manifest.get("report_type") != "calendar_daily":
        raise ValueError("unsupported report type")
    timezone = ZoneInfo(manifest["timezone"])
    materials = manifest["items"]
    if not materials:
        raise ValueError("cannot publish an empty report")
    lines = [f"# 行业日报 · {_label(manifest['date'])}", "",
             "结构化摘要。素材为生成时冻结的已采集报道；旧数据的历史可见时点未经核实。", ""]
    citations: list[dict] = []
    reported: dict[str, int] = {}
    for channel, name in CHANNELS:
        lines.extend((f"## {name}", ""))
        matching = [(ordinal, material) for ordinal, material in enumerate(materials)
                    if material["channel"] == channel][:MAX_PER_CHANNEL]
        reported[channel] = len(matching)
        if not matching:
            lines.extend(("本窗口无入选材料。", ""))
            continue
        for ordinal, material in matching:
            url = _source_url(material["url"])
            try:
                time_label = parse_utc(material["published_at_legacy"]).astimezone(timezone).strftime("%H:%M")
            except (TypeError, ValueError):
                time_label = "时间未核实"
            citation_number = len(citations) + 1
            title = _label(material["title_display"] or material["title_original"])
            source = _label(material["source_name"])
            lines.append(f"- **{time_label}** [{title}](<{url}>) — {source}【{citation_number}】")
            citations.append({
                "number": citation_number,
                "input_ordinal": ordinal,
                "legacy_item_id": material["item_id"],
                "document_version_id": material["document_version_id"],
                "source_url": url,
            })
        lines.append("")
    if not citations:
        raise ValueError("report has no supported channel citations")
    coverage = {
        "schema_version": "infohub.report-coverage/1.0",
        "selection": manifest["coverage"],
        "reported_by_channel": reported,
        "citation_count": len(citations),
        "point_in_time_status": manifest["point_in_time_status"],
        "method": "structured_fallback_from_frozen_input",
    }
    return "\n".join(lines).rstrip() + "\n", citations, coverage


def publish_structured_report(snapshot_id: str) -> dict:
    """Append one version and move the pointer, preserving any published LLM."""
    manifest = load_frozen_manifest(snapshot_id)
    content, citations, coverage = structured_report(manifest)
    generated_at = format_utc(datetime.now(timezone.utc))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = db.execute(
            "SELECT dataset_id,report_key,as_of FROM report_input_snapshots WHERE id=?",
            (snapshot_id,),
        ).fetchone()
        if snapshot is None or snapshot["report_key"] != manifest["report_key"]:
            raise ValueError("report input snapshot changed")
        existing = db.execute(
            """SELECT id,version FROM report_versions
               WHERE dataset_id=? AND report_key=? AND input_snapshot_id=?
                 AND mode='structured_fallback' ORDER BY version DESC LIMIT 1""",
            (snapshot["dataset_id"], snapshot["report_key"], snapshot_id),
        ).fetchone()
        if existing:
            return {"status": "already_exists", "version_id": existing["id"],
                    "version": existing["version"]}
        current = db.execute(
            """SELECT v.id,v.mode,s.as_of FROM report_publications p
               JOIN report_versions v ON v.id=p.current_version_id
               JOIN report_input_snapshots s ON s.id=v.input_snapshot_id
               WHERE p.dataset_id=? AND p.report_key=?""",
            (snapshot["dataset_id"], snapshot["report_key"]),
        ).fetchone()
        if current and current["mode"] == "llm":
            return {"status": "preserved_llm", "version_id": current["id"]}
        latest = db.execute(
            """SELECT v.id,v.version,s.as_of FROM report_versions v
               JOIN report_input_snapshots s ON s.id=v.input_snapshot_id
               WHERE v.dataset_id=? AND v.report_key=?
               ORDER BY v.version DESC LIMIT 1""",
            (snapshot["dataset_id"], snapshot["report_key"]),
        ).fetchone()
        if latest and snapshot["as_of"] <= latest["as_of"]:
            return {"status": "stale_input", "version_id": latest["id"]}
        version = 1 if latest is None else latest["version"] + 1
        version_id = str(uuid4())
        db.execute(
            """INSERT INTO report_versions(
                 id,dataset_id,report_key,version,input_snapshot_id,mode,content,
                 content_sha256,citations_json,coverage_json,generated_at,available_at,
                 supersedes_version_id)
               VALUES(?,?,?,?,?,'structured_fallback',?,?,?,?,?,?,?)""",
            (version_id, snapshot["dataset_id"], snapshot["report_key"], version,
             snapshot_id, content, _sha(content), _json(citations), _json(coverage),
             generated_at, generated_at, latest["id"] if latest else None),
        )
        db.execute(
            """INSERT INTO report_publications(dataset_id,report_key,current_version_id,published_at)
               VALUES(?,?,?,?) ON CONFLICT(dataset_id,report_key) DO UPDATE SET
                 current_version_id=excluded.current_version_id,published_at=excluded.published_at""",
            (snapshot["dataset_id"], snapshot["report_key"], version_id, generated_at),
        )
        return {"status": "published", "version_id": version_id, "version": version,
                "citation_count": len(citations)}
