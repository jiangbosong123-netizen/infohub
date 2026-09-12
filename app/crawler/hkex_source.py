from __future__ import annotations

"""港交所披露易抓取器：港股官方公告（业绩/回购/收购等）。

接口：prefix.do 查股票内部 stockId（缓存回填），titleSearchServlet.do 按日期拉公告列表。
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from ..config import APP_TZ
from ..database import get_db
from . import http
from .sources import HKEX_BASE

_TITLE_RULES = [
    (r"購回|回购| buyback", "buyback"),
    (r"業績|业绩|盈警|盈喜|盈利警告|正面盈利", "earnings"),
    (r"收購|收购|合併|合并|配售|供股", "ma"),
    (r"辭任|辞任|委任|董事变动|退任", "personnel"),
    (r"月報表|月报表", ""),  # 每月证券变动例行报表，噪音，跳过
]


def _resp_json(resp) -> object:
    text = resp.text.strip()
    m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text, re.S)  # JSONP 兜底
    if m:
        text = m.group(1)
    return json.loads(text)


def resolve_stock_id(code: str) -> str | None:
    url = (f"{HKEX_BASE}/search/prefix.do?callback=callback&lang=ZH&type=A"
           f"&name={quote(code)}&market=SEHK")
    try:
        data = _resp_json(http.fetch(url))
        info = (data.get("stockInfo") or [{}])[0]
        return str(info.get("stockId") or "") or None
    except Exception:
        return None


def _classify(title: str) -> str:
    for pattern, etype in _TITLE_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            return etype
    return "other"


def _fetch_company(companies_row, from_date: str, to_date: str) -> list[dict]:
    stock_id = companies_row["hkex_stock_id"]
    if not stock_id:
        stock_id = resolve_stock_id(companies_row["code"])
        if stock_id:
            with get_db() as db:
                db.execute("UPDATE companies SET hkex_stock_id=? WHERE slug=?",
                           (stock_id, companies_row["slug"]))
        else:
            return []
        time.sleep(0.5)
    url = (f"{HKEX_BASE}/search/titleSearchServlet.do?sortDir=0&sortByOptions=DateTime"
           f"&category=0&market=SEHK&stockId={stock_id}&documentType=-1"
           f"&fromDate={from_date}&toDate={to_date}&title=&searchType=1"
           f"&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=100&lang=zh")
    data = _resp_json(http.fetch(url, headers={"Referer": f"{HKEX_BASE}/search/titlesearch.xhtml"}))
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, str):  # 接口把数组二次编码成 JSON 字符串
        result = json.loads(result)
    zh = companies_row["name_zh"] or companies_row["name"]
    out = []
    for rec in (result or [])[:60]:
        title = (rec.get("LONG_TEXT") or rec.get("SHORT_TEXT") or rec.get("TITLE") or "")
        title = title.replace("<br/>", " ").strip()
        link = (rec.get("FILE_LINK") or "").strip()
        if not title or not link:
            continue
        etype = _classify(title)
        if not etype:  # 例行月报表等噪音
            continue
        try:
            published = datetime.strptime(rec["DATE_TIME"], "%d/%m/%Y %H:%M").replace(tzinfo=APP_TZ)
            published_at = published.astimezone(timezone.utc).isoformat()  # 统一存 UTC，保证全局排序正确
        except Exception:
            published_at = datetime.now(timezone.utc).isoformat()
        out.append(dict(
            url=f"{HKEX_BASE}{link}" if link.startswith("/") else link,
            title=f"{zh} · 港交所公告：{title}",
            summary=f"{companies_row['name']}（{companies_row['code']}.HK）于港交所披露：{title}",
            published_at=published_at,
            event_type=etype, official=1, companies=[companies_row["slug"]],
            extra=dict(code=companies_row["code"]),
        ))
    return out


def fetch_hkex(source: dict) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, name, name_zh, code, hkex_stock_id FROM companies WHERE market='HK'"
        ).fetchall()
    today = datetime.now(APP_TZ).date()
    from_date = (today - timedelta(days=2)).strftime("%Y%m%d")  # 抓最近 3 天，防漏
    to_date = today.strftime("%Y%m%d")
    out = []
    for row in rows:
        try:
            out.extend(_fetch_company(row, from_date, to_date))
        except Exception as exc:  # noqa: BLE001 - 单家公司失败不影响其他家
            out.append(dict(_error=f"{row['slug']}: {exc}"))
        time.sleep(0.5)
    return out
