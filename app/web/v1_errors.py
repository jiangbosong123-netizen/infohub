from __future__ import annotations

"""Stable, non-sensitive v1 error envelopes."""

from fastapi.responses import JSONResponse


_MESSAGES = {
    "invalid_token": "A valid Bearer token is required.",
    "insufficient_scope": "This key does not grant the requested scope.",
    "resource_not_found": "Resource not found.",
    "temporarily_unavailable": "The requested service is temporarily unavailable.",
    "rate_limited": "Request limit reached; retry later.",
    "secure_transport_required": "Use the configured private HTTPS origin.",
    "invalid_cursor": "The cursor is invalid.",
    "filter_mismatch": "The cursor does not match this query.",
    "cursor_expired": "The cursor has expired.",
    "epoch_changed": "The dataset epoch changed; restart this listing.",
    "invalid_parameter": "One or more query parameters are invalid.",
    "not_ready": "This API resource is not ready.",
    "restricted_content": "The requested content is restricted.",
    "unsupported_history": "This historical read mode is not supported yet.",
}


def v1_error(status: int, code: str, request_id: str,
             *, retry_after: int | None = None) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status,
        content={"error": {
            "code": code,
            "message": _MESSAGES[code],
            "request_id": request_id,
            "retryable": status in (429, 503),
            "details": {},
        }},
        headers=headers,
    )
