from __future__ import annotations

"""News for consumers that bring their own company list: /api/v1/signals/news (D31, M2 first step).

Three scopes, as agreed with the owner on 2026-10-18 (PLATFORM_DESIGN §5.1):
- company: items linked to the requested CIKs;
- industry and macro: items whose title or summary matches a theme rule (app/market_themes.py).

Point in time:
- available_at is when InfoHub first saw the item; a backtest may use only items with
  available_at <= as_of;
- the AI judgement (importance, mood) comes later, at judged_at. When as_of is given, a judgement
  is returned only if it was made by then.

Each item says how independent it is: official (the company or a regulator), report (the first
copy of a story), or repost (a later copy of the same story).
"""

import json
import re
from datetime import datetime, timedelta, timezone

from . import market_themes
from .ai.text_quality import usable

ALGORITHM = "news-signals-v1"
SCOPES = ("company", "industry", "macro")
DEFAULT_MIN_SCORE = {"company": 0, "industry": 50, "macro": 70}
MAX_CIKS = 100
MAX_LIMIT = 500
MAX_WINDOW = timedelta(days=31)
MAX_THEME_WINDOW = timedelta(days=7)  # theme scopes read every relevant item in the window
_CIK = re.compile(r"^\d{1,10}$")
_CANDIDATE_CHUNK = 1000


class NewsQueryError(ValueError):
    """A request the endpoint refuses (422)."""


def _time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NewsQueryError(f"{name} must be an ISO 8601 time") from None
    if parsed.tzinfo is None:
        raise NewsQueryError(f"{name} must include a time zone")
    return parsed.astimezone(timezone.utc)


def normalize_cik(value: str) -> str:
    if not _CIK.match(value.strip()):
        raise NewsQueryError("cik must be 1 to 10 digits")
    return value.strip().zfill(10)


def _after(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    stamp, _, item_id = value.rpartition("~")
    if not stamp or not item_id.isdigit():
        raise NewsQueryError("after must come from next_after")
    return stamp, int(item_id)


_COLUMNS = """i.id, i.title, i.title_zh, i.summary, i.url, i.channel, i.event_type, i.official, i.score, i.tmt,
              i.published_at, i.fetched_at, i.extra, s.name AS source_name, s.tier AS source_tier,
              m.mood, m.judged_at, si.story_id"""
_JOINS = """JOIN sources s ON s.id=i.source_id
            LEFT JOIN item_moods m ON m.item_id=i.id
            LEFT JOIN story_items si ON si.item_id=i.id"""


def query_news(db, *, scope: str, ciks: list[str], themes: list[str], since: str, as_of: str | None,
               min_score: int | None, limit: int, after: str | None, include_routine: bool,
               now: datetime | None = None) -> dict:
    if scope not in SCOPES:
        raise NewsQueryError("scope must be company, industry or macro")
    start = _time(since, "since")
    strict = as_of is not None
    end = _time(as_of, "as_of") if strict else (now or datetime.now(timezone.utc))
    if not start < end or end - start > (MAX_WINDOW if scope == "company" else MAX_THEME_WINDOW):
        raise NewsQueryError("since must be before as_of, at most 31 days (7 for industry and macro)")
    if not 1 <= limit <= MAX_LIMIT:
        raise NewsQueryError(f"limit must be 1 to {MAX_LIMIT}")
    floor = DEFAULT_MIN_SCORE[scope] if min_score is None else min_score
    if not 0 <= floor <= 100:
        raise NewsQueryError("min_score must be 0 to 100")
    position = _after(after)
    wanted_themes: set[str] = set()
    company_ids: dict[int, str] = {}
    unknown: list[str] = []
    if scope == "company":
        if not ciks or len(ciks) > MAX_CIKS or themes:
            raise NewsQueryError(f"company scope takes 1 to {MAX_CIKS} cik values and no theme")
        normalized = sorted({normalize_cik(cik) for cik in ciks})
        rows = db.execute(f"""SELECT id, cik FROM companies WHERE cik IN ({",".join("?" * len(normalized))})""",
                          normalized).fetchall()
        company_ids = {row["id"]: row["cik"] for row in rows}
        unknown = sorted(set(normalized) - set(company_ids.values()))
    else:
        if ciks:
            raise NewsQueryError("cik belongs to the company scope")
        scoped = {key for key, theme in market_themes.THEMES.items() if theme["scope"] == scope}
        if set(themes) - scoped:
            raise NewsQueryError(f"unknown {scope} theme")
        wanted_themes = set(themes) or scoped

    window = [start.isoformat(), end.isoformat()]
    order = ""
    if position:
        order = " AND (i.fetched_at>? OR (i.fetched_at=? AND i.id>?))"
        window += [position[0], position[0], position[1]]
    picked: list[dict] = []
    if scope == "company" and company_ids:
        rows = db.execute(
            f"""SELECT DISTINCT {_COLUMNS} FROM item_companies ic JOIN items i ON i.id=ic.item_id {_JOINS}
                WHERE ic.company_id IN ({",".join("?" * len(company_ids))})
                  AND i.fetched_at>? AND i.fetched_at<=?{order} AND COALESCE(i.tmt,1)!=0
                ORDER BY i.fetched_at, i.id""",
            [*company_ids, *window]).fetchall()
        for row in rows:
            item = _item(row, strict, end, floor, include_routine)
            if item is not None:
                picked.append(item)
                if len(picked) > limit:
                    break
    elif scope != "company":
        offset = 0
        while len(picked) <= limit:
            rows = db.execute(
                f"""SELECT {_COLUMNS} FROM items i {_JOINS}
                    WHERE i.fetched_at>? AND i.fetched_at<=?{order} AND COALESCE(i.tmt,1)!=0
                      AND (? = 0 OR i.score>=?)
                    ORDER BY i.fetched_at, i.id LIMIT ? OFFSET ?""",
                [*window, floor, floor, _CANDIDATE_CHUNK, offset]).fetchall()
            for row in rows:
                item = _item(row, strict, end, floor, include_routine)
                if item is None:
                    continue
                matched = [key for key in market_themes.classify(row["title"], row["title_zh"], row["summary"])
                           if key in wanted_themes]
                if matched:
                    item["themes"] = matched
                    picked.append(item)
                    if len(picked) > limit:
                        break
            if len(rows) < _CANDIDATE_CHUNK:
                break
            offset += _CANDIDATE_CHUNK
    more = len(picked) > limit
    picked = picked[:limit]
    _attach(db, picked)
    for item in picked:
        if scope == "company":
            item["themes"] = market_themes.classify(item["title"], item["title_zh"], item.pop("_summary"))
        else:
            item.pop("_summary")
    last = picked[-1] if picked else None
    coverage = {}
    if company_ids:
        rows = db.execute(
            f"""SELECT c.cik, p.started_at, p.basis FROM company_coverage_periods p JOIN companies c ON c.id=p.company_id
                WHERE p.ended_at IS NULL AND p.company_id IN ({",".join("?" * len(company_ids))})""",
            list(company_ids)).fetchall()
        coverage = {row["cik"]: {"covered_since": row["started_at"], "basis": row["basis"]} for row in rows}
    gaps = [{"from": row["started_at"], "to": row["ended_at"], "reason": row["reason"]}
            for row in db.execute("SELECT started_at, ended_at, reason FROM collection_gaps ORDER BY started_at")]
    return {
        "scope": scope, "since": start.isoformat(), "as_of": end.isoformat(), "point_in_time": strict,
        "coverage": coverage, "collection_gaps": gaps,
        "min_score": floor, "algorithm": ALGORITHM, "themes_version": market_themes.THEMES_VERSION,
        "unknown_ciks": unknown, "data": picked,
        "next_after": f"{last['available_at']}~{last['id']}" if more and last else None,
    }


def _item(row, strict: bool, end: datetime, floor: int, include_routine: bool) -> dict | None:
    try:
        extra = json.loads(row["extra"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    routine = bool(isinstance(extra, dict) and extra.get("routine"))
    if routine and not include_routine:
        return None
    judged_at = row["judged_at"]
    # score -1: the model's content filter refused the item; there is no judgement to give.
    refused = row["score"] is not None and row["score"] < 0
    known = row["score"] is not None and not refused and (
        not strict or (judged_at is not None and datetime.fromisoformat(judged_at) <= end))
    importance = row["score"] if known else None
    if floor and (importance is None or importance < floor):
        return None
    publisher = extra.get("publisher") if isinstance(extra, dict) else None
    return {
        "id": row["id"], "available_at": row["fetched_at"], "published_at": row["published_at"],
        "title": row["title"], "url": row["url"],
        # "-" marks a title the model would not translate; garbled model text is never handed out.
        "title_zh": row["title_zh"] if row["title_zh"] not in (None, "", "-") and usable(row["title_zh"]) else None,
        "publisher": publisher or row["source_name"], "source": row["source_name"],
        "official": bool(row["official"]), "story_id": row["story_id"], "routine": routine,
        "event_type": row["event_type"] or None, "channel": row["channel"],
        "importance": importance, "mood": row["mood"] if known else None,
        "judged_at": judged_at if known else None, "refused": refused, "_summary": row["summary"],
    }


def _attach(db, items: list[dict]) -> None:
    """CIKs and independence for the page: official, the first copy of its story, or a repost."""
    if not items:
        return
    ids = [item["id"] for item in items]
    marks = ",".join("?" * len(ids))
    ciks: dict[int, list[str]] = {}
    slugs: dict[int, list[tuple[str, str]]] = {}
    for row in db.execute(f"""SELECT ic.item_id, c.cik, c.slug FROM item_companies ic JOIN companies c ON c.id=ic.company_id
                              WHERE ic.item_id IN ({marks}) AND c.cik!='' ORDER BY c.cik""", ids):
        ciks.setdefault(row["item_id"], []).append(row["cik"])
        slugs.setdefault(row["item_id"], []).append((row["cik"], row["slug"]))
    stories = sorted({item["story_id"] for item in items if item["story_id"]})
    first: dict[str, int] = {}
    if stories:
        first = {row[0]: row[1] for row in db.execute(
            f"""SELECT story_id, MIN(item_id) FROM story_items WHERE story_id IN ({",".join("?" * len(stories))})
                GROUP BY story_id""", stories)}
    from .company_match import mentions_in_own_feed
    for item in items:
        item["ciks"] = ciks.get(item["id"], [])
        # The companies named in the headline itself: the story is about them, not a passing mention.
        headline = "\n".join(value for value in (item["title"], item["title_zh"]) if value)
        item["headline_ciks"] = [cik for cik, slug in slugs.get(item["id"], []) if mentions_in_own_feed(slug, headline)]
        if item["official"]:
            item["independence"] = "official"
        elif item["story_id"] and first.get(item["story_id"]) != item["id"]:
            item["independence"] = "repost"
        else:
            item["independence"] = "report"


SIGNALS_KEY_DAYS = 365


def issue_signals_key(db, consumer: str, *, actor: str, now: datetime | None = None) -> str:
    """One read:signals key for ``consumer`` (created if new); its older keys are revoked, so only the
    newest copy works. Returns the token, shown once."""
    from .api_auth import create_consumer, issue_api_key, revoke_api_key
    current = now or datetime.now(timezone.utc)
    row = db.execute("SELECT id,status FROM api_consumers WHERE name=?", (consumer,)).fetchone()
    if row and row[1] != "active":
        raise NewsQueryError(f"API consumer {consumer} was revoked; issue a key with api-admin instead")
    consumer_id = row[0] if row else create_consumer(db, consumer, actor=actor, now=current)
    for (key_id,) in db.execute("SELECT key_id FROM api_keys WHERE consumer_id=? AND revoked_at IS NULL",
                                (consumer_id,)).fetchall():
        revoke_api_key(db, key_id, actor=actor, now=current)
    return issue_api_key(db, consumer_id, {"read:signals"}, actor=actor, now=current,
                         expires_at=current + timedelta(days=SIGNALS_KEY_DAYS)).token

