# infohub — industry intelligence aggregator

A multi-source news ingestion service for **AI, robotics, and US/HK-listed technology
companies**. It crawls a set of sources on staggered schedules, deduplicates and clusters
what it finds into ranked topics and persistent event timelines, and publishes a daily
digest — with optional LLM curation on top.

It runs unattended. The design problem it actually solves is not *fetching* news; it is
**being sure that what ended up in the database is what you think it is**.

> 中文文档见 [README.zh-CN.md](./README.zh-CN.md)。

---

## Status at a glance

| Layer | State |
|---|---|
| Portal + crawler + daily digest | Working. This is what a running instance serves today. |
| v1 data foundations (raw evidence, versioned documents/events/analyses, durable jobs, publication ledger, authenticated read API, sync snapshots) | On `main`, behind **default-off** flags. Production has not been upgraded to enable them. |
| NLP quality evaluation (relevance, tone, impact) | Contracts, validators and review tooling exist. **No real labelled data and no model-quality claim yet**; checked-in datasets are synthetic fixtures that only exercise the tooling. |

[`docs/spec/IMPLEMENTATION_STATUS.md`](docs/spec/IMPLEMENTATION_STATUS.md) is the source of
truth for what is implemented versus planned; [`SPEC.md`](SPEC.md) is the target architecture.

## Quick start (development)

Requires Python 3.11 or 3.12 (CI tests both; the container uses 3.12). Python 3.9 — the
macOS system default — is too old.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python cli.py runtime-config   # show environment and data paths (no secrets)
.venv/bin/python cli.py init-db          # create the isolated dev database
.venv/bin/python cli.py serve            # portal on http://127.0.0.1:8000
```

Development defaults are deliberately inert: data lives under `.runtime/development-local/`,
and network tasks, the scheduler and model calls are all off. A one-off crawl must be asked
for explicitly:

```bash
INFOHUB_PROCESS_ROLE=maintenance INFOHUB_ALLOW_NETWORK_TASKS=true .venv/bin/python cli.py crawl
```

Without an LLM key the service runs as a pure aggregator; the LLM layer degrades away rather
than failing.

## What it serves

Three channels — **AI**, **robotics**, and **US/HK-listed tech equities** — each fed by its
own mix of sources.

| Page | What it shows |
|---|---|
| `/` | Today's ranked hot list, channel tabs, timeline grouped by date. The equity channel filters by company and event type (earnings, buybacks, M&A, ratings…). |
| `/hot` | Recent persistent events, counted by distinguishable publisher; click through to a report timeline. |
| `/topics`, `/topics/{slug}` | Topics across companies & models, technical directions, and content formats — each with statistics, recent focus, and a curated selection. |
| `/story/{id}` | A stable link per event: every source that covered it, the original items, and why they were merged. |
| `/daily` | The automatically generated daily digest. |
| `/search`, `/saved` | Title/summary full-text search; locally bookmarked items. |
| `/health` | Per-source last success, consecutive failures, and error text. |

Machine-readable endpoints: `/api/live` (web process answers), `/api/ready` (database identity
plus a fresh worker heartbeat of the same version), `/api/pipeline` (source, job, index and
digest freshness) and `/api/health` (combined snapshot; 503 when not ready). The `/api/v1/*`
read API (items, events, evidence, analyses, catalog, sync snapshots, changes) requires an API
key with the matching scope and is disabled unless its feature flag is set — see
[`docs/API_AUTH_FOUNDATION.md`](docs/API_AUTH_FOUNDATION.md) and
[`docs/API_READ_CONTRACT.md`](docs/API_READ_CONTRACT.md).

## Not missing things: three layers plus reconciliation

The equity channel is the one where a miss actually costs something, so coverage is layered
rather than trusted to any single feed:

| Layer | Sources | Cadence | Role |
|---|---|---|---|
| **First-party** | SEC EDGAR (8-K / 10-Q / Form 4), HKEX filings, company sites | 10–60 min | Official disclosure, highest trust |
| **Financial media** | CNBC, wire services, Chinese live feeds, one Google News feed per watched company | 10–30 min | Speed and breadth |
| **Daily reconciliation** | Per-company sweep | 06:30 daily | Compared against what is already stored; anything missing is backfilled and tagged as such |

Supporting mechanics: URL normalisation for de-duplication, per-source exponential backoff on
failure (flagged red on the health page), request staggering within a domain to avoid rate
limits, and a digest generated each morning (08:00 by default) for the previous day.
Scheduling is anchored to the exchange's local time, not hard-coded UTC offsets.

The three minute-level live feeds (Sina 7x24, CLS telegraph, WSCN live) only return their newest
page. When none of that page is stored yet, because the feed outran the poll interval or the
collector was down, older pages are read until they meet stored items: at most 20 more pages and
never past the source's last successful run (`app/crawler/catchup.py`). A steady poll still makes
one request. On the 2026-09-18 to 10-03 legacy record the collector was idle for about 40 of 356
hours, and an estimated ~800 Sina and ~1,000 CLS items were lost that way; the CLS newest page
also holds only 20 items, so busy ten-minute intervals lost items even without downtime.

## Architecture

```
app/
├── crawler/               ingestion: source registry, RSS/SEC/HKEX/Google News connectors, runner
├── ingest.py, documents.py, source_time.py
│                          immutable raw observations (content-addressed), document versions, source time rules
├── worker.py, jobs.py, publication.py, runtime_health.py
│                          durable jobs with leases, atomic publication ledger, web/worker health
├── catalog.py, sec_identity.py, company_match.py
│                          versioned entity catalog, SEC issuer/security semantics, bilingual alias matching
├── event_*.py, stories.py, ranking.py, topics.py, topic_*.py, curation_*.py
│                          stable events (matching, relations, revisions), topics, curation projections
├── analysis_*.py, tone_*.py, impact_*.py, ai/
│                          versioned NLP: pinned inputs, audited attempts, evidence-checked results
├── report_*.py            daily report snapshots, drafts, review and publication
├── api_*.py, web/         FastAPI portal and the authenticated /api/v1 read API
├── evaluation*.py, review_intake.py
│                          evaluation datasets, leakage checks, human review intake, metrics
└── database.py, db_admin.py, evidence_backup.py
                           SQLite (WAL) schema and migrations, verified backups and restores

cli.py                     every operational command (`init-db`, `serve`, `worker`, `db-*`, review tools…)
config/watchlist.yaml      companies tracked
config/topics.yaml         topic rules
evaluation/                dataset contracts, synthetic fixtures, baselines (private data is git-ignored)
```

Adding a company means editing `config/watchlist.yaml` and re-running `init-db`; adding a
source means one entry in `app/crawler/sources.py`. Neither requires touching the rest.

## Running it unattended

Production is a Windows host running **Docker Compose**: a one-shot `migrate` container
(`cli.py prepare-release`: verified backup, then migration), a read-only `infohub` web
container that never crawls, and a single `worker` container that owns scheduling, crawling
and model calls. SQLite, evidence blobs, backups and heartbeats persist under `./data`. The
web port is bound to `127.0.0.1` and reached privately through Tailscale Serve
([`docs/PRIVATE_HTTPS_INGRESS.md`](docs/PRIVATE_HTTPS_INGRESS.md)).

A companion deployment manager (separate repository) polls this repository and applies
fast-forward-only updates of `main`, passing the commit SHA into the container so
`/api/health` reports the exact running version.
Day-to-day commands, backups and restores are in [`docs/RUNBOOK.md`](docs/RUNBOOK.md). The first production
upgrade is planned in [`docs/CUTOVER_PLAN.md`](docs/CUTOVER_PLAN.md) from a full rehearsal on real data. A tested macOS
launchd alternative with the same roles lives in [`deploy/macos/`](deploy/macos/README.md); only one host may collect.

## Tests and CI

```bash
.venv/bin/python -m compileall -q app cli.py
.venv/bin/python -m unittest discover -s tests
```

The suite uses temporary databases and mocked LLM responses — no network calls, no paid API
usage — and runs in well under a minute. GitHub Actions runs it on Python 3.11 and 3.12 on
every push and pull request, and builds the container image.

## LLM curation (optional)

Configured through `.env` with any OpenAI-compatible endpoint
(`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`); only the worker receives the credentials.
When enabled it translates English sources into summaries, scores each item 0–100 for
importance, tags equity items by event type, and generates the daily digest. Remove the key
and the service falls back to pure aggregation.

## Evaluation and annotation

[`evaluation/README.md`](evaluation/README.md) describes the dataset format, leakage-safe
splits and review tooling. The project has one human annotator (its owner). Per
[SPEC §8.2](docs/spec/NLP_AND_EVALUATION.md), owner-labelled results count as *experimental*,
never as multi-reviewer gold, and model output never grades itself. The owner-labelling path
is being built; its state is tracked in the implementation status page.

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
- **No NLP output has a measured quality yet.** Tone and impact results stay review-only
  until real labelled data exists.

## Acknowledgements

Two source integrations follow the public implementations in
[RSSHub](https://github.com/DIYgod/RSSHub) and
[newsnow](https://github.com/ourongxing/newsnow).
