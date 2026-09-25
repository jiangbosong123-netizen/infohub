# infohub — industry intelligence aggregator

A multi-source news ingestion service for **AI, robotics, and US/HK-listed technology
companies**. It crawls a set of sources on staggered schedules, deduplicates and clusters
what it finds into ranked topics and persistent event timelines, and publishes a daily
digest — with optional LLM curation on top.

It runs unattended. The design problem it actually solves is not *fetching* news; it is
**being sure that what ended up in the database is what you think it is**.

> 中文文档见 [README.zh-CN.md](./README.zh-CN.md)。

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python cli.py init-db    # create schema, load the company watchlist
.venv/bin/python cli.py crawl      # first pass; also validates every source
.venv/bin/python cli.py serve      # web UI on http://127.0.0.1:8000
```

No API key required. Without one the service runs as a pure aggregator; the LLM layer
degrades away rather than failing.

## What it does

Three channels — **AI**, **robotics**, and **US/HK-listed tech equities** — each fed by
its own mix of sources.

| Page | What it shows |
|---|---|
| `/` | Today's ranked hot list, channel tabs, timeline grouped by date. The equity channel filters by company and event type (earnings, buybacks, M&A, ratings…). |
| `/hot` | Recent persistent events, counted by distinguishable publisher; click through to a report timeline. |
| `/topics` | Topics across companies & models, technical directions, and content formats — each with statistics, recent focus, and a curated selection. |
| `/story/{id}` | A stable link per event: every source that covered it, the original items, and why they were merged. |
| `/daily` | The automatically generated daily digest. |
| `/health` | Per-source last success, consecutive failures, and error text. |
| `/api/health` | The same, machine-readable: source failures, processing backlog, topic/event index lag, digest status, and the running build's commit SHA. |

## Architecture

```
app/
├── crawler/           ingestion layer
│   ├── sources.py       source registry — adding a source means adding a row here
│   ├── rss_source.py    generic RSS
│   ├── sec_source.py    SEC EDGAR (CIK resolved automatically)
│   ├── hkex_source.py   HKEX filings (stockId resolved automatically)
│   ├── googlenews.py    per-company feeds + the daily reconciliation pass
│   └── runner.py        scheduling, de-duplication on write, source health
├── ai/                LLM curation — summaries, scoring, digests; degrades without a key
├── company_match.py   strict bilingual alias matching
├── provenance.py      where every item came from
├── ranking.py         heat scoring and event clustering
├── topics.py          topic and event indexing
├── web/               FastAPI + Jinja2
└── database.py        SQLite schema (WAL)

cli.py                 init-db / crawl / reconcile / ai / report / reindex / serve
config/watchlist.yaml  companies tracked
config/topics.yaml     topic rules
```

Adding a company means editing `config/watchlist.yaml` and re-running `init-db`; adding a
source means one entry in `app/crawler/sources.py`. Neither requires touching the rest.

## Not missing things: three layers plus reconciliation

The equity channel is the one where a miss actually costs something, so coverage is
layered rather than trusted to any single feed:

| Layer | Sources | Cadence | Role |
|---|---|---|---|
| **First-party** | SEC EDGAR (8-K / 10-Q / Form 4), HKEX filings, company sites | 10–60 min | Official disclosure, highest trust |
| **Financial media** | CNBC, wire services, per-company news feeds | 20–240 min | Speed and breadth |
| **Daily reconciliation** | Per-company sweep | 06:30 daily | Compared against what is already stored; anything missing is backfilled and tagged as such |

Supporting mechanics: URL normalisation for de-duplication, per-source exponential backoff
on failure (flagged red on the health page), request staggering within a domain to avoid
rate limits, and a digest generated each morning for the previous day.

## Running it unattended

- **Docker Compose** with restart policies and a persistent volume; SQLite data lives on
  the host. `docker compose up -d --build` also rolls a new version.
- **launchd** on macOS with `KeepAlive` and `RunAtLoad` for an always-on local instance.
- **Tailscale** for private network access without exposing a port publicly.
- Missed scheduled jobs **self-recover after host sleep** via a misfire grace window.
- Scheduling is anchored to the exchange's local time, not hard-coded UTC offsets — US and
  UK daylight-saving transitions fall on different dates, and a hard-coded offset is wrong
  for about two weeks a year.

A companion deployment manager (separate repository) polls this repo's branch and applies
fast-forward-only updates, passing the running commit SHA into the container so `/health`
reports the exact deployed version.

## Tests and CI

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app cli.py
```

43 unit tests covering story clustering, crawler health, and past regressions. They use a
temporary database and mocked LLM responses — no network calls, no paid API usage.
GitHub Actions runs them on Python 3.11 and 3.12 on every push and pull request, and
builds the container image.

## LLM curation (optional)

Configured through `.env` with any OpenAI-compatible endpoint
(`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`). When enabled it translates English sources
into summaries, scores each item 0–100 for importance, tags equity items by event type,
and generates the daily digest. Remove the key and the service falls back to pure
aggregation.

## Known limitations

Stated because a coverage tool that does not tell you where it is blind is worse than no
tool at all.

- Two sources are not ingested: one renders purely client-side with a dead RSS feed, and
  one serves a malformed feed. Both would need a headless browser to fix properly.
- One newswire is excluded because its content is encrypted and sits behind a WAF.
- Per-ticker feeds from one large portal rate-limit too aggressively to rely on; replaced
  by per-company feeds on a 20-minute cycle plus the daily reconciliation pass.
- Anything behind a paywall, and scattered social posts without a paid API, are not
  covered.
- Event clustering uses conservative title, version, time and entity rules. It will still
  miss merges for heavily rewritten coverage.
- **Coverage of major corporate events has not been validated against an independent
  sample.** The three-layer design plus reconciliation is intended to make misses rare; it
  has not been measured, and is not a guarantee.

## Acknowledgements

Two source integrations follow the public implementations in
[RSSHub](https://github.com/DIYgod/RSSHub) and
[newsnow](https://github.com/ourongxing/newsnow).
