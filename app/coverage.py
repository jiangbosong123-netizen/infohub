from __future__ import annotations

"""Which companies InfoHub collects beyond watchlist.yaml (D31, docs/spec/PLATFORM_DESIGN.md §4).

Tiers are market rules over the security master, and a higher tier includes the lower ones:
- tier 1: the AI compute chain by industry;
- tier 2: the top 100 by market cap;
- tier 3: the top 500.

Consumers' coverage requests (CIK lists in config/coverage.yaml) only add companies to collect.
They never change the data model or what anyone else can query, so a changed list is a config
edit, not a code change.

``sync`` turns the plan into ``companies`` rows with origin='coverage':
- watchlist companies keep their own row, topics and Google News feeds;
- coverage companies get SEC filings plus a Nasdaq per-symbol feed (app/crawler/sources.py);
- companies that drop out of coverage are kept with covered=0, and their feed is disabled.
"""

import json
import re
from pathlib import Path

import yaml

from . import company_match, config
from .database import get_db

COVERAGE_PATH = config.BASE_DIR / "config" / "coverage.yaml"
LISTED_EXCHANGES = ("NYSE", "NASDAQ")
EXCLUDED_INDUSTRIES = {"Blank Checks"}  # SPACs are not operating companies
# Nasdaq names of listed lines that are not common stock (security_master.display_name keeps them).
_NOT_COMMON = re.compile(r"\b(Preferred (Stock|Shares|Securities)|Depositary Shares|Notes? due|Senior Notes|"
                         r"Warrants?|Rights|Tangible Equity|Corporate Units)\b", re.IGNORECASE)
# Legal suffixes dropped to get the name people write in headlines.
_LEGAL = re.compile(
    r"[,\s]+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|n\.?\s?v|"
    r"s\.?a|ag|se|lp|llc|l\.?p)\.?$", re.IGNORECASE)
# One-word short names that are also everyday words (or another company's name): they need the legal
# form, a ticker or a cashtag. Other one-word names match case-sensitively: "Intel", not "intel".
COMMON_WORDS = {
    "benchmark", "coherent", "corning", "progress", "pinnacle", "regional", "general", "advanced",
    "national", "american", "international", "universal", "standard", "summit", "frontier", "target",
    "block", "visa", "gap", "shell", "match", "ball", "crown", "cadence", "quanta", "credo", "global",
    "united", "first", "energy", "power", "digital", "alpha", "focus", "ambarella", "align", "trade",
    "insight", "unity", "diodes", "applied", "texas", "western", "analog", "super", "monolithic",
    "silicon", "micro", "lattice", "taiwan", "north", "south", "east", "west", "new", "great", "pure",
    "smart", "data", "cloud", "ultra", "onto",
}
# "Micron Technology" is "Micron" in headlines: a generic last word lets the first word stand alone.
GENERIC_TAILS = {
    "technology", "technologies", "systems", "networks", "semiconductor", "semiconductors", "electronics",
    "devices", "computer", "instruments", "materials", "research", "industries", "communications",
    "solutions", "services", "manufacturing", "international", "innovation", "integrations", "labs",
}


def tier() -> int:
    return config.COVERAGE_TIER


def load(path: Path = COVERAGE_PATH) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tiers = {int(key): value for key, value in (data.get("tiers") or {}).items()}
    return {"tiers": tiers, "requests": data.get("requests") or {}}


def short_name(name: str) -> str:
    """'Teradyne Inc.' -> 'Teradyne'; 'Eaton Corporation PLC' -> 'Eaton'."""
    previous, current = None, name.strip()
    while previous != current:
        previous, current = current, _LEGAL.sub("", current).strip(" ,.")
    return current


CASE_SENSITIVE = "="  # alias prefix understood by company_match


def aliases(display_name: str, ticker: str) -> list[str]:
    """Aliases safe for matching any article: cashtag, legal name, short name.

    A multi-word short name matches in any case. A one-word one matches only as written ("=Intel"),
    unless it is an everyday word, and never when it has three letters or fewer.
    """
    found = [f"${ticker}"]
    short = short_name(display_name)
    legal = display_name.strip(" ,.")
    words = short.split()
    if len(words) >= 2:
        found.append(short)
        if (len(words) == 2 and words[1].lower() in GENERIC_TAILS and len(words[0]) >= 4
                and words[0].lower() not in COMMON_WORDS):
            found.append(CASE_SENSITIVE + words[0])
    elif len(short) >= 4 and short.lower() not in COMMON_WORDS:
        found.append(CASE_SENSITIVE + short)
    if legal and legal.lower() != short.lower():
        found.append(legal)
    return list(dict.fromkeys(found))


def bound_aliases(display_name: str, ticker: str) -> list[str]:
    """Extra names accepted only in the company's own feed, where the context is already known."""
    return [name for name in (short_name(display_name), ticker) if name]


def _listed(db) -> list:
    return db.execute(
        f"""SELECT ticker,cik,COALESCE(display_name,name) AS name,market_cap,sector,industry FROM security_master
            WHERE exchange IN ({",".join("?" * len(LISTED_EXCHANGES))}) AND market_cap IS NOT NULL""",
        LISTED_EXCHANGES).fetchall()


def plan(db, level: int, settings: dict | None = None) -> dict[int, dict]:
    """CIK -> {ticker, name, market_cap, industry, reasons} for ``level`` (0 = nothing)."""
    settings = settings or load()
    if level <= 0:
        return {}
    rows = [row for row in _listed(db) if row["industry"] not in EXCLUDED_INDUSTRIES]
    tickers: dict[int, list[str]] = {}
    for row in rows:
        tickers.setdefault(row["cik"], []).append(row["ticker"])
    # SMCIP (a preferred), BTSGU (units), HBANL (depositary shares): a line whose ticker extends
    # another of the same company's is not its common stock, however large Nasdaq quotes it.
    lines = [row for row in rows
             if not any(row["ticker"] != other and row["ticker"].startswith(other) for other in tickers[row["cik"]])]
    # BMNP (BitMine's preferred, next to BMNR) shares no prefix; its name still says what it is.
    # A company whose only listed lines look like that keeps them.
    common = [row for row in lines if not _NOT_COMMON.search(row["name"] or "")]
    named = {row["cik"] for row in common}
    by_cik: dict[int, dict] = {}
    for row in common + [row for row in lines if row["cik"] not in named]:
        held = by_cik.get(row["cik"])
        if held is None or row["market_cap"] > held["market_cap"]:  # one row per company, largest class
            by_cik[row["cik"]] = {**dict(row), "tickers": tickers[row["cik"]], "reasons": []}
    ranked = sorted(by_cik.values(), key=lambda company: company["market_cap"], reverse=True)
    for number in range(1, level + 1):
        rule = settings["tiers"].get(number)
        if not rule:
            continue
        if "top_market_cap" in rule:
            members = ranked[:int(rule["top_market_cap"])]
        else:
            anywhere = set(rule.get("industries") or [])
            technology = set(rule.get("technology_industries") or [])
            members = [company for company in ranked
                       if company["market_cap"] >= float(rule.get("min_market_cap", 0))
                       and (company["industry"] in anywhere
                            or (company["industry"] in technology and company["sector"] == "Technology"))]
        for company in members:
            company["reasons"].append(f"tier {number}")
    for consumer, request in settings["requests"].items():
        for cik in request.get("ciks") or []:
            company = by_cik.get(int(cik))
            if company is not None:
                company["reasons"].append(f"request {consumer}")
    return {cik: company for cik, company in by_cik.items() if company["reasons"]}


def unresolved_requests(db, settings: dict | None = None) -> dict[str, list[int]]:
    """Requested CIKs with no listed company in the security master (delisted, renamed, typo)."""
    settings = settings or load()
    listed = {row["cik"] for row in _listed(db)}
    return {consumer: missing for consumer, request in settings["requests"].items()
            if (missing := [int(cik) for cik in request.get("ciks") or [] if int(cik) not in listed])}


def coverage_slug(cik: int) -> str:
    return f"cik{int(cik)}"


def _record_periods(db) -> None:
    """Open a collection period for each company now collected and close it when coverage ends
    (company_coverage_periods, migration 61), so consumers can tell "no news" from "not collected"."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO company_coverage_periods(company_id,started_at,basis)
           SELECT c.id, ?, 'recorded' FROM companies c
           WHERE (c.origin='watchlist' OR c.covered=1)
             AND NOT EXISTS (SELECT 1 FROM company_coverage_periods p WHERE p.company_id=c.id AND p.ended_at IS NULL)""",
        (now,))
    db.execute(
        """UPDATE company_coverage_periods SET ended_at=?
           WHERE ended_at IS NULL AND company_id IN (SELECT id FROM companies WHERE origin='coverage' AND covered=0)""",
        (now,))


def sync(level: int | None = None) -> dict:
    """Write the plan into ``companies``; watchlist rows win for companies they already hold."""
    level = tier() if level is None else level
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        planned = plan(db, level)
        rows = db.execute("SELECT slug,ticker,cik,origin,market FROM companies").fetchall()
        watchlist = [row for row in rows if row["origin"] == "watchlist" and row["market"] == "US"]
        watch_ciks = {int(row["cik"]) for row in watchlist if row["cik"]}
        # Watchlist companies get their CIK from the first SEC crawl, so also match them by ticker.
        watch_tickers = {row["ticker"] for row in watchlist if row["ticker"]}
        # A coverage row is found only by its CIK slug: a ticker can pass to another company
        # after a delisting, and that company needs a row of its own.
        coverage_slugs = {row["slug"] for row in rows if row["origin"] == "coverage"}
        added = updated = 0
        kept: list[str] = []
        for cik, company in planned.items():
            if cik in watch_ciks or any(ticker in watch_tickers for ticker in [company["ticker"], *company["tickers"]]):
                continue  # the watchlist row wins; a coverage row for the same company retires below
            slug = coverage_slug(cik)
            kept.append(slug)
            values = (company["name"], company["ticker"],
                      json.dumps(aliases(company["name"], company["ticker"]), ensure_ascii=False))
            if slug not in coverage_slugs:
                db.execute("""INSERT INTO companies(slug,name,name_zh,ticker,code,market,cik,aliases,origin,covered)
                              VALUES(?,?,'',?,'','US',?,?,'coverage',1)""",
                           (slug, values[0], values[1], str(cik).zfill(10), values[2]))
                added += 1
            else:
                db.execute("UPDATE companies SET name=?,ticker=?,aliases=?,covered=1 WHERE slug=?", (*values, slug))
                updated += 1
        retired = db.execute(
            f"""UPDATE companies SET covered=0 WHERE origin='coverage' AND covered=1
                AND slug NOT IN ({",".join("?" * len(kept)) or "''"})""", kept).rowcount
        _record_periods(db)
    company_match.invalidate_cache()
    return {"tier": level, "planned": len(planned), "added": added, "updated": updated, "retired": retired}
