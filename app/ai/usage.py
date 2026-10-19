from __future__ import annotations

"""Model usage per calendar day and task (D31 tier evaluation: "what does the AI cost?").

Every model request adds one row's worth: calls, tokens as the provider reports them, and how many
items it carried. A failed request counts as a failed call with no tokens. Recording must never
break the work it measures, so a database error is logged and dropped.
"""

import logging
import sqlite3
from datetime import datetime

from .. import config
from ..database import get_db

log = logging.getLogger(__name__)


def failure_kind(exc: BaseException) -> str:
    """Zhipu answers with its own codes inside the error text: 1301 content filter, 1302/1303/1305
    too many requests, 1113 no balance left."""
    text, name = str(exc), type(exc).__name__
    status = getattr(exc, "status_code", None)
    if "1301" in text or "contentFilter" in text:
        return "content_filter"
    if "1113" in text or "insufficient_quota" in text:
        return "quota"
    if status == 429 or "RateLimit" in name or any(code in text for code in ("1302", "1303", "1305")):
        return "rate_limited"
    if "Timeout" in name:
        return "timeout"
    if status in (401, 403) or "Authentication" in name or "PermissionDenied" in name:
        return "auth"
    if (isinstance(status, int) and status >= 500) or "InternalServer" in name:
        return "server"
    if "Connection" in name:
        return "connection"
    return "other"


def record(task: str, response=None, *, items: int = 0, failed: bool = False,
           error: BaseException | None = None) -> None:
    usage = getattr(response, "usage", None)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    day = datetime.now(config.APP_TZ).strftime("%Y-%m-%d")
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO ai_usage_daily(day,task,model,calls,failed_calls,prompt_tokens,completion_tokens,items)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(day,task,model) DO UPDATE SET calls=calls+excluded.calls,
                       failed_calls=failed_calls+excluded.failed_calls,
                       prompt_tokens=prompt_tokens+excluded.prompt_tokens,
                       completion_tokens=completion_tokens+excluded.completion_tokens,
                       items=items+excluded.items""",
                (day, task, config.LLM_MODEL or "", 0 if failed else 1, 1 if failed else 0,
                 prompt, completion, 0 if failed else max(0, int(items))))
            if failed:
                db.execute(
                    """INSERT INTO ai_failures_daily(day,task,kind,calls) VALUES(?,?,?,1)
                       ON CONFLICT(day,task,kind) DO UPDATE SET calls=calls+1""",
                    (day, task, failure_kind(error) if error is not None else "other"))
    except sqlite3.Error as exc:
        log.warning("could not record model usage: %s", type(exc).__name__)


def record_garbled(db, task: str, fields: int) -> None:
    """Count model text dropped as garbled (app/ai/text_quality.py) on the caller's connection,
    so /api/coverage shows how often it happens; kind "garbled" in ai_failures_daily."""
    if fields <= 0:
        return
    db.execute("""INSERT INTO ai_failures_daily(day,task,kind,calls) VALUES(?,?,'garbled',?)
                  ON CONFLICT(day,task,kind) DO UPDATE SET calls=calls+excluded.calls""",
               (datetime.now(config.APP_TZ).strftime("%Y-%m-%d"), task, int(fields)))


def call(task: str, create, *, items: int = 0):
    """Run ``create()`` (one chat completion) and record it, failed or not."""
    try:
        response = create()
    except Exception as exc:
        record(task, failed=True, error=exc)
        raise
    record(task, response, items=items)
    return response
