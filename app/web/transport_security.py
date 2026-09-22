from __future__ import annotations

"""Headers and host checks for the localhost-only Tailscale Serve backend."""

from urllib.parse import urlsplit

from fastapi import Request

from .. import config


def public_host() -> str | None:
    return urlsplit(config.PUBLIC_ORIGIN).hostname if config.PUBLIC_ORIGIN else None


def uses_public_origin(request: Request) -> bool:
    expected = public_host()
    return expected is not None and request.url.hostname == expected


async def private_https_headers(request: Request, call_next):
    response = await call_next(request)
    if uses_public_origin(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
    return response
