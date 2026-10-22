"""Record stretches when nothing was collected (10-22).

Consumers read ``collection_gaps`` to tell "no news" from "not collected" (news API, migration 61).
Only the gaps measured before 10-17 were in it: on 10-22 the Windows host slept from about
10:00Z to 19:08Z, nothing was fetched, and nothing said so. Every crawl cycle now first compares
the newest fetch_log run with the clock; more than GAP_AFTER between them is a gap, from that run
to now. Migration 65 adds the gaps fetch_log (kept 14 days) still shows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# A crawl tick runs every few minutes and some of the ~60 main sources are always due, so half an
# hour without a single run means the host or the worker was not working.
GAP_AFTER = timedelta(minutes=30)
REASON = "自动记录：{minutes} 分钟没有抓取任何来源（主机睡眠、断网或后台停止）"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(db, started: datetime, ended: datetime) -> dict | None:
    start, end = _stamp(started), _stamp(ended)
    if end <= start or db.execute("SELECT 1 FROM collection_gaps WHERE started_at<? AND ended_at>?",
                                  (end, start)).fetchone():
        return None  # already recorded, or inside a gap recorded by hand
    minutes = int((ended - started).total_seconds() // 60)
    db.execute("INSERT INTO collection_gaps(started_at,ended_at,reason) VALUES(?,?,?)",
               (start, end, REASON.format(minutes=minutes)))
    return {"from": start, "to": end, "minutes": minutes}


def record_open_gap(db, now: datetime | None = None) -> dict | None:
    """Before a crawl cycle: record the gap since the newest run if it is longer than GAP_AFTER."""
    row = db.execute("SELECT ran_at FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    last, current = _time(row[0]), now or datetime.now(timezone.utc)
    return _insert(db, last, current) if current - last > GAP_AFTER else None


def backfill(db) -> list[dict]:
    """Every gap longer than GAP_AFTER between consecutive fetch_log runs (migration 65)."""
    found, previous = [], None
    for (ran_at,) in db.execute("SELECT ran_at FROM fetch_log ORDER BY id"):
        current = _time(ran_at)
        if previous is not None and current - previous > GAP_AFTER:
            gap = _insert(db, previous, current)
            if gap:
                found.append(gap)
        previous = max(current, previous) if previous else current
    return found
