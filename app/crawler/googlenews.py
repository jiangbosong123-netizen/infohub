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
from ..ingest import begin_ingest_run, finish_ingest_run, observe_candidate
from ..source_time import parse_source_time
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
    observed_at = datetime.now(timezone.utc)
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
        raw_time = getattr(entry, "published", None)
        source_time = parse_source_time(
            raw_time, field_path="entry.published", role="other", parser="feed",
            interpretation="Google News aggregator-reported entry time",
            observed_at=observed_at, check_future=True,
        )
        published = source_time.utc if source_time.status == "valid" else None
        out.append(dict(
            url=link, title=title, summary="", published_at=published,
            event_type="", official=0, companies=[slug],
            extra=dict(publisher=publisher),
            source_time_values=[source_time.to_dict()],
            observed_at=observed_at.isoformat(),
            source_record=dict(entry), payload_kind="feed_entry",
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

    from .sources import SOURCES

    source = next(item for item in SOURCES if item["key"] == "google-news")
    ingest_run = begin_ingest_run(source)
    with get_db() as db:
        rows = db.execute("SELECT slug, name, name_zh, aliases FROM companies").fetchall()
    stats = {}
    accepted = 0
    duplicates = 0
    rejected = 0
    observed_bytes = 0
    raw_total = 0
    successful_requests = 0
    ordinal = 0
    for row in rows:
        aliases = json.loads(row["aliases"]) if isinstance(row["aliases"], str) else []
        try:
            fetched = fetch_company_news(row["slug"], row["name"], aliases)
        except Exception as exc:  # noqa: BLE001
            log.warning("对账抓取失败 %s: %s", row["slug"], exc)
            stats[row["slug"]] = dict(fetched=0, inserted=0, error=str(exc))
            continue
        inserted = 0
        company_error = None
        successful_requests += 1
        raw_total += len(fetched)
        for raw in fetched:
            current_ordinal = ordinal
            ordinal += 1
            try:
                observation = observe_candidate(
                    ingest_run, raw, ordinal=current_ordinal,
                    observed_at=raw.get("observed_at"),
                )
                observed_bytes += observation.size_bytes
                if insert_item(
                    "google-news", raw, via="reconcile", observation=observation
                ):
                    inserted += 1
                else:
                    duplicates += 1
                accepted += 1
            except Exception as exc:  # noqa: BLE001
                rejected += 1
                log.exception("对账条目证据或入库失败 %s", row["slug"])
                company_error = f"证据或入库失败: {type(exc).__name__}"
                continue
        # 检查媒体层是否 24 小时内完全没抓到这家公司（可能是某源挂了）
        with get_db() as db:
            cnt = db.execute(
                """SELECT COUNT(*) AS n FROM items
                   WHERE companies LIKE ? AND via='normal' AND fetched_at >= ?""",
                (f'%"{row["slug"]}"%', (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()),
            ).fetchone()["n"]
        stats[row["slug"]] = dict(fetched=len(fetched), inserted=inserted, media_24h=cnt)
        if company_error:
            stats[row["slug"]]["error"] = company_error
    if stats:
        from .runner import _record
        failures = [slug for slug,value in stats.items() if 'error' in value]
        finish_ingest_run(
            ingest_run,
            status=(
                "succeeded" if not failures
                else ("partial" if successful_requests else "failed")
            ),
            request_count=len(rows),
            raw_count=raw_total,
            accepted_count=accepted,
            duplicate_count=duplicates,
            rejected_count=rejected,
            byte_count=observed_bytes,
            error_code="reconcile_partial" if failures else None,
        )
        _record('google-news',ok=not failures,new=sum(value['inserted'] for value in stats.values()),
                message='对账失败公司：'+', '.join(failures) if failures else '',
                partial=bool(failures) and len(failures)<len(stats))
        from ..stories import refresh_derived
        refresh_derived()
    else:
        finish_ingest_run(
            ingest_run,
            status="succeeded",
            request_count=0,
            raw_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            byte_count=0,
        )
    return stats
