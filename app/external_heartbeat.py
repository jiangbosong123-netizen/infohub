from __future__ import annotations

"""Dead-man switch: tell an outside monitor that a crawl cycle finished, and whether it is healthy.

The health page cannot report a host that is off, asleep or offline; the legacy collector sat
idle for about 40 of 356 hours that way. When INFOHUB_EXTERNAL_HEARTBEAT_URL is set, the worker
pings it after each finished crawl cycle, and the monitor alerts when pings stop. When the
self-check finds a problem, the ping goes to ``<url>/fail`` (the Healthchecks.io convention) with
the reasons as the body, so the monitor alerts at once. A failed ping is logged and never fails
the crawl.
"""
import logging
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config

log = logging.getLogger(__name__)
PING_TIMEOUT_SECONDS = 10.0


def fail_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/fail"))


def ping(url: str | None = None, problems: Sequence = ()) -> bool:
    target = config.EXTERNAL_HEARTBEAT_URL if url is None else url
    if not target:
        return False
    try:
        if problems:
            body = "\n".join(f"{problem.code}: {problem.detail}" for problem in problems)
            httpx.post(fail_url(target), content=body.encode("utf-8"),
                       headers={"Content-Type": "text/plain; charset=utf-8"},
                       timeout=PING_TIMEOUT_SECONDS, follow_redirects=True).raise_for_status()
        else:
            httpx.get(target, timeout=PING_TIMEOUT_SECONDS, follow_redirects=True).raise_for_status()
    except Exception as exc:  # noqa: BLE001 - monitoring must never break the crawl
        # Only the host is logged: the URL itself is a credential.
        log.warning("external heartbeat to %s failed: %s", urlsplit(target).hostname,
                    type(exc).__name__)
        return False
    return True


def report_cycle() -> bool:
    """After a finished crawl cycle: run the self-check, record it for the health page and ping
    the monitor, alive when the check is clean and failed otherwise."""
    from .self_check import Problem, find_problems, record
    try:
        problems = find_problems()
    except Exception as exc:  # noqa: BLE001 - a broken check is itself worth an alert
        log.warning("self-check failed: %s", type(exc).__name__)
        problems = [Problem("self_check_error", type(exc).__name__)]
    if problems:
        log.warning("self-check found: %s", ", ".join(problem.code for problem in problems))
    try:
        record(problems)
    except OSError as exc:
        log.warning("could not record the self-check: %s", type(exc).__name__)
    return ping(problems=problems)
