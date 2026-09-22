from __future__ import annotations

"""API consumer keys: issue once, store only hashes, verify scopes and expiry."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

SCOPES = frozenset({
    "read:catalog", "read:items", "read:events", "read:analyses",
    "read:evidence", "read:signals", "read:reports", "read:sync", "read:ops",
})
_TOKEN = re.compile(r"^ih1\.([0-9a-f]{16})\.([A-Za-z0-9_-]{43})$")


class ApiAuthError(ValueError):
    """Invalid key-management input; never include the supplied token."""


@dataclass(frozen=True)
class IssuedApiKey:
    key_id: str
    consumer_id: str
    scopes: tuple[str, ...]
    expires_at: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ApiPrincipal:
    key_id: str
    consumer_id: str
    scopes: frozenset[str]
    authz_version: int

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ApiAuthError("API key time must include a timezone")
    return result.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clean_actor(actor: str) -> str:
    clean_actor = actor.strip() if isinstance(actor, str) else ""
    if not clean_actor or len(clean_actor) > 120:
        raise ApiAuthError("actor must contain 1 to 120 characters")
    return clean_actor


def _audit(db: sqlite3.Connection, action: str, actor: str, consumer_id: str,
           key_id: str | None, details: dict, at: datetime) -> None:
    db.execute(
        """INSERT INTO api_key_audit(action,actor,consumer_id,key_id,details_json,occurred_at)
           VALUES(?,?,?,?,?,?)""",
        (action, actor, consumer_id, key_id,
         json.dumps(details, sort_keys=True), _stamp(at)),
    )


def create_consumer(db: sqlite3.Connection, name: str, *, actor: str,
                    now: datetime | None = None) -> str:
    actor = _clean_actor(actor)
    clean_name = name.strip() if isinstance(name, str) else ""
    if not clean_name or len(clean_name) > 120:
        raise ApiAuthError("consumer name must contain 1 to 120 characters")
    current = _utc(now)
    consumer_id = str(uuid4())
    db.execute(
        """INSERT INTO api_consumers(id,name,status,authz_version,created_at)
           VALUES(?,?,'active',1,?)""",
        (consumer_id, clean_name, _stamp(current)),
    )
    _audit(db, "consumer_created", actor, consumer_id, None,
           {"name": clean_name}, current)
    return consumer_id


def issue_api_key(db: sqlite3.Connection, consumer_id: str, scopes: set[str] | frozenset[str],
                  *, expires_at: datetime, actor: str,
                  now: datetime | None = None) -> IssuedApiKey:
    actor = _clean_actor(actor)
    current = _utc(now)
    expiry = _utc(expires_at)
    selected = frozenset(scopes)
    if not selected or not selected <= SCOPES:
        raise ApiAuthError("API key requires one or more supported scopes")
    if expiry <= current:
        raise ApiAuthError("API key expiry must be in the future")
    row = db.execute("SELECT status FROM api_consumers WHERE id=?", (consumer_id,)).fetchone()
    if not row or row["status"] != "active":
        raise ApiAuthError("API consumer is not active")
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = f"ih1.{key_id}.{secret}"
    db.execute(
        """INSERT INTO api_keys(key_id,consumer_id,token_sha256,scopes_json,issued_at,expires_at)
           VALUES(?,?,?,?,?,?)""",
        (key_id, consumer_id, hashlib.sha256(secret.encode("ascii")).hexdigest(),
         json.dumps(sorted(selected)), _stamp(current), _stamp(expiry)),
    )
    _audit(db, "key_issued", actor, consumer_id, key_id,
           {"scopes": sorted(selected), "expires_at": _stamp(expiry)}, current)
    return IssuedApiKey(key_id, consumer_id, tuple(sorted(selected)), _stamp(expiry), token)


def authenticate_api_key(db: sqlite3.Connection, token: str,
                         *, required_scope: str | None = None,
                         now: datetime | None = None) -> ApiPrincipal | None:
    """Return None for invalid, expired, revoked or under-scoped keys."""
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if not match or (required_scope is not None and required_scope not in SCOPES):
        return None
    key_id, secret = match.groups()
    row = db.execute(
        """SELECT k.consumer_id,k.token_sha256,k.scopes_json,k.expires_at,k.revoked_at,
                  c.status,c.authz_version
           FROM api_keys k JOIN api_consumers c ON c.id=k.consumer_id
           WHERE k.key_id=?""",
        (key_id,),
    ).fetchone()
    if row is None:
        return None
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    if not hmac.compare_digest(digest, row["token_sha256"]):
        return None
    if row["status"] != "active" or row["revoked_at"] is not None:
        return None
    try:
        expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        stored_scopes = json.loads(row["scopes_json"])
    except (ValueError, TypeError):
        return None
    if (not isinstance(stored_scopes, list)
            or any(not isinstance(scope, str) for scope in stored_scopes)
            or len(set(stored_scopes)) != len(stored_scopes)):
        return None
    scopes = frozenset(stored_scopes)
    if expiry.tzinfo is None or expiry <= _utc(now) or not scopes or not scopes <= SCOPES:
        return None
    if required_scope is not None and required_scope not in scopes:
        return None
    return ApiPrincipal(key_id, row["consumer_id"], scopes, row["authz_version"])


def revoke_api_key(db: sqlite3.Connection, key_id: str, *, actor: str,
                   now: datetime | None = None) -> bool:
    """Idempotent revocation; bump authz_version to invalidate future cursors."""
    actor = _clean_actor(actor)
    current_time = _utc(now)
    current = _stamp(current_time)
    row = db.execute("SELECT consumer_id FROM api_keys WHERE key_id=? AND revoked_at IS NULL",
                     (key_id,)).fetchone()
    if row is None:
        return False
    changed = db.execute("UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
                         (current, key_id)).rowcount
    if not changed:
        return False
    db.execute("UPDATE api_consumers SET authz_version=authz_version+1 WHERE id=?",
               (row["consumer_id"],))
    _audit(db, "key_revoked", actor, row["consumer_id"], key_id, {}, current_time)
    return True


def revoke_consumer(db: sqlite3.Connection, consumer_id: str, *, actor: str,
                    now: datetime | None = None) -> bool:
    actor = _clean_actor(actor)
    current_time = _utc(now)
    current = _stamp(current_time)
    changed = db.execute(
        """UPDATE api_consumers SET status='revoked',revoked_at=?,
                  authz_version=authz_version+1 WHERE id=? AND status='active'""",
        (current, consumer_id),
    ).rowcount
    if changed:
        _audit(db, "consumer_revoked", actor, consumer_id, None, {}, current_time)
    return bool(changed)
