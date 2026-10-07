from __future__ import annotations

"""港交所披露易抓取器：港股官方公告（业绩/回购/收购等）。

接口：prefix.do 查股票内部 stockId（缓存回填），titleSearchServlet.do 按日期拉公告列表。
每条记录的 TITLE 是公告标题，LONG_TEXT 是港交所的标题分类“一级分类 - [二级分类 / 二级分类]”。
"""
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..database import get_db
from ..source_time import parse_source_time
from . import http
from .sources import HKEX_BASE

# Event types from HKEX headline categories (the tier-two names of its Headline Categories, as
# listed by hkexnews.hk/ncms/script/eds/tiertwo_c.json), most telling first: an announcement
# often carries several, e.g. 內幕消息 / 季度業績. Unlisted categories (AGM notices, book closure,
# share schemes, 內幕消息 alone) are "other".
_CATEGORY_EVENTS = (
    ("earnings", {
        "季度業績", "中期業績", "末期業績", "盈利警告", "延遲發表業績公告", "修訂已刊發初步業績的資料",
        "修改已刊發的財務報表及報告", "修正重大錯誤而作出的前期調整", "核數師發出「非標準報告」",
        "附屬公司的業績", "年報", "中期/半年度報告", "季度報告",
        # Results filed on another exchange first (SMIC's Shanghai quarterly reports).
        "其他-營運業績最新情況", "海外監管公告-營運業績最新情況",
    }),
    ("ma", {
        "須予披露的交易", "主要交易", "非常重大的收購事項", "非常重大的出售事項", "反收購", "股份交易",
        "終止交易", "分拆", "集團重組或協議安排", "《收購守則》所指的要約公司刊發的公告",
        "《收購守則》所指的受要約公司刊發的公告", "《收購守則》所指的要約公司發出的文件",
        "《收購守則》所指的受要約公司發出的文件",
    }),
    ("offering", {
        "配售", "供股", "公開招股", "代價發行", "根據一般性授權發行股份", "根據特定授權發行股份",
        "發行股份", "發行債務證券", "發行優先股", "發行可轉換證券", "發行權證", "主要附屬公司發行證券",
    }),
    ("buyback", {"股份購回", "根據《公司股份回購守則》發出的公告", "根據《公司股份回購守則》刊發的文件"}),
    ("personnel", {"更換董事或重要行政職能或職責的變更", "更換行政總裁", "更換公司秘書", "更換監事"}),
    ("regulation", {
        "停牌", "短暫停牌", "復牌", "私有化/撤銷或取消證券上市", "私有化/撤銷證券上市",
        "發行人、其控股公司或主要附屬公司結束營業及清盤", "其他-訴訟",
    }),
)
# Tier-one categories whose documents are of one kind, except early redemptions
# (贖回或購回 － 債務證券), which return money rather than raise it.
_GROUP_EVENTS = {"債券及結構性產品": "offering"}
# 每月证券变动例行报表，噪音，跳过
_SKIPPED_GROUPS = {"月報表"}
# Next-day disclosure returns are filed on most trading days a company buys back or issues shares
# (322 of 700 announcements of the watched companies in 2026). They are archived and searchable,
# but marked routine so the selected feed does not show each one (owner decision, 2026-10-07).
_ROUTINE_GROUPS = {"翌日披露報表"}
# HKEX's tier-one categories for listed issuers (tierone_c.json); a record under one of them is
# categorised even without a tier-two name (委任代表表格, 憲章文件), so its title is not guessed at.
_TIER_ONE = {
    "公告及通告", "通函", "上市文件", "財務報表/環境、社會及管治資料", "翌日披露報表", "月報表",
    "委任代表表格", "公司資料報表", "憲章文件", "展示文件", "監管者發出的公告及消息",
    "合併守則 - 交易披露", "債券及結構性產品", "展示文件（債務證券發行計劃）", "展示文件（債務證券）",
    "申請版本、整體協調人公告及聆訊後資料集",
}

_TITLE_RULES = [
    (r"購回|回购| buyback", "buyback"),
    (r"業績|业绩|盈警|盈喜|盈利警告|正面盈利", "earnings"),
    (r"收購|收购|合併|合并|配售|供股", "ma"),
    (r"辭任|辞任|委任|董事变动|退任", "personnel"),
    (r"月報表|月报表", ""),  # 每月证券变动例行报表，噪音，跳过
]
HKEX_TZ = ZoneInfo("Asia/Hong_Kong")


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


def _clean(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or "").replace("<br/>", " "))).strip()


def _headline_category(record: dict) -> tuple[str, list[str]]:
    """("一级分类", ["二级分类", ...]) from LONG_TEXT, e.g. 公告及通告 - [內幕消息 / 季度業績]."""
    text = _clean(record.get("LONG_TEXT") or record.get("SHORT_TEXT"))
    match = re.fullmatch(r"(.+?) - \[(.+)\]", text)
    if not match:
        return text, []
    # Names such as 中期/半年度報告 contain an unspaced slash; categories are joined by " / ".
    return match.group(1), [name.strip() for name in match.group(2).split(" / ") if name.strip()]


def _classify(title: str, group: str = "", categories: list[str] | tuple[str, ...] = ()) -> str:
    """Event type from the headline category; the title rules only decide uncategorised records."""
    if group in _SKIPPED_GROUPS:
        return ""
    for etype, names in _CATEGORY_EVENTS:
        if names.intersection(categories):
            return etype
    if group in _GROUP_EVENTS and not any(name.startswith("贖回或購回") for name in categories):
        return _GROUP_EVENTS[group]
    if categories or group in _TIER_ONE:
        return "other"
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
            raise RuntimeError(f"无法解析 {companies_row['slug']} 的港交所 stockId")
        time.sleep(0.5)
    url = (f"{HKEX_BASE}/search/titleSearchServlet.do?sortDir=0&sortByOptions=DateTime"
           f"&category=0&market=SEHK&stockId={stock_id}&documentType=-1"
           f"&fromDate={from_date}&toDate={to_date}&title=&searchType=1"
           f"&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=100&lang=zh")
    response = http.fetch(url, headers={"Referer": f"{HKEX_BASE}/search/titlesearch.xhtml"})
    response_observed_at = datetime.now(timezone.utc)
    data = _resp_json(response)
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, str):  # 接口把数组二次编码成 JSON 字符串
        result = json.loads(result)
    if not isinstance(result,list):
        raise RuntimeError('港交所返回缺少有效公告列表，不能记为无公告')
    zh = companies_row["name_zh"] or companies_row["name"]
    out = []
    for rec in (result or [])[:60]:
        group, categories = _headline_category(rec)
        title = _clean(rec.get("TITLE")) or group
        category_text = group + (f" - {' / '.join(categories)}" if categories else "")
        if categories and title == group:  # 翌日披露報表 etc.: the category says what it reports
            title = f"{title}（{'、'.join(categories)}）"
            category_text = ""
        link = (rec.get("FILE_LINK") or "").strip()
        if not title or not link:
            continue
        etype = _classify(title, group, categories)
        if not etype:  # 例行月报表等噪音
            continue
        source_time = parse_source_time(
            rec.get("DATE_TIME"), field_path="result.DATE_TIME", role="published",
            timezone_name="Asia/Hong_Kong", pattern="%d/%m/%Y %H:%M",
            interpretation="HKEX announcement publication time",
            observed_at=response_observed_at,
        )
        published_at = source_time.utc if source_time.status == "valid" else None
        out.append(dict(
            url=f"{HKEX_BASE}{link}" if link.startswith("/") else link,
            title=f"{zh} · 港交所公告：{title}",
            summary=f"{companies_row['name']}（{companies_row['code']}.HK）于港交所披露：{title}"
                    + (f"（分类：{category_text}）" if category_text and category_text != title else ""),
            published_at=published_at,
            event_type=etype, official=1, companies=[companies_row["slug"]],
            extra=dict(code=companies_row["code"], hkex_category=group, hkex_subcategories=categories,
                       **({"routine": True} if group in _ROUTINE_GROUPS else {})),
            source_time_values=[source_time.to_dict()],
            observed_at=response_observed_at.isoformat(),
            source_record=rec, payload_kind="api_record",
        ))
    return out


def fetch_hkex(source: dict) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, name, name_zh, code, hkex_stock_id FROM companies WHERE market='HK'"
        ).fetchall()
    today = datetime.now(HKEX_TZ).date()
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
