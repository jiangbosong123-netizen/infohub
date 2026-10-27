"""Record stretches when the model judged nothing (10-27).

On 10-27 the model account ran out of balance: from 01:09:24Z every scoring request failed, and
items kept arriving without importance or mood until the owner topped up (first judgement again
at 12:57:04Z). Consumers counting judged items need to know such stretches the way they know
collection gaps. A scoring round in which every request failed opens a gap (if none is open); the
first round with an answer closes it. A gap shorter than MIN_GAP (one network blip) is dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MIN_GAP = timedelta(minutes=30)
REASONS = {
    "quota": "模型账户余额不足",
    "auth": "模型密钥无效",
    "rate_limited": "模型请求被限流",
    "connection": "连不上模型服务",
    "timeout": "模型服务超时",
    "server": "模型服务出错",
}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def round_failed(db, kind: str | None, now: datetime | None = None) -> None:
    """Every request of a scoring round failed: open a gap unless one is open."""
    if db.execute("SELECT 1 FROM judging_gaps WHERE ended_at IS NULL").fetchone():
        return
    reason = "自动记录：" + REASONS.get(kind or "", "模型请求全部失败")
    db.execute("INSERT OR IGNORE INTO judging_gaps(started_at,reason) VALUES(?,?)",
               (_stamp(now or datetime.now(timezone.utc)), reason))


def round_answered(db, now: datetime | None = None) -> None:
    """A scoring round got answers: close the open gap, or drop it if it was only a blip."""
    row = db.execute("SELECT started_at FROM judging_gaps WHERE ended_at IS NULL").fetchone()
    if row is None:
        return
    current = now or datetime.now(timezone.utc)
    if current - _time(row[0]) < MIN_GAP:
        db.execute("DELETE FROM judging_gaps WHERE started_at=?", (row[0],))
    else:
        db.execute("UPDATE judging_gaps SET ended_at=? WHERE started_at=?", (_stamp(current), row[0]))


def listed(db, now: datetime | None = None) -> list[dict]:
    """Closed gaps, and an open one once it is MIN_GAP old (``to`` is then null)."""
    current = now or datetime.now(timezone.utc)
    out = []
    for started, ended, reason in db.execute(
            "SELECT started_at, ended_at, reason FROM judging_gaps ORDER BY started_at"):
        if ended is None and current - _time(started) < MIN_GAP:
            continue
        out.append({"from": started, "to": ended, "reason": reason})
    return out
