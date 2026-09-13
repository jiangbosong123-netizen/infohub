from __future__ import annotations

"""Google News 对账兜底层：按公司聚合全网新闻，每日核对补漏。

Google News 支持按任意关键词生成 RSS：news.google.com/rss/search?q=...&when:2d
对每家关注公司拉一遍最新新闻，凡是没入库的（漏抓的）就补录，via 标记为 reconcile。
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser

from ..company_match import match_companies
from ..database import get_db
from . import http

log = logging.getLogger(__name__)


def _clean_title(title: str) -> str:
    """Google News 标题尾部带 ' - 媒体名'，去掉。"""
    return re.sub(r"\s+-\s+[^-]{1,40}$", "", title or "").strip()


def _norm_title(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", (t or "").lower())


def _company_query(aliases: list[str]) -> str:
    return " OR ".join(f'"{a}"' for a in aliases[:5])


def fetch_company_news(slug: str, name: str, aliases: list[str], when: str = "2d") -> list[dict]:
    q = quote(f"{_company_query(aliases)} when:{when}")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    resp = http.fetch(url, timeout=30)
    parsed = feedparser.parse(resp.content)
    if not parsed.entries and (parsed.get('bozo') or not parsed.get('version')):
        raise RuntimeError('Google News 返回的内容不是有效 RSS，不能记为无新闻')
    out = []
    for entry in parsed.entries[:50]:
        title = _clean_title(getattr(entry, "title", ""))
        link = getattr(entry, "link", "")
        if not title or not link:
            continue
        # 严格匹配：必须命中公司别名，防止 Google News 的相关性漂移
        if slug not in match_companies(title):
            continue
        publisher = getattr(entry, "source", None)
        publisher = publisher.get("title") if publisher and hasattr(publisher, "get") else ""
        tp = getattr(entry, "published_parsed", None)
        published = (datetime(*tp[:6], tzinfo=timezone.utc) if tp
                     else datetime.now(timezone.utc)).isoformat()
        out.append(dict(
            url=link, title=title, summary="", published_at=published,
            event_type="", official=0, companies=[slug],
            extra=dict(publisher=publisher),
        ))
    return out


def fetch_google_news(source: dict) -> list[dict]:
    """常规轮询入口：按源绑定的公司抓 Google News。"""
    slug = source.get("company_slug") or ""
    if not slug:
        raise RuntimeError("google news 源缺少 company_slug（对账源请走 run_reconcile）")
    with get_db() as db:
        row = db.execute("SELECT slug, name, aliases FROM companies WHERE slug=?",
                         (slug,)).fetchone()
    if not row:
        return []
    return fetch_company_news(row["slug"], row["name"], json.loads(row["aliases"]))


def run_reconcile() -> dict:
    """对每家公司执行对账补漏。返回统计 {company: (fetched, inserted, missing_24h)}。"""
    from .runner import insert_item

    with get_db() as db:
        rows = db.execute("SELECT slug, name, name_zh, aliases FROM companies").fetchall()
    stats = {}
    for row in rows:
        aliases = json.loads(row["aliases"]) if isinstance(row["aliases"], str) else []
        try:
            fetched = fetch_company_news(row["slug"], row["name"], aliases)
        except Exception as exc:  # noqa: BLE001
            log.warning("对账抓取失败 %s: %s", row["slug"], exc)
            stats[row["slug"]] = dict(fetched=0, inserted=0, error=str(exc))
            continue
        inserted = 0
        for raw in fetched:
            if insert_item("google-news", raw, via="reconcile"):
                inserted += 1
        # 检查媒体层是否 24 小时内完全没抓到这家公司（可能是某源挂了）
        with get_db() as db:
            cnt = db.execute(
                """SELECT COUNT(*) AS n FROM items
                   WHERE companies LIKE ? AND via='normal' AND fetched_at >= ?""",
                (f'%"{row["slug"]}"%', (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()),
            ).fetchone()["n"]
        stats[row["slug"]] = dict(fetched=len(fetched), inserted=inserted, media_24h=cnt)
    if stats:
        from .runner import _record
        failures = [slug for slug,value in stats.items() if 'error' in value]
        _record('google-news',ok=not failures,new=sum(value['inserted'] for value in stats.values()),
                message='对账失败公司：'+', '.join(failures) if failures else '',
                partial=bool(failures) and len(failures)<len(stats))
        from ..stories import refresh_derived
        refresh_derived()
    return stats
