from __future__ import annotations

"""Transactional, resumable hotspot metrics for the legacy portal bridge."""

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .curation_projection import display_curation, published_curation
from .curation_query import CURATION_FILTER_CTE
from .database import get_db
from .provenance import display_title, publisher
from .ranking import item_heat
from .timeutil import format_utc

METRIC_VERSION = "curation-story-metrics-v1"


@dataclass(frozen=True)
class HotMetricsBuildReport:
    status: str
    generation: int
    last_story_id: str
    indexed_count: int
    scanned: int
    refreshed: int
    dirty_remaining: int

    def to_dict(self) -> dict:
        return asdict(self)


def _company_slugs(rows: list[dict]) -> str:
    slugs: set[str] = set()
    for row in rows:
        try:
            values = json.loads(row.get("companies") or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(values, list):
            slugs.update(value for value in values if isinstance(value, str) and value)
    return json.dumps(sorted(slugs), ensure_ascii=False)


def _refresh_stories(db: sqlite3.Connection, story_ids: list[str], now: datetime) -> None:
    if not story_ids:
        return
    marks = ",".join("?" for _ in story_ids)
    stories = {row["id"]: row for row in db.execute(
        f"SELECT id,redirect_to FROM stories WHERE id IN ({marks})", story_ids
    )}
    rows = db.execute(
        CURATION_FILTER_CTE + f""" SELECT si.story_id,i.*,s.name AS source_name,
                        cv.score AS curation_score
            FROM story_items si JOIN items i ON i.id=si.item_id
            JOIN sources s ON s.id=i.source_id
            JOIN curation_values cv ON cv.item_id=i.id
            WHERE si.story_id IN ({marks}) AND cv.visible=1""",
        story_ids,
    ).fetchall()
    item_ids = list(dict.fromkeys(row["id"] for row in rows))
    publications: dict[int, dict[str, dict]] = {}
    for start in range(0, len(item_ids), 500):
        publications.update(published_curation(db, item_ids[start:start + 500]))
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        value = display_curation(dict(row), publications.get(row["id"]))
        # Visibility and score are decided by the same SQL projection as the
        # portal before grouping; don't accidentally revive a legacy score.
        value["score"] = row["curation_score"]
        groups[row["story_id"]].append(value)
    timestamp = format_utc(now)
    for story_id in story_ids:
        story = stories.get(story_id)
        if story is None:
            db.execute("DELETE FROM curation_story_metrics WHERE story_id=?", (story_id,))
            continue
        members = [] if story["redirect_to"] else groups.get(story_id, [])
        if members:
            known = {key for key, _, is_known in (publisher(row) for row in members) if is_known}
            representative = max(
                members,
                key=lambda row: (row["official"], row["score"] if row["score"] is not None else 0,
                                 row["published_at"], row["id"]),
            )
            heat = round(max(item_heat(row, now) for row in members)
                         * (1 + 0.2 * min(max(0, len(known) - 1), 5)), 4)
            values = (len(members), len(known), heat, representative["id"],
                      display_title(representative), representative["url"],
                      _company_slugs(members), max(row["published_at"] for row in members))
        else:
            values = (0, 0, 0.0, None, "", "", "[]", None)
        db.execute(
            """INSERT INTO curation_story_metrics(
                   story_id,metric_schema_version,visible_item_count,publisher_count,
                   heat,representative_item_id,title_display,url_display,company_slugs,
                   last_visible_at,computed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(story_id) DO UPDATE SET
                   visible_item_count=excluded.visible_item_count,
                   publisher_count=excluded.publisher_count,heat=excluded.heat,
                   representative_item_id=excluded.representative_item_id,
                   title_display=excluded.title_display,url_display=excluded.url_display,
                   company_slugs=excluded.company_slugs,last_visible_at=excluded.last_visible_at,
                   computed_at=excluded.computed_at""",
            (story_id, METRIC_VERSION, *values, timestamp),
        )
    db.executemany("DELETE FROM curation_story_metrics_dirty WHERE story_id=?", [(sid,) for sid in story_ids])


def advance_hot_metrics(limit: int = 100) -> HotMetricsBuildReport:
    """Commit one bounded scan or dirty-queue batch, then report the cursor."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT * FROM curation_story_metrics_state WHERE singleton=1").fetchone()
        if state is None:
            raise RuntimeError("hot metrics schema has not been migrated")
        status, generation, cursor = state["status"], state["generation"], state["last_story_id"]
        if status == "empty":
            if cursor or db.execute("SELECT 1 FROM curation_story_metrics LIMIT 1").fetchone():
                raise RuntimeError("empty hot metrics state contains indexed rows or a cursor")
            generation += 1
            status = "building"
        scanned = refreshed = 0
        now = datetime.now(timezone.utc)
        if status == "building":
            ids = [row[0] for row in db.execute(
                "SELECT id FROM stories WHERE id>? ORDER BY id LIMIT ?", (cursor, limit)
            )]
            if ids:
                _refresh_stories(db, ids, now)
                scanned, cursor = len(ids), ids[-1]
        if not scanned:
            dirty = [row[0] for row in db.execute(
                "SELECT story_id FROM curation_story_metrics_dirty ORDER BY story_id LIMIT ?", (limit,)
            )]
            if dirty:
                _refresh_stories(db, dirty, now)
                refreshed = len(dirty)
        dirty_remaining = db.execute("SELECT COUNT(*) FROM curation_story_metrics_dirty").fetchone()[0]
        if status == "building" and not scanned and not dirty_remaining:
            if db.execute("SELECT 1 FROM stories WHERE id>? LIMIT 1", (cursor,)).fetchone() is None:
                status = "ready"
        count = db.execute("SELECT COUNT(*) FROM curation_story_metrics").fetchone()[0]
        db.execute(
            """UPDATE curation_story_metrics_state SET generation=?,status=?,last_story_id=?,
                      indexed_count=?,updated_at=? WHERE singleton=1""",
            (generation, status, cursor, count, format_utc(now)),
        )
        return HotMetricsBuildReport(status, generation, cursor, count, scanned, refreshed, dirty_remaining)
