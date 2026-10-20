from __future__ import annotations

"""Conditions that page the owner through the external heartbeat.

A finished crawl cycle proves the host and worker are alive, not that the system works: every
source can fail, the model can reject every call, or the disk can fill while cycles keep
finishing. The worker reports these conditions to the outside monitor as a failure, so the owner
hears about them without opening the health page. Every check is windowed, so one bad cycle or a
single slow item does not page anyone.
"""

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from . import config
from .database import get_db
from .disk_space import lowest_free
from .timeutil import format_utc, parse_utc

# More than this share of enabled sources failing on their latest attempt.
SOURCE_FAILURE_SHARE = 0.5
MIN_SOURCES = 5
# Items fetched in this window should have been scored by several AI ticks already.
AI_WINDOW = (timedelta(hours=6), timedelta(hours=1))
AI_UNSCORED_SHARE = 0.5
MIN_AI_ITEMS = 5
BACKUP_MAX_AGE = timedelta(hours=36)
FAILED_JOB_WINDOW = timedelta(hours=24)
# Crawls run in their own lane, so a job hung in the main lane (a model call, the backup) no
# longer stops the crawl cycles the outside monitor watches; its lease is renewed as long as the
# process lives. The longest normal job, the nightly backup, took 8 minutes at 4.9 GB.
JOB_STUCK_AFTER = timedelta(hours=2)
MB = 1024 * 1024
# The worker records its latest result here; the web role has no model credentials to check itself.
RESULT_FILE = "self-check.json"


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _sources(db) -> list[Problem]:
    total, failing = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(fail_count>0),0) FROM sources WHERE enabled=1"
    ).fetchone()
    if total >= MIN_SOURCES and failing > total * SOURCE_FAILURE_SHARE:
        return [Problem("sources_failing", f"{failing}/{total} 个来源最近一次抓取失败")]
    return []


def _ai(db, now: datetime) -> list[Problem]:
    if not config.llm_enabled():
        return [Problem("ai_off", "未配置模型，新闻不会被评分和翻译")]
    oldest, newest = (now - age for age in AI_WINDOW)
    total, unscored = db.execute(
        """SELECT COUNT(*), COALESCE(SUM(score IS NULL),0) FROM items
           WHERE fetched_at>=? AND fetched_at<?""",
        (oldest.isoformat(), newest.isoformat()),
    ).fetchone()
    if total >= MIN_AI_ITEMS and unscored > total * AI_UNSCORED_SHARE:
        return [Problem("ai_stalled", f"1–6 小时前抓到的 {total} 条中 {unscored} 条仍未评分"
                        + _ai_failure_hint(db, now))]
    return []


FAILURE_LABELS = {"quota": "余额不足", "auth": "密钥无效", "rate_limited": "请求过快被限流", "timeout": "超时",
                  "connection": "连不上", "server": "服务商出错", "content_filter": "内容审查拒绝", "other": "其他"}


def _ai_failure_hint(db, now: datetime) -> str:
    """Today's failed model requests by cause, so the alert says why (an empty balance, a revoked key)."""
    try:
        rows = db.execute(
            # Garbled text (dropped fields) is not a failed request.
            """SELECT kind, SUM(calls) AS n FROM ai_failures_daily WHERE day=? AND kind!='garbled'
               GROUP BY kind ORDER BY n DESC""",
            (now.astimezone(config.APP_TZ).strftime("%Y-%m-%d"),)).fetchall()
    except sqlite3.Error:
        return ""
    if not rows:
        return ""
    return "；今天模型请求失败：" + "、".join(f"{FAILURE_LABELS.get(row['kind'], row['kind'])} {row['n']} 次"
                                          for row in rows)


def _failed_jobs(db, now: datetime) -> list[Problem]:
    rows = db.execute(
        """SELECT kind, COUNT(*) AS n FROM jobs
           WHERE state IN ('dead_letter','blocked') AND updated_at>=?
           GROUP BY kind ORDER BY kind""",
        (format_utc(now - FAILED_JOB_WINDOW),),
    ).fetchall()
    if rows:
        kinds = "、".join(f"{row['kind']} ×{row['n']}" for row in rows)
        return [Problem("jobs_failed", f"24 小时内重试用尽的后台任务：{kinds}")]
    return []


def _stuck_jobs(db, now: datetime) -> list[Problem]:
    rows = db.execute(
        """SELECT job.kind, MIN(attempt.started_at) AS since FROM jobs job
           JOIN job_attempts attempt ON attempt.job_id=job.id AND attempt.lease_token=job.lease_token
           WHERE job.state='running' AND attempt.started_at<? AND job.lease_expires_at>?
           GROUP BY job.kind ORDER BY job.kind""",
        # Only a lease still being renewed means a live process holds the job; after the host
        # slept or the worker died, a job stays 'running' with a dead lease until the next claim
        # expires it, and is not stuck.
        (format_utc(now - JOB_STUCK_AFTER), format_utc(now)),
    ).fetchall()
    if rows:
        kinds = "、".join(
            f"{row['kind']}（{int((now - parse_utc(row['since'])).total_seconds() // 3600)} 小时）" for row in rows)
        return [Problem("job_stuck", f"后台任务运行过久未结束：{kinds}")]
    return []


def _backup(db, now: datetime) -> list[Problem]:
    from .scheduled_backup import nightly_bundles
    bundles = nightly_bundles()
    if bundles:
        stamp = bundles[-1].name.rsplit(".", 3)[1]
        made = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        if now - made > BACKUP_MAX_AGE:
            hours = int((now - made).total_seconds() // 3600)
            return [Problem("backup_stale", f"最新夜间备份是 {hours} 小时前")]
        return []
    row = db.execute("SELECT created_at FROM schedules WHERE id='maintenance:backup'").fetchone()
    if row and now - parse_utc(row["created_at"]) > BACKUP_MAX_AGE:
        return [Problem("backup_stale", "夜间备份从未成功")]
    return []


def _disk() -> list[Problem]:
    free, where = lowest_free((config.DB_PATH.parent, config.BLOB_PATH, config.BACKUP_PATH))
    if free < config.ALERT_DISK_FREE_MB * MB:
        return [Problem("disk_low", f"{where}只剩 {free // MB} MB（告警线 {config.ALERT_DISK_FREE_MB} MB）")]
    return []


def _offsite(db, now: datetime) -> list[Problem]:
    """Once the Mac pulls copies (D25), warn when its newest verified copy is a week old."""
    if not config.API_BACKUPS_ENABLED:
        return []
    from .offsite_backup import OFFSITE_MAX_AGE, read_receipt
    receipt = read_receipt()
    if receipt is not None:
        age = now - parse_utc(receipt["received_at"])
        if age > OFFSITE_MAX_AGE:
            return [Problem("offsite_stale", f"Mac 上的异地备份已经 {age.days} 天没有更新")]
        return []
    row = db.execute("SELECT created_at FROM schedules WHERE id='maintenance:backup'").fetchone()
    if row and now - parse_utc(row["created_at"]) > OFFSITE_MAX_AGE:
        return [Problem("offsite_stale", "Mac 还没有取走过任何异地备份")]
    return []


def find_problems(now: datetime | None = None) -> list[Problem]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with get_db() as db:
        problems = (_sources(db) + _ai(db, current) + _failed_jobs(db, current)
                    + _stuck_jobs(db, current) + _backup(db, current) + _offsite(db, current))
    return problems + _disk()


def disk_status() -> dict | None:
    """Free space on the fullest disk InfoHub writes to, for the health page."""
    try:
        free, where = lowest_free((config.DB_PATH.parent, config.BLOB_PATH, config.BACKUP_PATH))
    except OSError:
        return None
    return {"free_mb": free // MB, "where": where, "alert_mb": config.ALERT_DISK_FREE_MB}


def record(problems: list[Problem], checked_at: datetime | None = None) -> dict:
    """Atomically publish the latest result for the health page."""
    payload = {"checked_at": format_utc(checked_at or datetime.now(timezone.utc)),
               "problems": [problem.to_dict() for problem in problems], "disk": disk_status()}
    target = Path(config.RUNTIME_PATH) / RESULT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def recorded() -> dict | None:
    try:
        value = json.loads((Path(config.RUNTIME_PATH) / RESULT_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None
