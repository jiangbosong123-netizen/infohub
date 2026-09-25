from __future__ import annotations

"""Freeze the current calendar-day report selection without generating prose."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import config
from .curation_projection import display_curation, published_curation
from .curation_query import CURATION_FILTER_CTE
from .database import get_db
from .provenance import display_title
from .timeutil import format_utc

SELECTION_VERSION = "calendar-daily-selection-v2"
MAX_ITEMS = 120
CHANNEL_FLOOR = 20
REPORT_CHANNELS = ("stock", "ai", "robot")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def calendar_window(date_str: str) -> tuple[str, str]:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    if day.strftime("%Y-%m-%d") != date_str:
        raise ValueError("date must be YYYY-MM-DD")
    start = day.replace(tzinfo=config.APP_TZ)
    return format_utc(start), format_utc(start + timedelta(days=1))


def load_frozen_manifest(snapshot_id: str) -> dict:
    """Reject incomplete or mismatched material before a later generator uses it."""
    with get_db() as db:
        snapshot = db.execute(
            "SELECT * FROM report_input_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise ValueError("unknown report input snapshot")
        serialized = snapshot["manifest_json"]
        if _sha(serialized) != snapshot["manifest_sha256"]:
            raise ValueError("report input manifest digest mismatch")
        manifest = json.loads(serialized)
        if (not isinstance(manifest, dict)
                or manifest.get("report_key") != snapshot["report_key"]
                or manifest.get("as_of") != snapshot["as_of"]
                or manifest.get("window_start") != snapshot["window_start"]
                or manifest.get("window_end") != snapshot["window_end"]
                or not isinstance(manifest.get("items"), list)
                or len(manifest["items"]) != snapshot["input_count"]):
            raise ValueError("report input manifest metadata mismatch")
        members = db.execute(
            "SELECT * FROM report_input_members WHERE snapshot_id=? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        if len(members) != len(manifest["items"]):
            raise ValueError("report input member count mismatch")
        for index, (member, material) in enumerate(zip(members, manifest["items"])):
            if (member["ordinal"] != index or not isinstance(material, dict)
                    or member["legacy_item_id"] != material.get("item_id")
                    or member["document_version_id"] != material.get("document_version_id")
                    or member["material_sha256"] != _sha(_json(material))):
                raise ValueError("report input member digest or reference mismatch")
        return manifest


def freeze_calendar_daily(date_str: str, *, now: datetime | None = None) -> dict | None:
    """Atomically save the exact selected material; no report or legacy row changes."""
    as_of = format_utc(now or datetime.now(timezone.utc))
    window_start, window_end = calendar_window(date_str)
    report_key = f"calendar_daily:{date_str}:{config.APP_TZ}"
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        dataset_id = db.execute("SELECT dataset_id FROM dataset_state WHERE singleton=1").fetchone()[0]
        candidates = db.execute(
            CURATION_FILTER_CTE + """ ,ranked AS (
                SELECT i.*,s.name AS source_name,
                       cv.score AS curation_score,d.current_version_id AS document_version_id,
                       (SELECT story_id FROM story_items WHERE item_id=i.id) AS story_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY i.channel
                           ORDER BY COALESCE(cv.score,50) DESC,i.heat DESC,i.id DESC
                       ) AS channel_rank
                FROM items i JOIN sources s ON s.id=i.source_id
                JOIN curation_values cv ON cv.item_id=i.id
                LEFT JOIN documents d ON d.legacy_item_id=i.id AND d.status='active'
                WHERE julianday(i.published_at)>=julianday(?)
                  AND julianday(i.published_at)<julianday(?)
                  AND julianday(i.fetched_at)<=julianday(?) AND cv.visible=1
            ) SELECT * FROM ranked WHERE channel_rank<=?
              ORDER BY COALESCE(curation_score,50) DESC,heat DESC,id DESC""",
            (window_start, window_end, as_of, MAX_ITEMS),
        ).fetchall()
        if not candidates:
            return None
        reserved = {
            row["id"] for row in candidates
            if row["channel"] in REPORT_CHANNELS and row["channel_rank"] <= CHANNEL_FLOOR
        }
        selected_ids = set(reserved)
        for row in candidates:
            if len(selected_ids) >= MAX_ITEMS:
                break
            selected_ids.add(row["id"])
        rows = [row for row in candidates if row["id"] in selected_ids]
        eligible_by_channel = {
            row["channel"]: row["count"]
            for row in db.execute(
                CURATION_FILTER_CTE + """ SELECT i.channel,COUNT(*) AS count
                    FROM items i JOIN curation_values cv ON cv.item_id=i.id
                    WHERE julianday(i.published_at)>=julianday(?)
                      AND julianday(i.published_at)<julianday(?)
                      AND julianday(i.fetched_at)<=julianday(?) AND cv.visible=1
                    GROUP BY i.channel""",
                (window_start, window_end, as_of),
            )
        }
        ids = [row["id"] for row in rows]
        publications = published_curation(db, ids)
        marks = ",".join("?" for _ in ids)
        pointers = {}
        for row in db.execute(
            f"""SELECT d.legacy_item_id,p.task_type,p.current_publication_id
                FROM documents d JOIN analysis_publications p
                  ON p.subject_type='document' AND p.subject_version_id=d.current_version_id
                WHERE d.legacy_item_id IN ({marks}) AND d.status='active'""", ids
        ):
            pointers.setdefault(row["legacy_item_id"], {})[row["task_type"]] = row["current_publication_id"]
        materials = []
        selected_by_channel: dict[str, int] = {}
        for row in rows:
            item = display_curation(dict(row), publications.get(row["id"]))
            item["score"] = row["curation_score"]
            materials.append({
                "item_id": row["id"],
                "document_version_id": row["document_version_id"],
                "story_id": row["story_id"],
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "url": row["url"],
                "title_original": row["title"],
                "title_display": display_title(item),
                "summary_display": item["summary"],
                "channel": row["channel"],
                "event_type": row["event_type"],
                "score": item["score"],
                "official": row["official"],
                "companies": row["companies"],
                "published_at_legacy": row["published_at"],
                "fetched_at_legacy": row["fetched_at"],
                "curation_publication_ids": pointers.get(row["id"], {}),
            })
            selected_by_channel[row["channel"]] = selected_by_channel.get(row["channel"], 0) + 1
        manifest = {
            "schema_version": "infohub.report-input/1.0",
            "selection_version": SELECTION_VERSION,
            "report_type": "calendar_daily",
            "report_key": report_key,
            "date": date_str,
            "window_start": window_start,
            "window_end": window_end,
            "window_basis": "calendar_day",
            "timezone": str(config.APP_TZ),
            "calendar_id": None,
            "calendar_version": None,
            "as_of": as_of,
            "knowledge_checkpoint_id": None,
            "point_in_time_status": "legacy_mutable_unverified",
            "selection_limit": MAX_ITEMS,
            "selection_order": "channel_floor_20_then_score_heat_item_id",
            "channel_floor": CHANNEL_FLOOR,
            "coverage": {
                "eligible_by_channel": eligible_by_channel,
                "selected_by_channel": selected_by_channel,
                "truncated_by_global_limit": sum(eligible_by_channel.values()) > MAX_ITEMS,
            },
            "items": materials,
        }
        serialized = _json(manifest)
        digest = _sha(serialized)
        existing = db.execute(
            """SELECT id FROM report_input_snapshots
               WHERE dataset_id=? AND report_key=? AND manifest_sha256=?""",
            (dataset_id, report_key, digest),
        ).fetchone()
        if existing:
            return {"snapshot_id": existing["id"], "input_count": len(materials),
                    "manifest_sha256": digest, "created": False}
        snapshot_id = str(uuid4())
        db.execute(
            """INSERT INTO report_input_snapshots(
                 id,dataset_id,report_key,report_type,report_date,window_start,window_end,
                 window_basis,timezone,as_of,manifest_json,manifest_sha256,input_count,created_at)
               VALUES(?,?,?,'calendar_daily',?,?,?,'calendar_day',?,?,?,?,?,?)""",
            (snapshot_id, dataset_id, report_key, date_str, window_start, window_end,
             str(config.APP_TZ), as_of, serialized, digest, len(materials), as_of),
        )
        db.executemany(
            """INSERT INTO report_input_members(
                 snapshot_id,ordinal,legacy_item_id,document_version_id,material_sha256)
               VALUES(?,?,?,?,?)""",
            [(snapshot_id, index, material["item_id"], material["document_version_id"],
              _sha(_json(material))) for index, material in enumerate(materials)],
        )
        return {"snapshot_id": snapshot_id, "input_count": len(materials),
                "manifest_sha256": digest, "created": True}
