from __future__ import annotations

"""新浪财经 7x24 快讯：分钟级中文快讯线，覆盖美股港股宏观与个股动态。"""
import logging
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from . import http

log = logging.getLogger(__name__)

API_URL = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
CN_TZ = timezone(timedelta(hours=8))


def fetch_sina(source: dict) -> list[dict]:
    resp = http.fetch(API_URL)
    data = resp.json()
    feed = (((data.get("result") or {}).get("data") or {}).get("feed") or {})
    out = []
    for rec in (feed.get("list") or [])[:50]:
        text = BeautifulSoup(rec.get("rich_text") or "", "lxml").get_text(" ", strip=True)
        if not text:
            continue
        try:
            published = datetime.strptime(rec["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
            published_at = published.astimezone(timezone.utc).isoformat()
        except (KeyError, ValueError):
            published_at = datetime.now(timezone.utc).isoformat()
        # 标题取第一句（长度合适时），否则截断；全文放摘要
        first = re.split(r"[。！？]", text, maxsplit=1)[0]
        if 8 <= len(first) <= 70:
            title = first
        else:
            title = text[:60].rstrip() + ("…" if len(text) > 60 else "")
        out.append(dict(
            url=f"https://finance.sina.com.cn/7x24/?id={rec.get('id')}",
            title=title, summary=text[:400], published_at=published_at,
            event_type="", official=0, companies=None, extra={},
        ))
    return out
