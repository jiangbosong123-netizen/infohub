from __future__ import annotations

"""When each source last listed something new, and how long it normally stays quiet.

A feed can keep answering with the same entries for months (several public feeds we evaluated
had not changed in about a year), and the health page then shows it as healthy. A source is
*silent* when it has listed nothing new for longer than 1.5 times its longest quiet spell in
the 14 days before its last new item, and never less than 12 hours, so nights, weekends and
slow official feeds are not flagged. Sources with fewer than 10 new items in that window are
not judged.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3

HISTORY = timedelta(days=14)
MIN_HISTORY_ITEMS = 10
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
                   SELECT julianday(first_seen_at)
                          -julianday(LAG(first_seen_at) OVER (ORDER BY first_seen_at)) AS gap
                   FROM item_discoveries WHERE source_id=? AND first_seen_at>=?)""",
            (source_id, (last_new_at - HISTORY).isoformat()),
        ).fetchone()
        limit = None
        if count >= MIN_HISTORY_ITEMS and longest_days is not None:
            limit = max(QUIET_FLOOR, timedelta(days=longest_days) * QUIET_FACTOR)
        out[source_id] = SourceActivity(last_new_at, limit)
    return out
