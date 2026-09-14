from __future__ import annotations

"""Stable read-only API for downstream NLP and larger-system integration."""
import base64
import binascii
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from .. import database
from ..provenance import display_title, publisher

router = APIRouter(prefix="/api/v1", tags=["integration"])
API_VERSION = "v1"


def _encode_cursor(timestamp: str, identifier) -> str:
    raw = json.dumps([timestamp, identifier], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str, id_type=int):
    try:
        padded = value + "=" * (-len(value) % 4)
        timestamp, identifier = json.loads(base64.urlsafe_b64decode(padded).decode())
        datetime.fromisoformat(timestamp)
        return str(timestamp), id_type(identifier)
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        raise HTTPException(400, "invalid cursor") from None


def _item_payloads(rows) -> list[dict]:
    ids = [row["id"] for row in rows]
    topic_map: dict[int, list[dict]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        with database.get_db() as db:
            for row in db.execute(f"""SELECT it.item_id,t.slug,t.name FROM item_topics it
                JOIN topics t ON t.slug=it.topic_slug
                WHERE t.enabled=1 AND it.item_id IN ({marks}) ORDER BY t.position""", ids):
                topic_map.setdefault(row["item_id"], []).append(
                    {"slug": row["slug"], "name": row["name"]})
    result = []
    for row in rows:
        value = dict(row)
        result.append({
            "id": value["id"],
            "url": value["url"],
            "title": display_title(value),
            "title_original": value["title"],
            "title_zh": value["title_zh"] if value["title_zh"] != "-" else "",
            "summary": value["summary"] or "",
            "summary_original": value.get("raw_summary") or "",
            "channel": value["channel"],
            "ai_category": value["ai_cat"] or None,
            "event_type": value["event_type"] or None,
            "score": value["score"] if value["score"] is not None and value["score"] >= 0 else None,
            "reason": value["reason"] or "",
            "official": bool(value["official"]),
            "publisher": publisher(value)[1],
            "crawl_source": value["source_name"],
            "companies": json.loads(value["companies"] or "[]"),
            "topics": topic_map.get(value["id"], []),
            "story_id": value.get("story_id"),
            "published_at": value["published_at"],
            "fetched_at": value["fetched_at"],
        })
    return result


def _item_base_sql() -> str:
    return """SELECT i.*,s.name AS source_name,si.story_id
        FROM items i JOIN sources s ON s.id=i.source_id
        LEFT JOIN story_items si ON si.item_id=i.id"""


@router.get("/items")
def list_items(limit: int = Query(50, ge=1, le=100), cursor: str = "",
               channel: str = "", topic: str = "", company: str = "",
               since: str = ""):
    if channel and channel not in {"ai", "robot", "stock"}:
        raise HTTPException(422, "invalid channel")
    where = ["COALESCE(i.tmt,1)!=0"]
    params: list = []
    if channel:
        where.append("i.channel=?")
        params.append(channel)
    if topic:
        where.append("EXISTS(SELECT 1 FROM item_topics fit WHERE fit.item_id=i.id AND fit.topic_slug=?)")
        params.append(topic)
    if company:
        where.append("EXISTS(SELECT 1 FROM item_companies fic JOIN companies fc ON fc.id=fic.company_id WHERE fic.item_id=i.id AND fc.slug=?)")
        params.append(company)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(422, "invalid since timestamp") from None
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        since = since_dt.astimezone(timezone.utc).isoformat()
        where.append("i.published_at>=?")
        params.append(since)
    if cursor:
        timestamp, item_id = _decode_cursor(cursor)
        where.append("(i.published_at<? OR (i.published_at=? AND i.id<?))")
        params.extend([timestamp, timestamp, item_id])
    sql = _item_base_sql() + " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.published_at DESC,i.id DESC LIMIT ?"
    params.append(limit + 1)
    with database.get_db() as db:
        rows = db.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1]["published_at"], rows[-1]["id"]) if has_more else None
    return {"api_version": API_VERSION, "data": _item_payloads(rows),
            "pagination": {"limit": limit, "next_cursor": next_cursor}}


@router.get("/items/{item_id}")
def get_item(item_id: int):
    with database.get_db() as db:
        row = db.execute(_item_base_sql() +
            " WHERE i.id=? AND COALESCE(i.tmt,1)!=0", (item_id,)).fetchone()
        analyses = db.execute("""SELECT analysis_type,pipeline_version,model,
            input_json,output_json,created_at FROM nlp_results
            WHERE item_id=? ORDER BY created_at DESC""", (item_id,)).fetchall()
    if not row:
        raise HTTPException(404, "item not found")
    payload = _item_payloads([row])[0]
    payload["analyses"] = [{
        "type": analysis["analysis_type"],
        "pipeline_version": analysis["pipeline_version"],
        "model": analysis["model"],
        "input": json.loads(analysis["input_json"]),
        "output": json.loads(analysis["output_json"]),
        "created_at": analysis["created_at"],
    } for analysis in analyses]
    return {"api_version": API_VERSION, "data": payload}


@router.get("/stories")
def list_stories(limit: int = Query(30, ge=1, le=100), cursor: str = "",
                 channel: str = ""):
    if channel and channel not in {"ai", "robot", "stock"}:
        raise HTTPException(422, "invalid channel")
    where = ["redirect_to IS NULL", "item_count>0"]
    params: list = []
    if channel:
        where.append("channel=?")
        params.append(channel)
    if cursor:
        timestamp, story_id = _decode_cursor(cursor, str)
        where.append("(last_at<? OR (last_at=? AND id<?))")
        params.extend([timestamp, timestamp, story_id])
    sql = "SELECT * FROM stories WHERE " + " AND ".join(where)
    sql += " ORDER BY last_at DESC,id DESC LIMIT ?"
    params.append(limit + 1)
    with database.get_db() as db:
        rows = db.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    data = [{
        "id": row["id"], "title": row["title"], "channel": row["channel"],
        "url": row["url"], "heat": row["heat"],
        "publisher_count": row["source_count"], "report_count": row["item_count"],
        "companies": json.loads(row["company_slugs"] or "[]"),
        "first_at": row["first_at"], "last_at": row["last_at"],
    } for row in rows]
    next_cursor = _encode_cursor(rows[-1]["last_at"], rows[-1]["id"]) if has_more else None
    return {"api_version": API_VERSION, "data": data,
            "pagination": {"limit": limit, "next_cursor": next_cursor}}


@router.get("/topics")
def list_topics():
    with database.get_db() as db:
        rows = db.execute("""SELECT t.slug,t.name,t.group_key,t.description,t.position,
            COUNT(i.id) AS item_count,MAX(i.published_at) AS last_item_at
            FROM topics t LEFT JOIN item_topics it ON it.topic_slug=t.slug
            LEFT JOIN items i ON i.id=it.item_id AND COALESCE(i.tmt,1)!=0
            WHERE t.enabled=1 GROUP BY t.slug ORDER BY t.position""").fetchall()
    return {"api_version": API_VERSION, "data": [dict(row) for row in rows]}
