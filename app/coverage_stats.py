from __future__ import annotations

"""The four numbers each coverage tier is judged on (D31, docs/RUNBOOK.md "覆盖范围"):
items per day, model usage, database growth and, by hand, linking accuracy.

Days are calendar days in APP_TZ. ``record_size`` runs after every crawl cycle and keeps the day's
last reading, so growth is the difference between consecutive days.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from . import config
from .database import get_db


def _day(now: datetime) -> str:
    return now.astimezone(config.APP_TZ).strftime("%Y-%m-%d")


def _offset_modifier(now: datetime) -> str:
    minutes = int(now.astimezone(config.APP_TZ).utcoffset().total_seconds() // 60)
    return f"{minutes:+d} minutes"


def record_size(now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    with get_db() as db:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        size = db.execute("PRAGMA page_size").fetchone()[0]
        items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        db.execute(
            """INSERT INTO database_size_daily(day,bytes,items,recorded_at) VALUES(?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET bytes=excluded.bytes,items=excluded.items,
                   recorded_at=excluded.recorded_at""",
            (_day(current), pages * size, items, current.isoformat()))


def summary(days: int = 14, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    start_day = _day(current - timedelta(days=days - 1))
    # Items are bucketed by their local fetch day; the range scan reads idx_items_fetched.
    since = (current - timedelta(days=days)).astimezone(timezone.utc).isoformat()
    shift = _offset_modifier(current)
    with get_db() as db:
        companies = {row["origin"] + ("" if row["covered"] else "_retired"): row["n"] for row in db.execute(
            "SELECT origin, covered, COUNT(*) AS n FROM companies GROUP BY origin, covered")}
        nasdaq_sources = db.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled=1 AND key LIKE 'nasdaq-%'").fetchone()[0]
        per_day: dict[str, dict] = {}
        for row in db.execute(
                """SELECT date(i.fetched_at, ?) AS day, COUNT(*) AS items,
                          SUM(s.key LIKE 'nasdaq-%') AS coverage_feed_items,
                          SUM(i.score IS NOT NULL) AS scored,
                          SUM(i.tmt=1) AS kept
                   FROM items i JOIN sources s ON s.id=i.source_id
                   WHERE i.fetched_at>=? GROUP BY day""", (shift, since)):
            per_day[row["day"]] = {key: row[key] or 0 for key in
                                   ("items", "coverage_feed_items", "scored", "kept")}
        for row in db.execute(
                # CROSS JOIN keeps the recent-items range first instead of scanning every link.
                """SELECT date(i.fetched_at, ?) AS day, COUNT(DISTINCT i.id) AS n
                   FROM items i CROSS JOIN item_companies ic ON ic.item_id=i.id
                   JOIN companies c ON c.id=ic.company_id AND c.origin='coverage'
                   WHERE i.fetched_at>=? GROUP BY day""", (shift, since)):
            per_day.setdefault(row["day"], {}).update(coverage_linked_items=row["n"])
        usage: dict[str, list] = {}
        for row in db.execute(
                """SELECT day,task,model,calls,failed_calls,prompt_tokens,completion_tokens,items
                   FROM ai_usage_daily WHERE day>=? ORDER BY day,task""", (start_day,)):
            usage.setdefault(row["day"], []).append({key: row[key] for key in row.keys() if key != "day"})
        failures: dict[str, list] = {}
        for row in db.execute(
                "SELECT day,task,kind,calls FROM ai_failures_daily WHERE day>=? ORDER BY day,task,kind",
                (start_day,)):
            failures.setdefault(row["day"], []).append({"task": row["task"], "kind": row["kind"],
                                                        "calls": row["calls"]})
        sizes = {row["day"]: {"bytes": row["bytes"], "items": row["items"]} for row in db.execute(
            "SELECT day,bytes,items FROM database_size_daily WHERE day>=? ORDER BY day",
            (_day(current - timedelta(days=days)),))}
    table = []
    for offset in range(days - 1, -1, -1):
        day = _day(current - timedelta(days=offset))
        size = sizes.get(day)
        prior = sizes.get(_day(current - timedelta(days=offset + 1)))
        entry = {"day": day, **{"items": 0, "coverage_feed_items": 0, "coverage_linked_items": 0,
                                "scored": 0, "kept": 0}, **per_day.get(day, {})}
        entry["ai"] = usage.get(day, [])
        entry["ai_failures"] = failures.get(day, [])
        entry["database_bytes"] = size["bytes"] if size else None
        entry["database_growth_bytes"] = (size["bytes"] - prior["bytes"]) if size and prior else None
        table.append(entry)
    return {
        "tier": config.COVERAGE_TIER,
        "companies": companies,
        "nasdaq_sources_enabled": nasdaq_sources,
        "timezone": str(config.APP_TZ),
        "days": table,
        "checked_at": current.isoformat(),
    }


def safe_record_size() -> None:
    try:
        record_size()
    except sqlite3.Error:
        pass  # an older schema or a busy database: the next cycle records it
