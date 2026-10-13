from __future__ import annotations

"""Headers and host checks for the localhost-only Tailscale Serve backend."""

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse

from .. import config


def public_host() -> str | None:
    return urlsplit(config.PUBLIC_ORIGIN).hostname if config.PUBLIC_ORIGIN else None


def uses_public_origin(request: Request) -> bool:
    expected = public_host()
    return expected is not None and request.url.hostname == expected


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Every response: no framing, no MIME sniffing, no referrer sent off-site. Scripts are not
# restricted because the portal templates use inline ones.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
}


def allowed_host(request: Request) -> bool:
    """Once a public origin is configured, only it and the machine itself may address the portal,
    so a page on another site cannot read it through DNS rebinding."""
    expected = public_host()
    return expected is None or request.url.hostname in {expected, *LOCAL_HOSTS}


async def private_https_headers(request: Request, call_next):
    if not allowed_host(request):
        return PlainTextResponse("unknown host", status_code=400)
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if uses_public_origin(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response
