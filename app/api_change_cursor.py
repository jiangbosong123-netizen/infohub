from __future__ import annotations

"""Long-lived signed cursors for snapshot-to-change-feed handoff."""

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

from .api_auth import ApiPrincipal
from .api_cursor import CursorEpochChanged, CursorError, CursorExpired, _signing_key

CHANGE_CURSOR_TTL_SECONDS = 90 * 24 * 60 * 60
_FIELDS = frozenset({
    "v", "consumer_id", "key_id", "authz_version", "dataset_id",
    "dataset_epoch", "resources", "seq", "issued_at", "expires_at",
})


@dataclass(frozen=True)
class ChangeCursor:
    dataset_id: str
    dataset_epoch: str
    resources: tuple[str, ...]
    seq: int
    issued_at: int
    expires_at: int


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode_change_cursor(
    db: sqlite3.Connection, principal: ApiPrincipal, *, dataset_id: str,
    dataset_epoch: str, resources: Iterable[str], seq: int,
    now: float | None = None, expires_at: int | None = None,
) -> str:
    current = int(now if now is not None else time.time())
    expiry = expires_at if expires_at is not None else current + CHANGE_CURSOR_TTL_SECONDS
    ordered = tuple(resources)
    if (not dataset_id or not dataset_epoch or not ordered or len(set(ordered)) != len(ordered)
            or seq < 0 or expiry <= current):
        raise CursorError("change cursor input is invalid")
    payload = {
        "v": 1, "consumer_id": principal.consumer_id, "key_id": principal.key_id,
        "authz_version": principal.authz_version, "dataset_id": dataset_id,
        "dataset_epoch": dataset_epoch, "resources": list(ordered), "seq": seq,
        "issued_at": current, "expires_at": expiry,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_signing_key(db, principal), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(signature)}"


def decode_change_cursor(
    db: sqlite3.Connection, principal: ApiPrincipal, token: str, *,
    allowed_resources: frozenset[str], now: float | None = None,
) -> ChangeCursor:
    if not isinstance(token, str) or not token or len(token) > 4096 or token.count(".") != 1:
        raise CursorError("change cursor encoding is invalid")
    try:
        encoded, encoded_signature = token.split(".")
        raw = _b64decode(encoded)
        signature = _b64decode(encoded_signature)
        if _b64encode(raw) != encoded or _b64encode(signature) != encoded_signature:
            raise ValueError("non-canonical base64url")
        payload = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise CursorError("change cursor encoding is invalid") from exc
    expected = hmac.new(_signing_key(db, principal), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise CursorError("change cursor signature is invalid")
    if not isinstance(payload, dict) or frozenset(payload) != _FIELDS or payload.get("v") != 1:
        raise CursorError("change cursor payload is invalid")
    if (payload.get("consumer_id") != principal.consumer_id
            or payload.get("key_id") != principal.key_id
            or payload.get("authz_version") != principal.authz_version):
        raise CursorError("change cursor authorization is invalid")
    state = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise CursorError("dataset identity is unavailable")
    if (payload.get("dataset_id") != state["dataset_id"]
            or payload.get("dataset_epoch") != state["current_epoch"]):
        raise CursorEpochChanged("change cursor belongs to another dataset epoch")
    resources = payload.get("resources")
    if (not isinstance(resources, list) or not resources
            or any(not isinstance(item, str) or item not in allowed_resources for item in resources)
            or len(resources) != len(set(resources))):
        raise CursorError("change cursor resources are invalid")
    seq, issued_at, expires_at = (
        payload.get("seq"), payload.get("issued_at"), payload.get("expires_at")
    )
    current = now if now is not None else time.time()
    if (not isinstance(seq, int) or seq < 0 or not isinstance(issued_at, int)
            or not isinstance(expires_at, int) or expires_at <= issued_at
            or expires_at < current):
        raise CursorExpired("change cursor has expired")
    return ChangeCursor(
        dataset_id=state["dataset_id"], dataset_epoch=state["current_epoch"],
        resources=tuple(resources), seq=seq, issued_at=issued_at, expires_at=expires_at,
    )
