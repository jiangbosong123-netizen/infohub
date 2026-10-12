from __future__ import annotations

"""Article text for title-only items (D27), off unless INFOHUB_ARTICLE_TEXT_ENABLED=true.

Google News and a few blog feeds give only a title, so curation judged 43% of items from the
headline alone. For recent title-only items the worker resolves the publisher link, honours
robots.txt and a per-site interval, and keeps the extracted text privately: curation reads its
opening and the story page shows a short excerpt with the link. The API never serves it.

Google's consent page is only ever answered with "Reject all".
"""

import hashlib
import json
import logging
import re
import time
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlsplit

import httpx

from .. import config
from ..database import get_db
from ..timeutil import format_utc
from .http import HEADERS, host_paused_until

log = logging.getLogger(__name__)

# Feeds whose entries can lack any excerpt; filings and flash wires are complete as they are.
SOURCE_TYPES = ("googlenews", "rss", "html")
RECENT = timedelta(days=2)  # the backlog is never crawled
MIN_CHARS = 200
MAX_BYTES = 3_000_000
SITE_INTERVAL_SECONDS = 10.0
ROBOTS_TTL_SECONDS = 24 * 3600
RETRY_AFTER = timedelta(hours=1)
MAX_ATTEMPTS = 2
RUN_SECONDS = 240.0
EXCERPT_CHARS = 600


def extractor_id() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return f"trafilatura-{version('trafilatura')}"
    except PackageNotFoundError:
        return "trafilatura"


def eligible_clause(alias: str = "i") -> str:
    """SQL condition for items whose text the job fetches (title-only, from feeds that need it)."""
    types = ",".join(f"'{kind}'" for kind in SOURCE_TYPES)
    return (f"TRIM(COALESCE({alias}.raw_summary,''))='' AND {alias}.source_id IN "
            f"(SELECT id FROM sources WHERE type IN ({types}))")


def decompress(blob: bytes | None) -> str:
    return zlib.decompress(blob).decode("utf-8") if blob else ""


def excerpts(db, item_ids, limit: int = EXCERPT_CHARS) -> dict[int, str]:
    ids = list(item_ids)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(f"""SELECT item_id,text_zlib FROM article_texts
                          WHERE status='ok' AND item_id IN ({marks})""", ids)
    return {row[0]: decompress(row[1])[:limit] for row in rows}


@dataclass
class _Outcome:
    status: str
    article_url: str | None = None
    http_status: int | None = None
    text: str = ""
    error: str | None = None


@dataclass
class Fetcher:
    """One run's HTTP state: Google's consent cookie, robots.txt cache and per-site clock."""

    client: httpx.Client
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = field(default_factory=dict)
    last_request: dict[str, float] = field(default_factory=dict)

    def _wait_for(self, host: str) -> None:
        last = self.last_request.get(host)
        if last is not None:
            remaining = SITE_INTERVAL_SECONDS - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request[host] = self.clock()

    def get(self, url: str, **options) -> httpx.Response:
        self._wait_for((urlsplit(url).hostname or "").lower())
        return self.client.get(url, **options)

    # --- Google News -------------------------------------------------------------------------

    def _reject_consent(self, page: httpx.Response) -> None:
        for body in re.findall(r"<form[^>]*>(.*?)</form>", page.text, re.S):
            fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', body))
            # "Reject all" carries set_eom; "Accept all" carries set_sc and is never sent.
            if fields.get("set_eom") == "true" and "set_sc" not in fields:
                self.client.post("https://consent.google.com/save", data=fields, timeout=20)
                return
        raise ValueError("Google consent page has no Reject-all form")

    def google_article_url(self, link: str) -> str:
        article_id = urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1]
        page_url = f"https://news.google.com/rss/articles/{article_id}"
        page = self.get(page_url, timeout=20)
        if "consent.google.com" in str(page.url):
            self._reject_consent(page)
            page = self.get(page_url, timeout=20)
        page.raise_for_status()
        signature = re.search(r'data-n-a-sg="([^"]+)"', page.text)
        timestamp = re.search(r'data-n-a-ts="([^"]+)"', page.text)
        if not signature or not timestamp:
            raise ValueError("Google News page has no article signature")
        request = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
                                   None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                   article_id, int(timestamp.group(1)), signature.group(1)]
        payload = [[["Fbv4je", json.dumps(request, separators=(",", ":")), None, "generic"]]]
        self._wait_for("news.google.com")
        response = self.client.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            content=f"f.req={quote(json.dumps(payload, separators=(',', ':')))}",
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=20)
        response.raise_for_status()
        frames = json.loads(response.text.split("\n\n", 1)[1])
        url = json.loads(frames[0][2])[1]
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("Google News returned no article link")
        return url

    # --- robots.txt (RFC 9309) ---------------------------------------------------------------

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self.robots.get(origin)
        if cached is None or self.clock() - cached[0] > ROBOTS_TTL_SECONDS:
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self.get(f"{origin}/robots.txt", timeout=10)
                if response.status_code >= 500:
                    parser = None  # unreachable: assume complete disallow for now
                elif response.status_code >= 400:
                    parser.parse([])  # unavailable: no restrictions
                else:
                    parser.parse(response.text.splitlines())
            except httpx.HTTPError:
                parser = None
            cached = (self.clock(), parser)
            self.robots[origin] = cached
        return cached[1] is not None and cached[1].can_fetch("*", url)

    # --- one item ----------------------------------------------------------------------------

    def fetch(self, link: str) -> _Outcome:
        try:
            url = self.google_article_url(link) if urlsplit(link).hostname == "news.google.com" else link
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            return _Outcome("failed", error=f"resolve: {type(exc).__name__}")
        if host_paused_until(url) is not None:
            return _Outcome("failed", url, error="site asked us to wait")
        if not self.allowed(url):
            return _Outcome("blocked", url, error="robots.txt")
        try:
            response = self.get(url, timeout=20)
        except httpx.HTTPError as exc:
            return _Outcome("failed", url, error=type(exc).__name__)
        status = response.status_code
        if status in (401, 403, 451):
            return _Outcome("blocked", url, status)
        if status in (404, 410):
            return _Outcome("gone", url, status)
        if status != 200:
            return _Outcome("failed", url, status, error=f"HTTP {status}")
        if "html" not in response.headers.get("content-type", "") or len(response.content) > MAX_BYTES:
            return _Outcome("not_html", url, status)
        import trafilatura  # only the worker needs it, and only when the feature is on
        text = (trafilatura.extract(response.text, url=str(response.url), favor_precision=True,
                                    include_comments=False) or "").strip()
        if len(text) < MIN_CHARS:
            return _Outcome("no_text", url, status)
        return _Outcome("ok", url, status, text)


def _store(db, item_id: int, outcome: _Outcome, attempts: int, now: datetime) -> None:
    text = outcome.text if outcome.status == "ok" else ""
    retry = outcome.status == "failed" and attempts < MAX_ATTEMPTS
    db.execute(
        """INSERT INTO article_texts(item_id,status,article_url,http_status,attempts,text_zlib,
               text_sha256,chars,extractor,error,fetched_at,next_attempt_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET status=excluded.status,article_url=excluded.article_url,
               http_status=excluded.http_status,attempts=excluded.attempts,text_zlib=excluded.text_zlib,
               text_sha256=excluded.text_sha256,chars=excluded.chars,extractor=excluded.extractor,
               error=excluded.error,fetched_at=excluded.fetched_at,next_attempt_at=excluded.next_attempt_at""",
        (item_id, outcome.status, outcome.article_url, outcome.http_status, attempts,
         zlib.compress(text.encode("utf-8"), 9) if text else None,
         hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None, len(text), extractor_id(),
         (outcome.error or "")[:200] or None, format_utc(now),
         format_utc(now + RETRY_AFTER) if retry else None),
    )


def due_items(db, now: datetime, limit: int) -> list:
    return db.execute(
        f"""SELECT i.id,i.url,COALESCE(a.attempts,0) AS attempts FROM items i
            LEFT JOIN article_texts a ON a.item_id=i.id
            WHERE {eligible_clause()} AND i.fetched_at >= ?
              AND (a.item_id IS NULL OR (a.status='failed' AND a.next_attempt_at <= ?))
            ORDER BY i.id DESC LIMIT ?""",
        ((now - RECENT).isoformat(), format_utc(now), limit),
    ).fetchall()


def fetch_article_texts(limit: int = 40, *, fetcher: Fetcher | None = None,
                        now: datetime | None = None) -> dict:
    """Fetch text for up to ``limit`` recent title-only items, newest first."""
    if not config.ARTICLE_TEXT_ENABLED:
        return {"status": "disabled"}
    config.require_network_tasks("article text")
    current = now or datetime.now(timezone.utc)
    with get_db() as db:
        items = due_items(db, current, limit)
    counts: dict[str, int] = {}
    own_client = None
    if fetcher is None:
        own_client = httpx.Client(follow_redirects=True, headers={**HEADERS, "Accept": "text/html,*/*;q=0.8"})
        fetcher = Fetcher(own_client)
    started = fetcher.clock()
    try:
        for row in items:
            if fetcher.clock() - started > RUN_SECONDS:
                break  # the next run continues; a long run would hold up crawling
            outcome = fetcher.fetch(row["url"])
            with get_db() as db:
                _store(db, row["id"], outcome, row["attempts"] + 1, now or datetime.now(timezone.utc))
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
    finally:
        if own_client is not None:
            own_client.close()
    return {"status": "ok", "candidates": len(items), **counts}


def waiting_for_text_clause(now: datetime, alias: str = "i") -> tuple[str, tuple]:
    """Curation skips a title-only item until its text is fetched, for at most WAIT minutes."""
    if not config.ARTICLE_TEXT_ENABLED:
        return "", ()
    cutoff = (now - timedelta(minutes=config.ARTICLE_TEXT_WAIT_MINUTES)).isoformat()
    return (f" AND NOT ({eligible_clause(alias)} AND {alias}.fetched_at >= ? AND NOT EXISTS "
            f"(SELECT 1 FROM article_texts a WHERE a.item_id={alias}.id))", (cutoff,))

