from __future__ import annotations

"""RSS 抓取器（feedparser），产出统一 RawItem。"""
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
from bs4 import BeautifulSoup

from . import http
from ..company_match import match_companies


def _to_iso(entry) -> str:
    for attr in ("published_parsed", "updated_parsed"):
        tp = getattr(entry, attr, None)
        if tp:
            return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


def _clean_html(raw: str, limit: int = 400) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "lxml").get_text(" ", strip=True)
    return text[:limit]


def fetch_rss(source: dict) -> list[dict]:
    """返回 RawItem 列表：{url,title,summary,published_at,event_type,official,companies,extra}"""
    resp = http.fetch(source["url"])
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
        published = _to_iso(entry)
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
        ))
    return out
