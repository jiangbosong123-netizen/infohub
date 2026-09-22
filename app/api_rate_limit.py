from __future__ import annotations

"""SQLite-backed request admission shared by all API web processes."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .api_auth import ApiPrincipal


@dataclass(frozen=True)
class RequestAdmission:
    allowed: bool
    lease_id: str | None
    retry_after: int
    reason: str | None


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("request admission time must include a timezone")
    return result.astimezone(timezone.utc)


def reserve_api_request(
    db: sqlite3.Connection, principal: ApiPrincipal, *, rate_per_minute: int,
    consumer_concurrency: int, lease_seconds: int, now: datetime | None = None,
    transaction_open: bool = False,
) -> RequestAdmission:
    """Atomically admit one request; the caller commits before executing it."""
    if min(rate_per_minute, consumer_concurrency, lease_seconds) < 1:
        raise ValueError("API request limits must be positive")
    second = int(_utc(now).timestamp())
    window = second - second % 60
    if transaction_open and not db.in_transaction:
        raise ValueError("request admission requires an open write transaction")
    if not transaction_open:
        db.execute("BEGIN IMMEDIATE")
    # Bounded transient state; abandoned leases expire after a crashed process.
    db.execute("DELETE FROM api_request_leases WHERE expires_at<=?", (second,))
    db.execute("DELETE FROM api_rate_buckets WHERE window_start<?", (window - 3600,))
    used = db.execute(
        "SELECT used_count FROM api_rate_buckets WHERE key_id=? AND window_start=?",
        (principal.key_id, window),
    ).fetchone()
    if used is not None and used["used_count"] >= rate_per_minute:
        return RequestAdmission(False, None, max(1, window + 60 - second), "rate")
    active = db.execute(
        "SELECT COUNT(*) FROM api_request_leases WHERE consumer_id=? AND expires_at>?",
        (principal.consumer_id, second),
    ).fetchone()[0]
    if active >= consumer_concurrency:
        return RequestAdmission(False, None, 1, "concurrency")
    lease_id = str(uuid4())
    db.execute(
        """INSERT INTO api_rate_buckets(key_id,window_start,used_count)
           VALUES(?,?,1)
           ON CONFLICT(key_id,window_start) DO UPDATE SET used_count=used_count+1""",
        (principal.key_id, window),
    )
    db.execute(
        """INSERT INTO api_request_leases(
               lease_id,consumer_id,key_id,acquired_at,expires_at)
           VALUES(?,?,?,?,?)""",
        (lease_id, principal.consumer_id, principal.key_id, second, second + lease_seconds),
    )
    return RequestAdmission(True, lease_id, 0, None)


def release_api_request(db: sqlite3.Connection, lease_id: str) -> None:
    db.execute("DELETE FROM api_request_leases WHERE lease_id=?", (lease_id,))
