from __future__ import annotations

"""网页抓取器：没有 RSS 的站点。每个源注册一个解析函数。"""
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import http
from ..source_time import parse_source_time


def _parse_jiqizhixin(resp, observed_at: datetime) -> list[dict]:
    """机器之心：首页文章链接 /articles/YYYY-MM-DD-xxx。"""
    soup = BeautifulSoup(resp.text, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/articles/([\d-]+[a-z0-9]*)", href)
        if not m:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 8:  # 过滤导航等短文本
            continue
        url = urljoin("https://www.jiqizhixin.com", href)
        if url in seen:
            continue
        seen.add(url)
        source_time = parse_source_time(
            None, field_path="article.published", role="published",
            interpretation="listing page exposes no publication timestamp",
        )
        out.append(dict(url=url, title=title, summary="", published_at=None,
                        event_type="", official=0, companies=None, extra={},
                        source_time_values=[source_time.to_dict()],
                        observed_at=observed_at.isoformat()))
    return out[:40]


# 源 key → 解析函数。加新 html 源时在这里注册。
HTML_PARSERS = {
    "jiqizhixin": _parse_jiqizhixin,
}


def fetch_html(source: dict) -> list[dict]:
    parser = HTML_PARSERS.get(source["key"])
    if parser is None:
        raise RuntimeError(f"html 源 {source['key']} 没有注册解析函数")
    resp = http.fetch(source["url"])
    return parser(resp, datetime.now(timezone.utc))
