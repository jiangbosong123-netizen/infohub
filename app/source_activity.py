from __future__ import annotations

"""When each source last listed something new, and how long it normally stays quiet.

A feed can keep answering with the same entries for months (several public feeds we evaluated
had not changed in about a year), and the health page then shows it as healthy. A source is
*silent* when it has listed nothing new for longer than 1.5 times its longest quiet spell in
the 14 days before its last new item, and never less than 12 hours, so nights, weekends and
slow official feeds are not flagged.

History is counted in listings, not items: everything a source first showed within the same
minute is one listing. A new source's first fetch lists its whole backlog at once (each Nasdaq
company feed brought about 14 items on 10-15), which says nothing about its rhythm; sources
with fewer than 10 listings in the window are not judged.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3

HISTORY = timedelta(days=14)
MIN_HISTORY_LISTINGS = 10
QUIET_FACTOR = 1.5
QUIET_FLOOR = timedelta(hours=12)


@dataclass(frozen=True)
class SourceActivity:
    last_new_at: datetime
    quiet_limit: timedelta | None  # None: too little history to judge

    def silent(self, now: datetime) -> bool:
        return self.quiet_limit is not None and now - self.last_new_at > self.quiet_limit


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def source_activity(db: sqlite3.Connection, source_ids: list[int]) -> dict[int, SourceActivity]:
    """Activity for each source that has listed anything (idx_item_discoveries_source_seen)."""
    out: dict[int, SourceActivity] = {}
    for source_id in source_ids:
        last = db.execute(
            "SELECT MAX(first_seen_at) FROM item_discoveries WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        if not last:
            continue
        last_new_at = _utc(last)
        count, longest_days = db.execute(
            """SELECT COUNT(*),MAX(gap) FROM (
                   SELECT julianday(seen)-julianday(LAG(seen) OVER (ORDER BY seen)) AS gap FROM (
                       SELECT MIN(first_seen_at) AS seen FROM item_discoveries
                       WHERE source_id=? AND first_seen_at>=? GROUP BY substr(first_seen_at,1,16)))""",
            (source_id, (last_new_at - HISTORY).isoformat()),
        ).fetchone()
        limit = None
        if count >= MIN_HISTORY_LISTINGS and longest_days is not None:
            limit = max(QUIET_FLOOR, timedelta(days=longest_days) * QUIET_FACTOR)
        out[source_id] = SourceActivity(last_new_at, limit)
    return out
