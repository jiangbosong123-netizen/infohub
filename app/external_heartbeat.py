from __future__ import annotations

"""Dead-man switch: tell an outside monitor that a crawl cycle finished.

The health page cannot report a host that is off, asleep or offline; the legacy collector sat
idle for about 40 of 356 hours that way. When INFOHUB_EXTERNAL_HEARTBEAT_URL is set, the worker
pings it after each finished crawl cycle, and the monitor alerts when pings stop. A failed ping
is logged and never fails the crawl.
"""
import logging
from urllib.parse import urlsplit

import httpx

from . import config

log = logging.getLogger(__name__)
PING_TIMEOUT_SECONDS = 10.0


def ping(url: str | None = None) -> bool:
    target = config.EXTERNAL_HEARTBEAT_URL if url is None else url
    if not target:
        return False
    try:
        httpx.get(target, timeout=PING_TIMEOUT_SECONDS, follow_redirects=True).raise_for_status()
    except Exception as exc:  # noqa: BLE001 - monitoring must never break the crawl
        # Only the host is logged: the URL itself is a credential.
        log.warning("external heartbeat to %s failed: %s", urlsplit(target).hostname,
                    type(exc).__name__)
        return False
    return True
