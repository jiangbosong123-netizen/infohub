from __future__ import annotations

"""SEC EDGAR 抓取器：保留申报、发行人和交易代码的官方来源记录。

文档地址：https://www.sec.gov/submissions JSON（无需 Key，需声明 User-Agent）。
CIK 缺失时从 SEC 官方 ticker/exchange 关联文件解析并回填数据库。
"""
import json
import re
import time
from datetime import datetime, timezone

import httpx

from .. import config
from ..database import get_db
from ..source_time import parse_source_time
from . import http

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# The submissions API's acceptanceDateTime is not a reliable instant. Checked against EDGAR's
# filing headers on 2026-10-13: Apple's ten latest filings read 4 h late, Microsoft's matched, and
# NVIDIA's newest matched while its older ones read 4 h late; the old collector stored values 4 h
# early for filings it caught within minutes (11 of 60 sampled). The header of each filing states
# the acceptance time as Eastern wall-clock time, so a filing's time is read from there, once.
HEADER_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index-headers.html"
_HEADER_FIELDS = re.compile(r"^<(ACCEPTANCE-DATETIME|ACCESSION-NUMBER)>(\S+)", re.MULTILINE)
# Header reads per run, newest filings first; the rest wait for a later run. A newly followed
# company brings 40 filings at once, and an empty dataset of 13 companies fills in about 90 minutes.
HEADER_READS_PER_RUN = 60

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


def _source_times(values: dict, index: int, header: dict | None = None) -> list[dict]:
    times = [
        parse_source_time(
            _at(values, "acceptanceDateTime", index),
            field_path="filings.recent.acceptanceDateTime", role="other",
            interpretation="SEC submissions API acceptanceDateTime; not the acceptance time, "
                           "it has read 4 h either side of the filing header",
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
    if header:
        times.append(parse_source_time(
            header.get("acceptance_datetime"), field_path="header.acceptance_datetime",
            role="accepted", interpretation="SEC EDGAR filing header ACCEPTANCE-DATETIME",
            timezone_name="America/New_York", pattern="%Y%m%d%H%M%S",
            observed_at=header.get("read_at"), check_future=True,
        ).to_dict())
    return times


class HeaderFormatError(RuntimeError):
    """EDGAR answered, but not with this filing's header in the format read here."""


def read_acceptance(cik: str, accession: str) -> dict | None:
    """The filing's header, reduced to its acceptance time (Eastern, YYYYMMDDHHMMSS).

    None when EDGAR has no header for the filing (404/410). HeaderFormatError when the page names
    another filing or has no acceptance time, as a format change would; other HTTP and network
    errors propagate, so a caller can stop and try again later.
    """
    url = HEADER_URL.format(cik=int(cik), folder=accession.replace("-", ""), accession=accession)
    try:
        # No inline retry: a throttled host would otherwise cost each header two requests and up to
        # 30 s; the caller decides when to stop.
        response = http.fetch(url, headers={"User-Agent": config.SEC_USER_AGENT}, retries=0)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 410):
            return None
        raise
    read_at = datetime.now(timezone.utc).isoformat()
    fields: dict[str, str] = {}
    for name, value in _HEADER_FIELDS.findall(response.text):
        fields.setdefault(name, value)
    value = fields.get("ACCEPTANCE-DATETIME", "")
    if fields.get("ACCESSION-NUMBER") != accession or not re.fullmatch(r"\d{14}", value):
        raise HeaderFormatError(f"{accession}: header without a matching accession and acceptance time")
    return {"url": url, "acceptance_datetime": value, "read_at": read_at}


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


def _known_headers(urls: list[str]) -> dict[str, dict | None]:
    """Filings already stored, with the header read when each was first collected (if any)."""
    if not urls:
        return {}
    with get_db() as db:
        rows = db.execute(
            f"""SELECT url, CASE WHEN json_valid(extra) THEN json_extract(extra,'$.sec_acceptance') END AS header
                FROM items WHERE url IN ({",".join("?" * len(urls))})""",
            urls,
        ).fetchall()
    return {row["url"]: json.loads(row["header"]) if row["header"] else None for row in rows}


def _read_new_headers(filings: list[dict], known: dict[str, dict | None], out: list[dict]) -> dict:
    """Headers for filings not stored yet, newest first and at most HEADER_READS_PER_RUN.

    A filing left out waits for a later run instead of being stored without its time, so an old
    filing never looks new. A filing without a header (404) is stored anyway, at the time it was
    seen. The first HTTP error in a run defers only that filing, so one header that always fails
    does not hold back the others; a second one, a network failure or a pause the host asked for
    stops the reads, as a throttled or failing host answers every request the same way. A header in an unknown format is stored without its time and
    reported, so a format change shows on the source's health instead of passing unnoticed.
    """
    headers: dict[str, dict | None] = {}
    new = sorted((f for f in filings if f["url"] not in known),
                 key=lambda f: (str(f["filing_date"] or ""), str(f["api_accepted"] or "")), reverse=True)
    deferred, unreadable = [], []
    for filing in new[:HEADER_READS_PER_RUN]:
        try:
            headers[filing["url"]] = read_acceptance(filing["cik"], filing["accession"])
        except HeaderFormatError:
            headers[filing["url"]] = None
            unreadable.append(filing["accession"])
        except httpx.HTTPStatusError as exc:  # 第一次出错只跳过这一份；再出错就是主机的问题，停下
            deferred.append(f"{filing['accession']} HTTP {exc.response.status_code}")
            if len(deferred) > 1:
                out.append(dict(_error=f"SEC 文件头读取失败，{len(new) - len(headers)} 份新文件留到下一轮: "
                                       f"{', '.join(deferred)}"))
                deferred = []
                break
        except Exception as exc:  # noqa: BLE001 - 断网或被要求暂停：停下，剩下的下一轮再读
            out.append(dict(_error=f"SEC 文件头读取失败，{len(new) - len(headers)} 份新文件留到下一轮: {exc}"))
            break
        time.sleep(0.15)  # SEC 限速要求：≤10 req/s
    if deferred:
        out.append(dict(_error=f"SEC 文件头读取失败，{len(deferred)} 份留到下一轮: {', '.join(deferred[:3])}"))
    if unreadable:
        out.append(dict(_error=f"SEC 文件头格式无法识别，{len(unreadable)} 份按首次看到的时间入库: {', '.join(unreadable[:3])}"))
    return headers


def fetch_sec(source: dict) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, name, name_zh, ticker, cik FROM companies WHERE market='US' AND ticker != ''"
            " AND (origin='watchlist' OR covered=1)"
        ).fetchall()
    companies = [dict(r) for r in rows]
    associations_by_cik = resolve_missing_ciks(companies)

    out: list[dict] = []
    filings: list[dict] = []
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
        for i in range(min(len(forms), 40)):
            accession = _at(recent, "accessionNumber", i)
            doc = _at(recent, "primaryDocument", i) or ""
            if not accession:
                continue
            if not doc:
                continue
            filings.append(dict(
                company=c, data=data, recent=recent, index=i, cik=c["cik"], accession=accession,
                url=(f"https://www.sec.gov/Archives/edgar/data/{int(c['cik'])}/"
                     f"{accession.replace('-', '')}/{doc}"),
                filing_date=_at(recent, "filingDate", i),
                api_accepted=_at(recent, "acceptanceDateTime", i),
                observed_at=response_observed_at,
            ))
        time.sleep(0.2)  # SEC 限速要求：≤10 req/s，留足余量

    known = _known_headers([f["url"] for f in filings])
    headers = {**_read_new_headers(filings, known, out), **known}
    for filing in filings:
        if filing["url"] in headers:
            out.append(_candidate(filing, headers[filing["url"]], associations_by_cik))
    return out


def _candidate(filing: dict, header: dict | None, associations_by_cik: dict[str, list[dict]]) -> dict:
    c, data, recent, i = filing["company"], filing["data"], filing["recent"], filing["index"]
    form = _at(recent, "form", i)
    doc = _at(recent, "primaryDocument", i) or ""
    items = _at(recent, "items", i) or ""
    desc, etype = _classify(form, items)
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
    observed_at = filing["observed_at"]
    if header:  # the stored record is complete once the header is read
        observed_at = max(observed_at, datetime.fromisoformat(header["read_at"]))
    zh = c["name_zh"] or c["name"]
    return dict(
        url=filing["url"],
        title=f"{zh} · SEC {desc}" + (f"（{items}）" if items and "·" in desc else ""),
        summary=f"{c['name']}（{c['ticker']}）向 SEC 提交 {form}"
                + (f"，条目 {_items_text(items)}" if items else "") + "。",
        # Acceptance is not verified public dissemination time; the portal sorts by it.
        published_at=None,
        event_type=etype, official=1, companies=[c["slug"]],
        extra=dict(
            form=form, cik=c["cik"], accession=filing["accession"],
            primary_document=doc, filing_date=filing["filing_date"],
            report_date=_at(recent, "reportDate", i), items=items,
            sec_associations=associations,
            **({"sec_acceptance": header} if header else {}),
            **({"routine": True} if _routine(form) else {}),
        ),
        source_time_values=_source_times(recent, i, header),
        observed_at=observed_at.isoformat(),
        source_record={"filing": source_record, "issuer": issuer_record,
                       **({"header": header} if header else {})},
        payload_kind="api_record",
    )
