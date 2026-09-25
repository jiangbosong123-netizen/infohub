from __future__ import annotations

"""分钟级财经快讯抓取器（方法来自 GitHub 开源项目的逆向实现）：

- 财联社电报：签名算法抄自 RSSHub 项目（DIYgod/RSSHub，lib/routes/cls/utils.ts）
  sorted(params) → sha1(querystring) → md5(sha1hex)
- 华尔街见闻快讯：API 端点来自 newsnow 项目（ourongxing/newsnow）
"""
import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from . import http
from ..source_time import parse_source_time

log = logging.getLogger(__name__)

CLS_API = "https://www.cls.cn/api/cache"
WSCN_LIVE_API = "https://api-one-wscn.awtmt.com/apiv1/content/lives"


def _cls_sign(params: dict) -> str:
    qs = urlencode(sorted(params.items()))
    sha1 = hashlib.sha1(qs.encode()).hexdigest()
    return hashlib.md5(sha1.encode()).hexdigest()


def _strip_html(raw: str, limit: int = 400) -> str:
    if not raw:
        return ""
    return BeautifulSoup(raw, "lxml").get_text(" ", strip=True)[:limit]


def _make_title(text: str) -> str:
    """快讯常没有标题：取第一句，过长则截断。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    first = re.split(r"[。！？]", text, maxsplit=1)[0]
    if 8 <= len(first) <= 70:
        return first
    return text[:60].rstrip() + ("…" if len(text) > 60 else "")


def fetch_cls(source: dict) -> list[dict]:
    """财联社电报：分钟级中文快讯，重点覆盖港美股公司与宏观。"""
    params = {"appName": "CailianpressWeb", "category": "", "name": "telegraph",
              "os": "web", "sv": "8.7.9"}
    params["sign"] = _cls_sign(params)
    resp = http.fetch(f"{CLS_API}?{urlencode(params)}",
                      headers={"Referer": "https://www.cls.cn/telegraph"})
    response_observed_at = datetime.now(timezone.utc)
    data = resp.json()
    out = []
    for rec in ((data.get("data") or {}).get("roll_data") or [])[:50]:
        content = _strip_html(rec.get("content") or rec.get("brief") or "")
        title = (rec.get("title") or "").strip() or _make_title(content)
        if not title:
            continue
        ctime = rec.get("ctime")
        source_time = parse_source_time(
            ctime, field_path="data.roll_data.ctime", role="published",
            epoch_unit="seconds", interpretation="CLS Unix seconds",
            observed_at=response_observed_at,
        )
        published = source_time.utc if source_time.status == "valid" else None
        link = (rec.get("shareurl") or "").strip() or f"https://www.cls.cn/detail/{rec.get('id')}"
        out.append(dict(url=link, title=title, summary=content,
                        published_at=published, event_type="", official=0,
                        companies=None, extra={},
                        source_time_values=[source_time.to_dict()],
                        observed_at=response_observed_at.isoformat(),
                        source_record=rec, payload_kind="api_record"))
    return out


def fetch_wscn_live(source: dict) -> list[dict]:
    """华尔街见闻快讯（全球频道）：分钟级，比其 RSS 快。"""
    resp = http.fetch(f"{WSCN_LIVE_API}?channel=global-channel&client=web&limit=50")
    response_observed_at = datetime.now(timezone.utc)
    data = resp.json()
    out = []
    for rec in ((data.get("data") or {}).get("items") or [])[:50]:
        title = (rec.get("title") or "").strip()
        content = _strip_html(rec.get("content_text") or "")
        if not title:
            title = _make_title(content)
        if not title:
            continue
        uri = (rec.get("uri") or "").strip()
        if uri and not uri.startswith("http"):
            uri = f"https://wallstreetcn.com{uri}"
        if not uri:
            uri = f"https://wallstreetcn.com/live/{rec.get('id')}"
        ts = rec.get("display_time")
        source_time = parse_source_time(
            ts, field_path="data.items.display_time", role="published",
            epoch_unit="seconds", interpretation="WSCN Unix seconds",
            observed_at=response_observed_at,
        )
        published = source_time.utc if source_time.status == "valid" else None
        out.append(dict(url=uri, title=title, summary=content,
                        published_at=published, event_type="", official=0,
                        companies=None, extra={},
                        source_time_values=[source_time.to_dict()],
                        observed_at=response_observed_at.isoformat(),
                        source_record=rec, payload_kind="api_record"))
    return out
