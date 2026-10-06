from __future__ import annotations

"""Flash-feed catch-up: page back after a gap until the feed meets what is already stored.

The minute-level feeds (Sina 7x24, CLS telegraph, WSCN live) return only their newest page.
When none of that page is stored yet, items were published faster than the poll interval or
the collector was down, so older pages are read until one contains a stored item. Bounds: at
most ``MAX_PAGES`` more pages, and never past the source's last successful run (less a margin);
a source that has never succeeded reads only its newest page, as before. Items from older pages
that are already stored are left out, so catch-up never touches existing rows.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..database import get_db

log = logging.getLogger(__name__)

MAX_PAGES = 20
PAGE_DELAY_SECONDS = 1.0
RESUME_MARGIN = timedelta(minutes=15)

# (raws, cursor of the next older page or None)
Page = tuple[list[dict], object]


def _normalized(raw: dict) -> str:
    from .runner import _normalize_url  # the runner imports the connectors that import this
    return _normalize_url(raw.get("url") or "")


def _resume_point(source_key: str) -> datetime | None:
    with get_db() as db:
        row = db.execute(
            "SELECT last_success_at FROM sources WHERE key=?", (source_key,)
        ).fetchone()
    if row is None or not row["last_success_at"]:
        return None
    last = datetime.fromisoformat(row["last_success_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last - RESUME_MARGIN


def _stored(urls: set[str]) -> set[str]:
    if not urls:
        return set()
    with get_db() as db:
        return {row["url"] for row in db.execute(
            f"SELECT url FROM items WHERE url IN ({','.join('?' * len(urls))})", sorted(urls)
        )}


def _oldest(raws: list[dict]) -> datetime | None:
    times = []
    for raw in raws:
        try:
            value = datetime.fromisoformat(str(raw.get("published_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is not None:
            times.append(value)
    return min(times) if times else None


def catch_up(
    source: dict, first: Page, older: Callable[[object], Page],
) -> list[dict]:
    """The newest page's raws, then any unstored raws from older pages that close a gap."""
    raws, cursor = first
    key = source.get("key")
    if not key or not raws or cursor is None:
        return raws
    resume = _resume_point(key)
    if resume is None:
        return raws
    out = list(raws)
    seen = {_normalized(raw) for raw in raws}
    page = raws
    for number in range(2, MAX_PAGES + 2):
        if _stored({_normalized(raw) for raw in page} - {""}):
            break  # the feed meets what is stored
        oldest = _oldest(page)
        if oldest is not None and oldest < resume:
            break  # older than the last successful run: nothing left to close
        if cursor is None:
            break
        time.sleep(PAGE_DELAY_SECONDS)
        try:
            page, cursor = older(cursor)
        except Exception as exc:  # noqa: BLE001 - keep what was read, report the rest
            log.warning("源 %s 补抓第 %d 页失败: %s", key, number, exc)
            out.append({"_error": f"补抓第 {number} 页失败: {exc}"[:300]})
            break
        if not page:
            break
        urls = {_normalized(raw) for raw in page} - seen - {""}
        stored = _stored(urls)
        for raw in page:
            url = _normalized(raw)
            if url and url not in seen and url not in stored:
                out.append(raw)
            seen.add(url)
        log.info("源 %s 断档补抓第 %d 页", key, number)
    return out
