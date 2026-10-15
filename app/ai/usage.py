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


def record(task: str, response=None, *, items: int = 0, failed: bool = False) -> None:
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
    except sqlite3.Error as exc:
        log.warning("could not record model usage: %s", type(exc).__name__)


def call(task: str, create, *, items: int = 0):
    """Run ``create()`` (one chat completion) and record it, failed or not."""
    try:
        response = create()
    except Exception:
        record(task, failed=True)
        raise
    record(task, response, items=items)
    return response
