from __future__ import annotations

"""Guarded, one-shot scheduled publication for a calendar day."""

from datetime import datetime, timedelta

from . import config
from .database import get_db
from .report_inputs import calendar_window, freeze_calendar_daily
from .report_versions import publish_structured_report


def _existing_status(date_str: str) -> str | None:
    report_key = f"calendar_daily:{date_str}:{config.APP_TZ}"
    with get_db() as db:
        if db.execute("SELECT 1 FROM daily_reports WHERE date=?", (date_str,)).fetchone():
            return "legacy_preserved"
        if db.execute(
            """SELECT 1 FROM report_publications p JOIN dataset_state d
                 ON d.dataset_id=p.dataset_id AND d.singleton=1
                 WHERE p.report_key=?""", (report_key,)
        ).fetchone():
            return "already_published"
    return None


def generate_legacy_scheduled_report(date_str: str | None = None) -> dict:
    """Keep rollback mode from overwriting either report representation."""
    date_str = date_str or (datetime.now(config.APP_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    calendar_window(date_str)
    status = _existing_status(date_str)
    if status:
        return {"date": date_str, "status": status}
    from .ai.daily import generate_daily
    written = generate_daily(date_str, overwrite=False)
    return {"date": date_str, "status": "legacy_written" if written else (_existing_status(date_str) or "no_input")}


def generate_scheduled_report(date_str: str | None = None) -> dict:
    date_str = date_str or (datetime.now(config.APP_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    calendar_window(date_str)
    status = _existing_status(date_str)
    if status:
        return {"date": date_str, "status": status}
    snapshot = freeze_calendar_daily(date_str)
    if snapshot is None:
        return {"date": date_str, "status": "no_input"}
    result = publish_structured_report(snapshot["snapshot_id"], protect_existing=True)
    return {"date": date_str, **result}
