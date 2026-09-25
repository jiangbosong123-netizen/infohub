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
    "4": ("内部人持股变动", "insider"),
    "144": ("内部人拟出售通知", "insider"),
    "S-1": ("IPO 注册", "offering"),
    "S-3ASR": ("增发注册", "offering"),
    "424B5": ("增发定价", "offering"),
    "DEF 14A": ("股东大会委托书", ""),
    "13F-HR": ("机构持仓报告", ""),
    "SC 13G": ("大股东持股披露", ""),
}

# 8-K Item 代码 → 事件类型
_ITEM_MAP = [
    ("1.01", "重大协议", "ma"), ("2.01", "收购完成", "ma"),
    ("2.02", "业绩披露", "earnings"), ("5.02", "高管/董事变动", "personnel"),
    ("4.02", "财报不信任", "earnings"), ("3.01", "退市通知", "regulation"),
    ("2.03", "重大财务义务", ""), ("5.01", "控制权变更", ""),
    ("8.01", "其他事件", ""), ("7.01", "投资者披露", ""), ("9.01", "附件", ""),
]


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


def _classify(form: str, items: str) -> tuple[str, str]:
    """返回 (事件描述, event_type)。"""
    base_form = form.upper()[:-2] if form.upper().endswith("/A") else form.upper()
    desc, etype = _FORM_DESC.get(base_form, ("提交文件", ""))
    if base_form == "8-K" and items:
        for code, d, t in _ITEM_MAP:
            if code in items:
                desc, etype = f"8-K · {d}", t or "other"
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
                        + (f"，条目 {items}" if items else "") + "。",
                # Acceptance is not verified public dissemination time.
                published_at=None,
                event_type=etype, official=1, companies=[c["slug"]],
                extra=dict(
                    form=form, cik=c["cik"], accession=accession,
                    primary_document=doc, filing_date=filing_date,
                    report_date=report_date, items=items,
                    sec_associations=associations,
                ),
                source_time_values=source_times,
                observed_at=response_observed_at.isoformat(),
                source_record={"filing": source_record, "issuer": issuer_record},
                payload_kind="api_record",
            ))
        time.sleep(0.2)  # SEC 限速要求：≤10 req/s，留足余量
    return out
