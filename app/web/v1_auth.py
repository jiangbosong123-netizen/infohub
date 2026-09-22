from __future__ import annotations

"""Fail-closed authentication boundary for the reserved /api/v1 routes."""

import logging
import re
import sqlite3
import time
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import config
from ..api_auth import authenticate_api_key
from ..api_rate_limit import release_api_request, reserve_api_request
from ..api_request_audit import record_request_event
from ..database import get_db
from .transport_security import uses_public_origin


# This is the target contract's read-only surface, not a list of enabled routes.
# A route absent from the application still returns 404 after authentication.
_POLICIES = (
    ("GET", re.compile(r"/api/v1/items/[^/]+/evidence/?"), "read:evidence", "evidence"),
    ("GET", re.compile(r"/api/v1/items(?:/[^/]+(?:/versions)?)?/?"), "read:items", "items"),
    ("GET", re.compile(r"/api/v1/evidence/[^/]+/?"), "read:evidence", "evidence"),
    ("GET", re.compile(r"/api/v1/events/[^/]+/evidence/?"), "read:evidence", "evidence"),
    ("GET", re.compile(r"/api/v1/events(?:/[^/]+)?/?"), "read:events", "events"),
    ("GET", re.compile(r"/api/v1/analyses/[^/]+/?"), "read:analyses", "analyses"),
    ("GET", re.compile(r"/api/v1/(?:entities|topics|sources)(?:/[^/]+)?/?"), "read:catalog", "catalog"),
    ("GET", re.compile(r"/api/v1/signals/[^/]+/inputs/?"), "read:signals", "signals"),
    ("GET", re.compile(r"/api/v1/signals(?:/[^/]+)?/?"), "read:signals", "signals"),
    ("GET", re.compile(r"/api/v1/reports(?:/[^/]+)?/?"), "read:reports", "reports"),
    ("POST", re.compile(r"/api/v1/sync/snapshots/?"), "read:sync", "snapshots"),
    ("GET", re.compile(r"/api/v1/sync/snapshots/[^/]+(?:/pages)?/?"), "read:sync", "snapshots"),
    ("GET", re.compile(r"/api/v1/changes/?"), "read:sync", "changes"),
)
_BEARER = re.compile(r"Bearer ([^\s]+)", re.IGNORECASE)
_LOG = logging.getLogger(__name__)


def required_v1_scope(method: str, path: str) -> str | None:
    policy = _request_policy(method, path)
    return policy[0] if policy else None


def _request_policy(method: str, path: str) -> tuple[str, str] | None:
    for allowed_method, pattern, scope, resource in _POLICIES:
        if method == allowed_method and pattern.fullmatch(path):
            return scope, resource
    return None


def _failure(status: int, code: str, request_id: str,
             *, retry_after: int | None = None) -> JSONResponse:
    messages = {
        "invalid_token": "A valid Bearer token is required.",
        "insufficient_scope": "This key does not grant the requested scope.",
        "resource_not_found": "Resource not found.",
        "temporarily_unavailable": "Authentication is temporarily unavailable.",
        "rate_limited": "Request limit reached; retry later.",
        "secure_transport_required": "Use the configured private HTTPS origin.",
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
    if config.PUBLIC_ORIGIN and not uses_public_origin(request):
        _LOG.warning("API v1 transport rejected request_id=%s reason=public_host_mismatch", request_id)
        return _failure(421, "secure_transport_required", request_id)
    policy = _request_policy(request.method, path)
    if policy is None:
        _LOG.warning("API v1 route rejected request_id=%s reason=unknown_route", request_id)
        return _failure(404, "resource_not_found", request_id)
    scope, resource = policy
    headers = [value for key, value in request.scope["headers"] if key.lower() == b"authorization"]
    if len(headers) != 1 or len(headers[0]) > 256:
        _LOG.warning("API v1 authentication rejected request_id=%s reason=invalid_header", request_id)
        return _failure(401, "invalid_token", request_id)
    try:
        authorization = headers[0].decode("ascii")
    except UnicodeDecodeError:
        _LOG.warning("API v1 authentication rejected request_id=%s reason=invalid_header", request_id)
        return _failure(401, "invalid_token", request_id)
    match = _BEARER.fullmatch(authorization)
    if match is None:
        _LOG.warning("API v1 authentication rejected request_id=%s reason=invalid_header", request_id)
        return _failure(401, "invalid_token", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            principal = authenticate_api_key(db, match.group(1))
            if principal is not None and not principal.allows(scope):
                record_request_event(
                    db, request_id=request_id, event="denied", principal=principal,
                    method=request.method, resource=resource, required_scope=scope,
                    status_code=403, error_code="insufficient_scope",
                )
            elif principal is not None:
                admission = reserve_api_request(
                    db, principal,
                    rate_per_minute=config.API_KEY_RATE_PER_MINUTE,
                    consumer_concurrency=config.API_CONSUMER_CONCURRENCY,
                    lease_seconds=config.API_REQUEST_LEASE_SECONDS,
                    transaction_open=True,
                )
                record_request_event(
                    db, request_id=request_id,
                    event="admitted" if admission.allowed else "denied",
                    principal=principal, method=request.method, resource=resource,
                    required_scope=scope,
                    status_code=None if admission.allowed else 429,
                    error_code=None if admission.allowed else "rate_limited",
                )
    except (sqlite3.Error, OSError):
        return _failure(503, "temporarily_unavailable", request_id)
    if principal is None:
        _LOG.warning("API v1 authentication rejected request_id=%s reason=invalid_token", request_id)
        return _failure(401, "invalid_token", request_id)
    if not principal.allows(scope):
        return _failure(403, "insufficient_scope", request_id)
    if not admission.allowed:
        return _failure(429, "rate_limited", request_id,
                        retry_after=admission.retry_after)
    request.state.api_principal = principal
    started = time.monotonic_ns()
    try:
        response = await call_next(request)
    except Exception:
        duration = max(0, (time.monotonic_ns() - started) // 1_000_000)
        try:
            with get_db() as db:
                release_api_request(db, admission.lease_id)
                record_request_event(
                    db, request_id=request_id, event="handler_error", principal=principal,
                    method=request.method, resource=resource, required_scope=scope,
                    status_code=500, error_code="handler_error", duration_ms=duration,
                )
        except (sqlite3.Error, OSError):
            _LOG.warning("API request failure audit unavailable request_id=%s", request_id)
        raise
    duration = max(0, (time.monotonic_ns() - started) // 1_000_000)
    try:
        with get_db() as db:
            release_api_request(db, admission.lease_id)
            record_request_event(
                db, request_id=request_id, event="completed", principal=principal,
                method=request.method, resource=resource, required_scope=scope,
                status_code=response.status_code, duration_ms=duration,
            )
    except (sqlite3.Error, OSError):
        _LOG.warning("API request completion audit unavailable request_id=%s", request_id)
        return _failure(503, "temporarily_unavailable", request_id)
    response.headers["X-Request-ID"] = request_id
    return response
