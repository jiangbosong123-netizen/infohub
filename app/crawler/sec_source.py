from __future__ import annotations

"""SEC EDGAR 抓取器：保留申报、发行人和交易代码的官方来源记录。

文档地址：https://www.sec.gov/submissions JSON（无需 Key，需声明 User-Agent）。
CIK 缺失时从 SEC 官方 ticker/exchange 关联文件解析并回填数据库。
"""
import json
import time
from datetime import datetime, timezone

from .. import config
from ..database import get_db
from ..source_time import parse_source_time
from . import http

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

_FORM_DESC = {
    "8-K": ("重大事件报告", ""),
    "10-Q": ("季报", "earnings"),
    "10-K": ("年报", "earnings"),
    "20-F": ("外国发行人年报", "earnings"),
    "40-F": ("加拿大外国发行人年报", "earnings"),
    "6-K": ("外国发行人临时报告", "other"),
    "3": ("内部人首次持股报告", "insider"),
    "4": ("内部人持股变动", "insider"),
    "5": ("内部人年度持股报告", "insider"),
    "144": ("内部人拟出售通知", "insider"),
    "SC 13D": ("大股东持股披露（主动）", "insider"),
    "SCHEDULE 13D": ("大股东持股披露（主动）", "insider"),
    "SC 13G": ("大股东持股披露", "insider"),
    "SCHEDULE 13G": ("大股东持股披露", "insider"),
    "S-1": ("IPO 注册", "offering"),
    "S-3": ("证券发行注册", "offering"),
    "S-3ASR": ("增发注册", "offering"),
    "424B5": ("增发定价", "offering"),
    "FWP": ("发行自由书面材料", "offering"),
    # S-4 registers securities for a merger or for an exchange offer: in 2026 Amazon's was a
    # merger and Broadcom's an exchange of notes, so the form alone does not say which.
    "S-4": ("合并或置换证券注册", "other"),
    "S-8": ("员工股权计划注册", "other"),
    "DEF 14A": ("股东大会委托书", ""),
    "13F-HR": ("机构持仓报告", ""),
}

# Form 8-K items (General Instructions B of Form 8-K), most telling first: a filing often lists
# several, and the first one found here names the filing and sets its event type. 1.01 alone is
# not an acquisition: in the 2025-12..2026-10 rehearsal data all five 1.01 filings were credit
# agreements, note indentures or charter changes, so it ranks below 2.03 (financing) and keeps
# "other". 9.01 (exhibits) accompanies most filings and never decides.
_ITEM_MAP = [
    ("1.03", "破产或接管", "regulation"),
    ("2.02", "业绩披露", "earnings"),
    ("4.02", "前期财报不可依赖", "earnings"),
    ("2.01", "收购或处置完成", "ma"),
    ("5.01", "控制权变更", "ma"),
    ("3.01", "退市或不符合上市标准通知", "regulation"),
    ("1.05", "重大网络安全事件", "other"),
    ("2.06", "重大资产减值", "earnings"),
    ("5.02", "高管/董事变动", "personnel"),
    ("2.03", "新增直接债务", "offering"),
    ("3.02", "未注册股权发行", "offering"),
    ("2.05", "退出或处置活动成本", "other"),
    ("1.01", "签订重大协议", "other"),
    ("1.02", "终止重大协议", "other"),
    ("2.04", "触发债务加速", "other"),
    ("4.01", "更换审计师", "other"),
    ("3.03", "证券持有人权利重大变更", "other"),
    ("5.03", "章程或财年变更", "other"),
    ("5.07", "股东大会表决结果", "other"),
    ("5.08", "股东提名董事", "other"),
    ("5.05", "道德守则修订或豁免", "other"),
    ("5.04", "员工福利计划交易暂停", "other"),
    ("5.06", "空壳公司状态变更", "other"),
    ("1.04", "矿山安全", "other"),
    ("7.01", "投资者披露", "other"),
    ("8.01", "其他事件", "other"),
]
_ITEM_NAMES = {code: name for code, name, _ in _ITEM_MAP} | {"9.01": "财务报表与附件"}

# Insiders' ownership reports and planned-sale notices: 507 of the 641 SEC filings in the rehearsal
# data (Form 4 376, 144 122, 3 9). Archived and searchable, but marked routine so the selected feed
# does not show each one (owner decision D25, 2026-10-07). Schedules 13D/13G are not included.
_ROUTINE_FORMS = {"3", "4", "5", "144"}


def _routine(form: str) -> bool:
    base_form = form.upper()[:-2] if form.upper().endswith("/A") else form.upper()
    return base_form in _ROUTINE_FORMS


def _sec_headers() -> dict:
    return {"User-Agent": config.SEC_USER_AGENT, "Accept": "application/json"}


def _at(values: dict, key: str, index: int):
    items = values.get(key)
    return items[index] if isinstance(items, list) and index < len(items) else None


def _source_times(values: dict, index: int) -> list[dict]:
    return [
        parse_source_time(
            _at(values, "acceptanceDateTime", index),
            field_path="filings.recent.acceptanceDateTime", role="accepted",
            interpretation="SEC submission acceptance time",
        ).to_dict(),
        parse_source_time(
            _at(values, "filingDate", index), field_path="filings.recent.filingDate",
            role="filing_date", interpretation="SEC filing calendar date",
            calendar_date=True,
        ).to_dict(),
        parse_source_time(
            _at(values, "reportDate", index), field_path="filings.recent.reportDate",
            role="report_period", interpretation="SEC report period end date",
            calendar_date=True,
        ).to_dict(),
    ]


def _parse_associations(payload: object) -> list[dict]:
    """Parse SEC's column-oriented ticker/exchange association file."""
    if not isinstance(payload, dict):
        raise RuntimeError("SEC ticker/exchange association payload is not an object")
    fields = payload.get("fields")
    data = payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise RuntimeError("SEC ticker/exchange association payload has an unknown shape")
    required = {"cik", "name", "ticker", "exchange"}
    if not required.issubset(fields):
        raise RuntimeError("SEC ticker/exchange association fields are incomplete")
    out = []
    for values in data:
        if not isinstance(values, list) or len(values) != len(fields):
            continue
        row = dict(zip(fields, values))
        if row.get("cik") is None or not row.get("ticker") or not row.get("exchange"):
            continue
        out.append({
            "cik": str(row["cik"]).zfill(10),
            "name": str(row.get("name") or "").strip(),
            "ticker": str(row["ticker"]).strip().upper(),
            "exchange": str(row["exchange"]).strip().upper(),
        })
    return out


def resolve_missing_ciks(companies: list[dict]) -> dict[str, list[dict]]:
    """Resolve CIKs and return all SEC-asserted listings for each CIK."""
    resp = http.fetch(TICKERS_URL, headers=_sec_headers())
    associations = _parse_associations(json.loads(resp.text))
    ticker_ciks: dict[str, set[str]] = {}
    for row in associations:
        ticker_ciks.setdefault(row["ticker"], set()).add(row["cik"])
    by_ticker = {
        ticker: next(iter(ciks)) for ticker, ciks in ticker_ciks.items() if len(ciks) == 1
    }
    by_cik: dict[str, list[dict]] = {}
    for row in associations:
        by_cik.setdefault(row["cik"], []).append(row)
    with get_db() as db:
        for c in companies:
            cik = str(c.get("cik") or "").zfill(10) if c.get("cik") else ""
            if not cik:
                cik = by_ticker.get(c["ticker"].upper(), "")
            if cik:
                if not c.get("cik"):
                    db.execute("UPDATE companies SET cik=? WHERE slug=?", (cik, c["slug"]))
                c["cik"] = cik
    return by_cik


def _item_codes(items: str) -> list[str]:
    """SEC lists 8-K items as "2.02,9.01"; compare whole codes, never substrings."""
    return [code.strip() for code in (items or "").split(",") if code.strip()]


def _items_text(items: str) -> str:
    return "、".join(f"{code} {_ITEM_NAMES[code]}" if code in _ITEM_NAMES else code
                    for code in _item_codes(items))


def _classify(form: str, items: str) -> tuple[str, str]:
    """返回 (事件描述, event_type)。"""
    base_form = form.upper()[:-2] if form.upper().endswith("/A") else form.upper()
    desc, etype = _FORM_DESC.get(base_form, ("提交文件", ""))
    if base_form == "8-K" and items:
        listed = set(_item_codes(items))
        for code, d, t in _ITEM_MAP:
            if code in listed:
                desc, etype = f"8-K · {d}", t
                break
        else:
            desc, etype = "8-K · 重大事件", "other"
    elif base_form.startswith(("S-", "424B", "F-1")):
        desc, etype = _FORM_DESC.get(base_form, ("证券发行", "offering"))
    if form.upper().endswith("/A"):
        desc = f"{desc}修订"
    return desc, etype


def fetch_sec(source: dict) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, name, name_zh, ticker, cik FROM companies WHERE market='US' AND ticker != ''"
        ).fetchall()
    companies = [dict(r) for r in rows]
    associations_by_cik = resolve_missing_ciks(companies)

    out: list[dict] = []
    for c in companies:
        if not c.get("cik"):
            out.append(dict(_error=f"{c['slug']}: 无法解析 SEC CIK"))
            continue
        try:
            resp = http.fetch(
                f"https://data.sec.gov/submissions/CIK{c['cik']}.json",
                headers=_sec_headers(),
            )
            response_observed_at = datetime.now(timezone.utc)
            data = json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001 - 单家公司失败不影响其他家
            out.append(dict(_error=f"{c['slug']}: {exc}"))
            continue
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        zh = c["name_zh"] or c["name"]
        for i in range(min(len(forms), 40)):
            form = forms[i]
            accession = _at(recent, "accessionNumber", i)
            doc = _at(recent, "primaryDocument", i) or ""
            items = _at(recent, "items", i) or ""
            if not accession:
                continue
            if not doc:
                continue
            desc, etype = _classify(form, items)
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(c['cik'])}/"
                   f"{accession.replace('-', '')}/{doc}")
            filing_date = _at(recent, "filingDate", i)
            report_date = _at(recent, "reportDate", i)
            source_times = _source_times(recent, i)
            source_record = {
                key: _at(recent, key, i)
                for key, values in recent.items()
                if isinstance(values, list)
            }
            associations = associations_by_cik.get(c["cik"], [])
            issuer_record = {
                "cik": str(data.get("cik") or c["cik"]).zfill(10),
                "name": data.get("name"),
                "formerNames": data.get("formerNames"),
                "tickers": data.get("tickers"),
                "exchanges": data.get("exchanges"),
                "tickerExchangeAssociations": associations,
            }
            out.append(dict(
                url=url,
                title=f"{zh} · SEC {desc}" + (f"（{items}）" if items and "·" in desc else ""),
                summary=f"{c['name']}（{c['ticker']}）向 SEC 提交 {form}"
                        + (f"，条目 {_items_text(items)}" if items else "") + "。",
                # Acceptance is not verified public dissemination time.
                published_at=None,
                event_type=etype, official=1, companies=[c["slug"]],
                extra=dict(
                    form=form, cik=c["cik"], accession=accession,
                    primary_document=doc, filing_date=filing_date,
                    report_date=report_date, items=items,
                    sec_associations=associations,
                    **({"routine": True} if _routine(form) else {}),
                ),
                source_time_values=source_times,
                observed_at=response_observed_at.isoformat(),
                source_record={"filing": source_record, "issuer": issuer_record},
                payload_kind="api_record",
            ))
        time.sleep(0.2)  # SEC 限速要求：≤10 req/s，留足余量
    return out
