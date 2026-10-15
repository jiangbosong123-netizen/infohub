from __future__ import annotations

"""The platform's security master (D31): every US-listed company, keyed by SEC CIK.

Once a day:
- SEC's ticker/exchange file gives CIK, name, ticker and exchange.
- Nasdaq's public stock screener adds market cap, sector, industry and country, joined by ticker.
- SIC codes come from each covered company's SEC submissions, at most 100 a run at SEC's
  fair-access pace (large companies' files are several megabytes).

Coverage tiers (app/coverage.py) read this table; nothing else in it depends on any consumer's
list.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import config
from .crawler import http
from .crawler.sec_source import TICKERS_URL, _parse_associations
from .database import get_db
from .timeutil import format_utc

log = logging.getLogger(__name__)

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
LISTED_EXCHANGES = ("NYSE", "NASDAQ")
SIC_FETCH_PER_RUN = 100
# SEC lists over 10,000 tickers; a much shorter answer is a broken download, not delistings.
MIN_LISTINGS = 1000
SIC_REFRESH_AFTER = timedelta(days=180)
SEC_PAUSE_SECONDS = 0.15  # SEC asks for at most 10 requests a second


def _ticker(symbol: str) -> str:
    """Nasdaq writes share classes as BRK/B and preferreds with ^; SEC uses BRK-B."""
    return symbol.strip().upper().replace("/", "-").replace("^", "-")


def _market_cap(value) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# Nasdaq appends the security type to the company name ("(Delaware)Common Stock" has no space).
_SECURITY_TYPE = re.compile(
    r"(?:\s+|(?<=\)))(Class [A-Z]\s+)?((New York|NY) Registry Shares|Common Stock|Capital Stock|Ordinary Shares?|"
    r"Common Shares|American Deposit[ao]ry (Shares|Receipts)|Subordinate Voting Shares|Limited Voting Shares|"
    r"Units?)\b.*$", re.IGNORECASE)
# A relisting marker ("United Microelectronics Corporation (NEW)") and a state of incorporation.
_MARKERS = re.compile(r"\s*\((new|delaware)\)", re.IGNORECASE)


def display_name(raw: str) -> str:
    """'Teradyne Inc. Common Stock' -> 'Teradyne Inc.'"""
    return _MARKERS.sub("", _SECURITY_TYPE.sub("", raw.strip())).strip(" ,")


def _sec_headers() -> dict:
    return {"User-Agent": config.SEC_USER_AGENT, "Accept": "application/json"}


def fetch_listings() -> list[dict]:
    """SEC's associations joined with the Nasdaq screener, one row per ticker."""
    associations = _parse_associations(json.loads(http.fetch(TICKERS_URL, headers=_sec_headers()).text))
    screener = http.fetch(NASDAQ_SCREENER_URL, headers={"Accept": "application/json"}).json()
    rows = ((screener or {}).get("data") or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Nasdaq screener returned no rows")
    by_ticker = {_ticker(row.get("symbol") or ""): row for row in rows if isinstance(row, dict)}
    listings = {}
    for association in associations:
        ticker = association["ticker"]
        if ticker in listings or not 1 <= len(ticker) <= 16:
            continue  # SEC lists a company's primary ticker first
        quote = by_ticker.get(ticker, {})
        listings[ticker] = {
            "ticker": ticker, "cik": int(association["cik"]), "name": association["name"],
            "display_name": display_name(quote.get("name") or "") or None,
            "exchange": association["exchange"], "market_cap": _market_cap(quote.get("marketCap")),
            "sector": quote.get("sector") or None, "industry": quote.get("industry") or None,
            "country": quote.get("country") or None,
        }
    return list(listings.values())


def store_listings(listings: list[dict], now: datetime | None = None) -> dict:
    """Replace the current listings; a ticker missing from today's SEC file is removed."""
    stamp = format_utc(now or datetime.now(timezone.utc))
    if len(listings) < MIN_LISTINGS:
        raise RuntimeError(f"only {len(listings)} listings; refusing to replace the security master")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        before = {row[0] for row in db.execute("SELECT ticker FROM security_master")}
        for listing in listings:
            db.execute(
                """INSERT INTO security_master(ticker,cik,name,display_name,exchange,market_cap,sector,industry,
                                               country,first_seen_at,updated_at)
                   VALUES(:ticker,:cik,:name,:display_name,:exchange,:market_cap,:sector,:industry,:country,
                          :now,:now)
                   ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik,name=excluded.name,
                       display_name=excluded.display_name,
                       exchange=excluded.exchange,market_cap=excluded.market_cap,sector=excluded.sector,
                       industry=excluded.industry,country=excluded.country,updated_at=excluded.updated_at""",
                {**listing, "now": stamp})
        gone = before - {listing["ticker"] for listing in listings}
        db.executemany("DELETE FROM security_master WHERE ticker=?", [(ticker,) for ticker in gone])
    return {"listings": len(listings), "added": len({l["ticker"] for l in listings} - before),
            "removed": len(gone)}


def refresh_industry_codes(ciks, limit: int = SIC_FETCH_PER_RUN, now: datetime | None = None,
                           pause: float = SEC_PAUSE_SECONDS) -> dict:
    """Fetch SIC codes for ``ciks`` (the covered companies) that have none, or a stale one."""
    current = now or datetime.now(timezone.utc)
    wanted = sorted({int(cik) for cik in ciks})
    with get_db() as db:
        fresh = {row[0] for row in db.execute(
            "SELECT cik FROM security_industry_codes WHERE fetched_at>=?",
            (format_utc(current - SIC_REFRESH_AFTER),))}
    due = [cik for cik in wanted if cik not in fresh]
    ciks = due[:limit]
    fetched = failed = 0
    for cik in ciks:
        try:
            submission = http.fetch(SUBMISSIONS_URL.format(cik=cik), headers=_sec_headers(), retries=0).json()
        except Exception as exc:  # one company's failure must not stop the rest
            failed += 1
            log.warning("SIC lookup failed for CIK %s: %s", cik, type(exc).__name__)
            continue
        with get_db() as db:
            db.execute(
                """INSERT INTO security_industry_codes(cik,sic,sic_description,fetched_at) VALUES(?,?,?,?)
                   ON CONFLICT(cik) DO UPDATE SET sic=excluded.sic,sic_description=excluded.sic_description,
                       fetched_at=excluded.fetched_at""",
                (cik, submission.get("sic") or None, submission.get("sicDescription") or None,
                 format_utc(current)))
        fetched += 1
        time.sleep(pause)
    return {"sic_fetched": fetched, "sic_failed": failed, "sic_pending": len(due) - fetched}
