from __future__ import annotations

"""RSS 抓取器（feedparser），产出统一 RawItem。"""
from datetime import datetime, timezone

import feedparser
from bs4 import BeautifulSoup

from . import http
from ..company_match import match_companies
from ..source_time import parse_source_time


def _source_times(entry, observed_at: datetime) -> tuple[str | None, list[dict]]:
    values = []
    published = getattr(entry, "published", None)
    updated = getattr(entry, "updated", None)
    if published is not None:
        parsed = parse_source_time(
            published, field_path="entry.published", role="published", parser="feed",
            interpretation="RSS publisher-reported publication time",
            observed_at=observed_at,
        )
        values.append(parsed.to_dict())
    else:
        values.append(parse_source_time(
            None, field_path="entry.published", role="published",
            interpretation="RSS entry has no publication field",
        ).to_dict())
    if updated is not None:
        values.append(parse_source_time(
            updated, field_path="entry.updated", role="updated", parser="feed",
            interpretation="RSS publisher-reported update time",
        ).to_dict())
    # updated is never promoted to a publisher publication claim. P06c removes
    # the legacy items NOT NULL fallback once document versions take over.
    trusted = next((value["utc"] for value in values
                    if value["role"] == "published" and value["status"] == "valid"), None)
    return trusted, values


def _clean_html(raw: str, limit: int = 400) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "lxml").get_text(" ", strip=True)
    return text[:limit]


def fetch_rss(source: dict) -> list[dict]:
    """返回 RawItem 列表：{url,title,summary,published_at,event_type,official,companies,extra}"""
    resp = http.fetch(source["url"])
    observed_at = datetime.now(timezone.utc)
    parsed = feedparser.parse(resp.content)
    if not parsed.entries and getattr(parsed, "bozo", False):
        # 防止「限流空页/坏 XML 静默成功」：有解析异常且零条目要当失败处理
        raise RuntimeError(f"feed 解析失败: {parsed.get('bozo_exception')}")
    out: list[dict] = []
    bound_slug = source.get("company_slug") or ""

    for entry in parsed.entries[:60]:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "").strip()
        if not url or not title:
            continue
        summary = _clean_html(getattr(entry, "summary", "") or getattr(entry, "description", ""))
        published, source_times = _source_times(entry, observed_at)
        text = f"{title} {summary}"

        # 个股源（Yahoo 等）噪声多：标题/摘要必须命中绑定公司的别名才收
        if bound_slug:
            slugs = match_companies(text)
            if bound_slug not in slugs:
                continue
        else:
            slugs = None  # 由 runner 自动匹配

        out.append(dict(
            url=url, title=title, summary=summary, published_at=published,
            event_type="", official=0, companies=slugs, extra={},
            source_time_values=source_times,
            observed_at=observed_at.isoformat(),
            source_record=dict(entry), payload_kind="feed_entry",
        ))
    return out
