from __future__ import annotations

"""SEC EDGAR 抓取器：美股官方文件（8-K 重大事件 / 10-Q 季报 / Form 4 内部人交易等）。

文档地址：https://www.sec.gov/submissions JSON（无需 Key，需声明 User-Agent）。
CIK 缺失时自动从 SEC 官方 ticker 映射表解析并回填数据库。
"""
import json
import time
from datetime import datetime, timezone

from .. import config
from ..database import get_db
from . import http

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_FORM_DESC = {
    "8-K": ("重大事件报告", ""),
    "10-Q": ("季报", "earnings"),
    "10-K": ("年报", "earnings"),
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


def _parse_ts(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    iso = raw.replace(" ", "T")
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).isoformat()
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return datetime.now(timezone.utc).isoformat()


def resolve_missing_ciks(companies: list[dict]) -> None:
    """从 SEC 官方映射表解析缺失的 CIK 并写回 companies 表。"""
    missing = [c for c in companies if not c.get("cik")]
    if not missing:
        return
    resp = http.fetch(TICKERS_URL, headers=_sec_headers())
    mapping = {
        row["ticker"].upper(): str(row["cik_str"]).zfill(10)
        for row in json.loads(resp.text).values()
    }
    with get_db() as db:
        for c in missing:
            cik = mapping.get(c["ticker"].upper())
            if cik:
                db.execute("UPDATE companies SET cik=? WHERE slug=?", (cik, c["slug"]))
                c["cik"] = cik


def _classify(form: str, items: str) -> tuple[str, str]:
    """返回 (事件描述, event_type)。"""
    desc, etype = _FORM_DESC.get(form, ("提交文件", ""))
    if form.startswith("8-K") and items:
        for code, d, t in _ITEM_MAP:
            if code in items:
                return f"8-K · {d}", t or "other"
        return "8-K · 重大事件", "other"
    if form.startswith(("S-", "424B", "F-1")):
        return _FORM_DESC.get(form, ("证券发行", "offering"))
    return desc, etype


def fetch_sec(source: dict) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, name, name_zh, ticker, cik FROM companies WHERE market='US' AND ticker != ''"
        ).fetchall()
    companies = [dict(r) for r in rows]
    resolve_missing_ciks(companies)

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
            data = json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001 - 单家公司失败不影响其他家
            out.append(dict(_error=f"{c['slug']}: {exc}"))
            continue
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        zh = c["name_zh"] or c["name"]
        for i in range(min(len(forms), 40)):
            form = forms[i]
            accession = recent["accessionNumber"][i]
            doc = (recent.get("primaryDocument") or [""])[i] if isinstance(recent.get("primaryDocument"), list) else ""
            items = (recent.get("items") or [""])[i] if isinstance(recent.get("items"), list) else ""
            if not doc:
                continue
            desc, etype = _classify(form, items)
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(c['cik'])}/"
                   f"{accession.replace('-', '')}/{doc}")
            out.append(dict(
                url=url,
                title=f"{zh} · SEC {desc}" + (f"（{items}）" if items and "·" in desc else ""),
                summary=f"{c['name']}（{c['ticker']}）向 SEC 提交 {form}"
                        + (f"，条目 {items}" if items else "") + "。",
                published_at=_parse_ts(recent["acceptanceDateTime"][i]),
                event_type=etype, official=1, companies=[c["slug"]],
                extra=dict(form=form, cik=c["cik"]),
            ))
        time.sleep(0.2)  # SEC 限速要求：≤10 req/s，留足余量
    return out
