from __future__ import annotations

"""新浪财经 7x24 快讯：分钟级中文快讯线，覆盖美股港股宏观与个股动态。"""
import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from . import http
from ..source_time import parse_source_time

log = logging.getLogger(__name__)

API_URL = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1")


def fetch_sina(source: dict) -> list[dict]:
    resp = http.fetch(API_URL)
    response_observed_at = datetime.now(timezone.utc)
    data = resp.json()
    feed = (((data.get("result") or {}).get("data") or {}).get("feed") or {})
    out = []
    for rec in (feed.get("list") or [])[:50]:
        text = BeautifulSoup(rec.get("rich_text") or "", "lxml").get_text(" ", strip=True)
        if not text:
            continue
        source_time = parse_source_time(
            rec.get("create_time"), field_path="result.data.feed.list.create_time",
            role="published", timezone_name="Asia/Shanghai",
            pattern="%Y-%m-%d %H:%M:%S",
            interpretation="Sina 7x24 local publication time",
            observed_at=response_observed_at,
        )
        published_at = source_time.utc if source_time.status == "valid" else None
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
            source_time_values=[source_time.to_dict()],
            observed_at=response_observed_at.isoformat(),
            source_record=rec, payload_kind="api_record",
        ))
    return out
