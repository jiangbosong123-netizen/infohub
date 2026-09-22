from __future__ import annotations

"""Allowlisted audit events for authenticated API requests."""

import sqlite3
from datetime import datetime, timezone

from .api_auth import ApiPrincipal, SCOPES

EVENTS = frozenset({"admitted", "completed", "denied", "handler_error"})
RESOURCES = frozenset({
    "items", "evidence", "events", "analyses", "catalog", "signals",
    "reports", "snapshots", "changes",
})


def record_request_event(
    db: sqlite3.Connection, *, request_id: str, event: str,
    principal: ApiPrincipal, method: str, resource: str, required_scope: str,
    status_code: int | None = None, error_code: str | None = None,
    duration_ms: int | None = None, now: datetime | None = None,
) -> None:
    """Record only enumerated metadata; paths, queries and headers are not accepted."""
    if event not in EVENTS or resource not in RESOURCES or required_scope not in SCOPES:
        raise ValueError("unsupported API request audit value")
    if method not in {"GET", "POST"}:
        raise ValueError("unsupported API request method")
    if status_code is not None and not 100 <= status_code <= 599:
        raise ValueError("invalid API response status")
    if duration_ms is not None and duration_ms < 0:
        raise ValueError("request duration cannot be negative")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("request audit time must include a timezone")
    occurred_at = timestamp.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    db.execute(
        """INSERT INTO api_request_audit(
               request_id,event,consumer_id,key_id,method,resource,required_scope,
               status_code,error_code,duration_ms,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (request_id, event, principal.consumer_id, principal.key_id, method,
         resource, required_scope, status_code, error_code, duration_ms, occurred_at),
    )
