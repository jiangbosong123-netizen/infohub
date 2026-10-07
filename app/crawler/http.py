from __future__ import annotations

"""抓取共用的 HTTP 客户端：浏览器 UA、超时、单次重试，并遵守对方要求的等待时间。"""
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# A 429/503 asking for a short wait is retried in place, as before; a longer Retry-After pauses
# every request to that host (the 23 Google News feeds share one) instead of each source hitting
# it again on its own schedule. FreshRSS and Miniflux honour Retry-After the same way.
INLINE_RETRY_SECONDS = 30.0
MAX_HOST_PAUSE_SECONDS = 6 * 3600.0
_pauses: dict[str, float] = {}
_pauses_lock = threading.Lock()


class HostPaused(RuntimeError):
    """The host asked us to wait; no request was sent."""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_paused_until(url: str, now: float | None = None) -> float | None:
    """Epoch seconds until which ``url``'s host asked us to wait, if it still applies."""
    now = time.time() if now is None else now
    with _pauses_lock:
        until = _pauses.get(_host(url))
    return until if until and until > now else None


def _pause_host(url: str, seconds: float) -> None:
    until = time.time() + min(seconds, MAX_HOST_PAUSE_SECONDS)
    with _pauses_lock:
        host = _host(url)
        _pauses[host] = max(until, _pauses.get(host, 0.0))
    log.warning("主机 %s 要求等待 %.0f 秒，期间暂停对它的所有请求", _host(url), seconds)


def retry_after_seconds(response: httpx.Response, now: datetime | None = None) -> float | None:
    """Retry-After as seconds, from either delta-seconds or an HTTP-date (RFC 9110 §10.2.3)."""
    value = (response.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def fetch(url: str, *, headers: dict | None = None, timeout: float = 25.0,
          retries: int = 1, allow_not_modified: bool = False) -> httpx.Response:
    """GET，失败重试一次；429/503 按 Retry-After 处理。

    With ``allow_not_modified`` a 304 (the reply to a conditional request) is returned instead
    of raised.
    """
    until = host_paused_until(url)
    if until is not None:
        raise HostPaused(
            f"{_host(url)} 要求暂停至 "
            f"{datetime.fromtimestamp(until, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
    merged = {**HEADERS, **(headers or {})}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, headers=merged, timeout=timeout, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001 - 统一记日志后重试
            last_exc = exc
            if attempt < retries:
                time.sleep(2)
            continue
        if allow_not_modified and resp.status_code == 304:
            return resp
        if resp.status_code in (429, 503):
            wait = retry_after_seconds(resp)
            if wait is not None and wait > INLINE_RETRY_SECONDS:
                _pause_host(url, wait)
                resp.raise_for_status()  # no retry: the server said when to come back
            if attempt < retries and (resp.status_code == 429 or wait is not None):
                time.sleep(INLINE_RETRY_SECONDS if wait is None else wait)  # 限流：等一波再试
                continue
        try:
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2)
    raise last_exc  # type: ignore[misc]
