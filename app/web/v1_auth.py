from __future__ import annotations

"""Fail-closed authentication boundary for the reserved /api/v1 routes."""

import logging
import re
import sqlite3
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import config
from ..api_auth import authenticate_api_key
from ..api_rate_limit import release_api_request, reserve_api_request
from ..database import get_db


# This is the target contract's read-only surface, not a list of enabled routes.
# A route absent from the application still returns 404 after authentication.
_POLICIES = (
    ("GET", re.compile(r"/api/v1/items/[^/]+/evidence/?"), "read:evidence"),
    ("GET", re.compile(r"/api/v1/items(?:/[^/]+(?:/versions)?)?/?"), "read:items"),
    ("GET", re.compile(r"/api/v1/evidence/[^/]+/?"), "read:evidence"),
    ("GET", re.compile(r"/api/v1/events/[^/]+/evidence/?"), "read:evidence"),
    ("GET", re.compile(r"/api/v1/events(?:/[^/]+)?/?"), "read:events"),
    ("GET", re.compile(r"/api/v1/analyses/[^/]+/?"), "read:analyses"),
    ("GET", re.compile(r"/api/v1/(?:entities|topics|sources)(?:/[^/]+)?/?"), "read:catalog"),
    ("GET", re.compile(r"/api/v1/signals/[^/]+/inputs/?"), "read:signals"),
    ("GET", re.compile(r"/api/v1/signals(?:/[^/]+)?/?"), "read:signals"),
    ("GET", re.compile(r"/api/v1/reports(?:/[^/]+)?/?"), "read:reports"),
    ("POST", re.compile(r"/api/v1/sync/snapshots/?"), "read:sync"),
    ("GET", re.compile(r"/api/v1/sync/snapshots/[^/]+(?:/pages)?/?"), "read:sync"),
    ("GET", re.compile(r"/api/v1/changes/?"), "read:sync"),
)
_BEARER = re.compile(r"Bearer ([^\s]+)", re.IGNORECASE)
_LOG = logging.getLogger(__name__)


def required_v1_scope(method: str, path: str) -> str | None:
    for allowed_method, pattern, scope in _POLICIES:
        if method == allowed_method and pattern.fullmatch(path):
            return scope
    return None


def _failure(status: int, code: str, request_id: str,
             *, retry_after: int | None = None) -> JSONResponse:
    messages = {
        "invalid_token": "A valid Bearer token is required.",
        "insufficient_scope": "This key does not grant the requested scope.",
        "resource_not_found": "Resource not found.",
        "temporarily_unavailable": "Authentication is temporarily unavailable.",
        "rate_limited": "Request limit reached; retry later.",
    }
    headers = {"X-Request-ID": request_id}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": messages[code],
                           "request_id": request_id, "retryable": status in (429, 503),
                           "details": {}}},
        headers=headers,
    )


async def v1_auth_guard(request: Request, call_next):
    """Protect v1 even when a future route forgets its own auth dependency."""
    path = request.url.path
    if path != "/api/v1" and not path.startswith("/api/v1/"):
        return await call_next(request)
    request_id = str(uuid4())
    request.state.request_id = request_id
    scope = required_v1_scope(request.method, path)
    if scope is None:
        return _failure(404, "resource_not_found", request_id)
    headers = [value for key, value in request.scope["headers"] if key.lower() == b"authorization"]
    if len(headers) != 1 or len(headers[0]) > 256:
        return _failure(401, "invalid_token", request_id)
    try:
        authorization = headers[0].decode("ascii")
    except UnicodeDecodeError:
        return _failure(401, "invalid_token", request_id)
    match = _BEARER.fullmatch(authorization)
    if match is None:
        return _failure(401, "invalid_token", request_id)
    try:
        with get_db() as db:
            principal = authenticate_api_key(db, match.group(1))
    except (sqlite3.Error, OSError):
        return _failure(503, "temporarily_unavailable", request_id)
    if principal is None:
        return _failure(401, "invalid_token", request_id)
    if not principal.allows(scope):
        return _failure(403, "insufficient_scope", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh_principal = authenticate_api_key(db, match.group(1))
            if fresh_principal is None:
                return _failure(401, "invalid_token", request_id)
            if not fresh_principal.allows(scope):
                return _failure(403, "insufficient_scope", request_id)
            admission = reserve_api_request(
                db, fresh_principal,
                rate_per_minute=config.API_KEY_RATE_PER_MINUTE,
                consumer_concurrency=config.API_CONSUMER_CONCURRENCY,
                lease_seconds=config.API_REQUEST_LEASE_SECONDS,
                transaction_open=True,
            )
    except (sqlite3.Error, OSError):
        return _failure(503, "temporarily_unavailable", request_id)
    if not admission.allowed:
        return _failure(429, "rate_limited", request_id,
                        retry_after=admission.retry_after)
    request.state.api_principal = fresh_principal
    try:
        response = await call_next(request)
    finally:
        try:
            with get_db() as db:
                release_api_request(db, admission.lease_id)
        except (sqlite3.Error, OSError):
            # The short database lease expires even if release cannot run.
            _LOG.warning("API request lease release failed for request %s", request_id)
    response.headers["X-Request-ID"] = request_id
    return response
