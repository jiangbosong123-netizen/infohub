from __future__ import annotations

"""Short-lived, key-bound cursors for live v1 list queries."""

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Mapping

from .api_auth import ApiPrincipal


CURSOR_TTL_SECONDS = 900
_FIELDS = frozenset({
    "v", "resource", "filters", "last_id", "consumer_id", "key_id",
    "authz_version", "dataset_epoch", "expires_at",
})


class CursorError(ValueError):
    code = "invalid_cursor"


class CursorExpired(CursorError):
    code = "cursor_expired"


class CursorFilterMismatch(CursorError):
    code = "filter_mismatch"


class CursorEpochChanged(CursorError):
    code = "epoch_changed"


@dataclass(frozen=True)
class CursorPosition:
    last_id: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def filter_hash(filters: Mapping[str, object]) -> str:
    encoded = json.dumps(
        filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _signing_key(db: sqlite3.Connection, principal: ApiPrincipal) -> bytes:
    row = db.execute(
        "SELECT token_sha256 FROM api_keys WHERE key_id=? AND consumer_id=?",
        (principal.key_id, principal.consumer_id),
    ).fetchone()
    if row is None:
        raise CursorError("cursor key is unavailable")
    try:
        return bytes.fromhex(row["token_sha256"])
    except (ValueError, TypeError) as exc:
        raise CursorError("cursor key is invalid") from exc


def encode_cursor(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    resource: str,
    filters: Mapping[str, object],
    last_id: str,
    dataset_epoch: str,
    now: float | None = None,
) -> str:
    payload = {
        "v": 1,
        "resource": resource,
        "filters": filter_hash(filters),
        "last_id": last_id,
        "consumer_id": principal.consumer_id,
        "key_id": principal.key_id,
        "authz_version": principal.authz_version,
        "dataset_epoch": dataset_epoch,
        "expires_at": int(now if now is not None else time.time()) + CURSOR_TTL_SECONDS,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_signing_key(db, principal), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(signature)}"


def decode_cursor(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    token: str,
    *,
    resource: str,
    filters: Mapping[str, object],
    dataset_epoch: str,
    now: float | None = None,
) -> CursorPosition:
    if not isinstance(token, str) or not token or len(token) > 2048 or token.count(".") != 1:
        raise CursorError("cursor encoding is invalid")
    try:
        encoded, encoded_signature = token.split(".")
        raw = _b64decode(encoded)
        signature = _b64decode(encoded_signature)
        payload = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor encoding is invalid") from exc
    expected = hmac.new(_signing_key(db, principal), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise CursorError("cursor signature is invalid")
    if not isinstance(payload, dict) or frozenset(payload) != _FIELDS or payload.get("v") != 1:
        raise CursorError("cursor payload is invalid")
    if payload.get("consumer_id") != principal.consumer_id \
            or payload.get("key_id") != principal.key_id \
            or payload.get("authz_version") != principal.authz_version:
        raise CursorError("cursor authorization is invalid")
    if payload.get("resource") != resource or payload.get("filters") != filter_hash(filters):
        raise CursorFilterMismatch("cursor does not match this query")
    if payload.get("dataset_epoch") != dataset_epoch:
        raise CursorEpochChanged("cursor belongs to another dataset epoch")
    expires_at = payload.get("expires_at")
    current = now if now is not None else time.time()
    if not isinstance(expires_at, int) or expires_at < current:
        raise CursorExpired("cursor has expired")
    last_id = payload.get("last_id")
    if not isinstance(last_id, str) or not last_id or len(last_id) > 128:
        raise CursorError("cursor position is invalid")
    return CursorPosition(last_id)
