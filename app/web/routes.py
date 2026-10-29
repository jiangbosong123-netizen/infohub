from __future__ import annotations

"""FastAPI 页面路由：首页时间线 / 热点榜 / 日报 / 搜索 / 源状态。"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..ai.daily import EVENT_NAMES, render_report
from ..ai.text_quality import usable
from .. import config
from ..api_catalog import (
    CatalogNotFound,
    CatalogUnavailable,
    ENTITY_TYPES,
    EntityListResponse,
    EntityResponse,
    RestrictedCatalog,
    entity_etag,
    get_entity,
    list_entities,
)
from ..api_cursor import CursorEpochChanged, CursorError, CursorExpired, CursorFilterMismatch
from ..api_sources import (
    SOURCE_CHANNELS,
    SOURCE_TIERS,
    SourceCatalogUnavailable,
    SourceListResponse,
    SourceNotFound,
    SourceResponse,
    get_source,
    list_sources,
    source_etag,
)
from ..api_publishers import (
    PublisherCatalogUnavailable,
    PublisherListResponse,
    PublisherNotFound,
    PublisherResponse,
    RestrictedPublisher,
    get_publisher,
    list_publishers,
    publisher_etag,
)
from ..api_items import (
    DOCUMENT_KINDS,
    DuplicateItemAlias,
    ItemCatalogUnavailable,
    ItemListResponse,
    ItemNotFound,
    ItemResponse,
    ItemVersionHistoryResponse,
    RestrictedItem,
    get_item,
    item_etag,
    list_item_versions,
    list_items,
)
from ..api_events import (
    EVENT_TYPES,
    PUBLIC_STATES,
    EventCatalogUnavailable,
    EventListResponse,
    EventNotFound,
    EventResponse,
    event_etag,
    get_event,
    list_events,
)
from ..api_event_evidence import (
    EVIDENCE_ROLES,
    EventEvidenceResponse,
    EventEvidenceUnavailable,
    list_event_evidence,
)
from ..api_analyses import (
    AnalysisNotFound,
    AnalysisResponse,
    AnalysisUnavailable,
    analysis_etag,
    get_analysis,
)
from ..api_evidence import (
    EvidenceNotFound,
    EvidenceResponse,
    EvidenceRestricted,
    EvidenceUnavailable,
    ItemEvidenceResponse,
    evidence_etag,
    get_evidence,
    list_item_evidence,
)
from ..api_topics import (
    RestrictedTopic,
    TOPIC_GROUPS,
    TopicListResponse,
    TopicResponse,
    TopicStatisticsAdmissionError,
    TopicStatisticsNotFound,
    TopicStatisticsUnavailable,
    get_topic,
    list_topics,
    topic_etag,
)
from ..api_sync import (
    RESOURCES as SYNC_RESOURCES,
    SnapshotConflict,
    SnapshotCreate,
    SnapshotExpired,
    SnapshotLimitReached,
    SnapshotNotFound,
    SnapshotNotReady,
    SnapshotPageResponse,
    SnapshotResponse,
    SnapshotUnavailable,
    create_snapshot,
    get_snapshot,
    read_snapshot_page,
)
from ..api_changes import ChangeFeedUnavailable, ChangesResponse, list_changes
from ..config import (
    APP_TZ,
    APP_VERSION,
    BASE_DIR,
    CURATED_FEED_ENABLED,
    CURATION_READ_ENABLED,
    CURATION_SEARCH_ENABLED,
    CURATION_HOT_ENABLED,
    REPORT_READ_ENABLED,
    TOPIC_READ_ENABLED,
    ENVIRONMENT,
    ENVIRONMENT_ID,
    DURABLE_JOBS_ENABLED,
    PIPELINE_JOB_STALE_SECONDS,
    PROCESS_ROLE,
    SCHEDULER_ENABLED,
)
from .. import database as database_module
from ..database import get_db, sqlite_wal_reset_safe
from .. import offsite_backup
from ..curation_projection import display_curation, published_curation
from ..curation_query import portal_curation_sql
from ..curation_search_query import search_curated, search_index_usable
from ..curation_hot_query import curated_top_clusters, hot_metrics_usable, story_member_filters
from ..report_query import published_calendar_dates, published_calendar_report
from ..provenance import publisher, display_title
from ..crawler.article_text import excerpts as article_excerpts
from ..runtime_health import read_worker_heartbeat
from ..hot_pairs import same_event_groups
from ..self_check import FAILED_JOB_WINDOW, recorded as recorded_self_check
from ..source_activity import SourceActivity, source_activity
from ..timeutil import format_utc, parse_utc
from ..topics import GROUPS
from ..topic_portal_projection import (
    published_portal_topic,
    published_portal_topics,
)
from .v1_auth import v1_auth_guard
from .transport_security import private_https_headers
from .v1_errors import v1_error

# The interactive docs load scripts from a CDN; the schema itself stays at /openapi.json.
app = FastAPI(title="Infohub", docs_url=None, redoc_url=None)
app.middleware("http")(v1_auth_guard)
app.middleware("http")(private_https_headers)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))
# Static URLs carry the release, so browsers and the installed app fetch new CSS after an upgrade.
templates.env.globals["app_version"] = APP_VERSION

# Deep pages cost a scan of every earlier row; nobody reads past page 1,000.
MAX_PAGE = 1000
CHANNEL_TABS = [("all", "全部"), ("ai", "AI"), ("robot", "机器人"), ("stock", "股市")]
STARTED_AT = datetime.now(timezone.utc)

_ENTITY_LIST_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "q", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 200}},
        {"name": "type", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(ENTITY_TYPES)}},
        {"name": "identifier_namespace", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 64}},
        {"name": "identifier_value", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 256}},
        {"name": "exchange", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 32}},
    ],
}
_ENTITY_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "version_id", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1, "maxLength": 128}},
        {"name": "as_of", "in": "query", "required": False,
         "schema": {"type": "string", "format": "date-time"},
         "description": "Logical history cutoff; mutually exclusive with version_id."},
        {"name": "knowledge_checkpoint_id", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1, "maxLength": 128},
         "description": "Reserved for P19; currently returns 422 unsupported_history."},
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_TOPIC_LIST_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "group", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(TOPIC_GROUPS)}},
    ],
}
_TOPIC_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_SOURCE_LIST_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "q", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 200}},
        {"name": "channel", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(SOURCE_CHANNELS)}},
        {"name": "tier", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(SOURCE_TIERS)}},
    ],
}
_SOURCE_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_PUBLISHER_LIST_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "q", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 200}},
    ],
}
_PUBLISHER_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:catalog"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_ITEM_LIST_OPENAPI = {
    "x-required-scopes": ["read:items"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "q", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 200}},
        {"name": "kind", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(DOCUMENT_KINDS)}},
        {"name": "language", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 32}},
        {"name": "source_id", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 128}},
        {"name": "publisher_id", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 128}},
    ],
}
_ITEM_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:items"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_ITEM_VERSION_LIST_OPENAPI = {
    "x-required-scopes": ["read:items"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
    ],
}
_ITEM_EVIDENCE_LIST_OPENAPI = {
    "x-required-scopes": ["read:items", "read:evidence"],
    "parameters": [
        {"name": "version_id", "in": "query", "required": True,
         "schema": {"type": "string", "minLength": 1, "maxLength": 128}},
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
    ],
}
_EVIDENCE_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:evidence"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_EVENT_LIST_OPENAPI = {
    "x-required-scopes": ["read:events"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "q", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": 200}},
        {"name": "type", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(EVENT_TYPES)}},
        {"name": "state", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(PUBLIC_STATES)}},
        {"name": "entity_id", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1, "maxLength": 128}},
        {"name": "topic_id", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1, "maxLength": 128}},
    ],
}
_EVENT_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:events"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}
_EVENT_EVIDENCE_LIST_OPENAPI = {
    "x-required-scopes": ["read:evidence"],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "cursor", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1}},
        {"name": "role", "in": "query", "required": False,
         "schema": {"type": "string", "enum": sorted(EVIDENCE_ROLES)}},
        {"name": "fact_id", "in": "query", "required": False,
         "schema": {"type": "string", "minLength": 1, "maxLength": 128}},
    ],
}
_ANALYSIS_DETAIL_OPENAPI = {
    "x-required-scopes": ["read:analyses"],
    "parameters": [
        {"name": "If-None-Match", "in": "header", "required": False,
         "schema": {"type": "string", "maxLength": 512}},
    ],
}


# A story several sources covered qualifies an item for the selected feed only if the item is not
# weak itself: 32 copies of a game page once formed a two-publisher story of 5-point items.
CORROBORATED_MIN_SCORE = 30


def _selected_clause(alias: str = "i", score_expr: str | None = None) -> str:
    """High-signal entries: strong AI score, official source, or corroborated event.

    Routine official filings (``extra.routine``, e.g. HKEX next-day disclosure returns) do not
    qualify by being official; they stay in the full feed, company filters and search.
    """
    score = score_expr or f"{alias}.score"
    return f"""(COALESCE({score}, 0) >= 70
        OR ({alias}.official=1 AND CASE WHEN json_valid({alias}.extra)
                                        THEN json_extract({alias}.extra,'$.routine') END IS NULL)
        OR (COALESCE({score}, {CORROBORATED_MIN_SCORE}) >= {CORROBORATED_MIN_SCORE} AND EXISTS (
        SELECT 1 FROM story_items selected_si
        JOIN stories selected_st ON selected_st.id=selected_si.story_id
        WHERE selected_si.item_id={alias}.id AND selected_st.redirect_to IS NULL
          AND selected_st.source_count>=2)))"""


def _fmt_dt(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ)


def _relative(iso: str | None) -> str:
    if not iso:
        return "从未抓取"
    delta = datetime.now(timezone.utc) - _fmt_dt(iso)
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "刚刚"
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 48:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def _effective_interval(row) -> int:
    """The interval the crawler really uses: a quiet Nasdaq feed is asked every 2-4 hours (#267)."""
    from ..crawler.runner import quiet_interval
    keys = row.keys()
    if "quiet_runs" in keys:
        quiet = row["quiet_runs"] or 0
    else:  # a full sources row: read it from the state JSON
        try:
            quiet = int(json.loads(row["state"] or "{}").get("quiet_runs") or 0) if "state" in keys else 0
        except (TypeError, ValueError, AttributeError):
            quiet = 0
    return quiet_interval(row["key"], row["interval_minutes"], quiet)


PIPELINE_ISSUE_LABELS = {
    "source_failures_or_staleness": "有信息源失败或延迟",
    "sources_silent": "有信息源长时间没有新内容",
    "durable_job_failures": "后台任务失败",
    "durable_job_backlog_stale": "后台任务积压",
}


def _source_status(row, now: datetime | None = None,
                   activity: SourceActivity | None = None) -> str:
    status = "bad" if row["fail_count"] else ("ok" if row["last_success_at"] else "never")
    if row["last_error"] and not row["fail_count"]:
        status = "partial"
    now = now or datetime.now(timezone.utc)
    # On 10-18 the 60-minute interval marked 131 quiet feeds (asked every 4 hours) stale.
    if (status == "ok" and now - _fmt_dt(row["last_success_at"])
            > timedelta(minutes=max(15, _effective_interval(row) * 3))):
        status = "stale"
    if status == "ok" and activity is not None and activity.silent(now):
        status = "silent"  # fetched fine, but nothing new for far longer than usual
    return status


COMPANY_FEEDS_SILENT_SHARE = 0.2


def _system_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    now_utc = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with get_db() as db:
        # Separate counts so each reads a small index (the queues are partial indexes) instead of
        # one aggregate scanning every item on every health request.
        item = db.execute("""SELECT (SELECT COUNT(*) FROM items) AS total,
            (SELECT COUNT(*) FROM items WHERE score IS NULL) AS pending_score,
            (SELECT COUNT(*) FROM items WHERE tmt IS NULL) AS pending_tmt,
            (SELECT COUNT(*) FROM items WHERE tmt=0) AS hidden,
            (SELECT MAX(fetched_at) FROM items) AS last_item_at""").fetchone()
        derived_pending = db.execute("SELECT COUNT(*) FROM derived_dirty").fetchone()[0]
        reports = db.execute(
            "SELECT COUNT(*) AS total, MAX(date) AS latest FROM daily_reports").fetchone()
        source_rows = db.execute("""SELECT id,key,fail_count,last_success_at,last_error,interval_minutes,
            COALESCE(json_extract(CASE WHEN json_valid(state) THEN state END,'$.quiet_runs'),0) AS quiet_runs
            FROM sources WHERE enabled=1""").fetchall()
        activity = source_activity(db, [row["id"] for row in source_rows])
        last_fetch = db.execute("SELECT MAX(ran_at) FROM fetch_log").fetchone()[0]
        job_states = {
            state: 0
            for state in (
                "pending", "running", "succeeded", "retry_wait", "blocked",
                "dead_letter", "cancelled",
            )
        }
        for row in db.execute("SELECT state,COUNT(*) AS n FROM jobs GROUP BY state"):
            job_states[row["state"]] = row["n"]
        # Failed jobs stay in the table for good; like the self-check, only the last 24 hours make
        # the pipeline need attention. Counting every one ever kept the status yellow after 10-27.
        failed_recently = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('dead_letter','blocked') AND updated_at>=?",
            (format_utc(now - FAILED_JOB_WINDOW),),
        ).fetchone()[0]
        oldest_ready = db.execute(
            """SELECT MIN(next_attempt_at) FROM jobs
               WHERE state IN ('pending','retry_wait') AND next_attempt_at<=?""",
            (now.isoformat(timespec="microseconds").replace("+00:00", "Z"),),
        ).fetchone()[0]
        expired_running = db.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE state='running' AND lease_expires_at<=?""",
            (now_utc,),
        ).fetchone()[0]
        # Which kinds are running and waiting, so a stuck queue can be read remotely.
        running_jobs = [dict(row) for row in db.execute(
            """SELECT j.kind,j.priority,j.attempt_count AS attempt,a.started_at FROM jobs j
               LEFT JOIN job_attempts a ON a.lease_token=j.lease_token
               WHERE j.state='running' ORDER BY a.started_at""")]
        retrying = [dict(row) for row in db.execute(
            """SELECT kind,attempt_count AS attempt,max_attempts,next_attempt_at,error_code FROM jobs
               WHERE state='retry_wait' ORDER BY next_attempt_at LIMIT 20""")]
        ready_by_kind = {row["kind"]: {"count": row["n"], "oldest_at": row["oldest"]} for row in db.execute(
            """SELECT kind,COUNT(*) AS n,MIN(next_attempt_at) AS oldest FROM jobs
               WHERE state IN ('pending','retry_wait') AND next_attempt_at<=?
               GROUP BY kind ORDER BY oldest""", (now_utc,))}
        dataset_row = db.execute(
            """SELECT dataset_id,current_epoch,owner_environment_id
               FROM dataset_state WHERE singleton=1"""
        ).fetchone()
        change_high_water = db.execute(
            """SELECT COALESCE(MAX(seq),0) FROM change_log
               WHERE dataset_id=? AND epoch=?""",
            (dataset_row["dataset_id"], dataset_row["current_epoch"]),
        ).fetchone()[0]
        checkpoint = db.execute(
            """SELECT id,high_water,observed_at,clock_status
               FROM knowledge_checkpoints WHERE dataset_id=? AND epoch=?
               ORDER BY observed_at DESC,id DESC LIMIT 1""",
            (dataset_row["dataset_id"], dataset_row["current_epoch"]),
        ).fetchone()
        ingest_states = {
            state: 0
            for state in ("queued", "running", "succeeded", "partial", "failed", "skipped")
        }
        for row in db.execute("SELECT status,COUNT(*) AS n FROM ingest_runs GROUP BY status"):
            ingest_states[row["status"]] = row["n"]
        ingest_evidence = db.execute(
            """SELECT
                   (SELECT COUNT(*) FROM raw_records) AS records,
                   (SELECT COUNT(*) FROM raw_observations) AS observations,
                   (SELECT MAX(finished_at) FROM ingest_runs) AS last_finished_at"""
        ).fetchone()
        source_time_states = {}
        for row in db.execute(
            "SELECT status,COUNT(*) AS n FROM source_time_values GROUP BY status"
        ):
            source_time_states[row["status"]] = row["n"]
    source_states = [_source_status(row, now, activity.get(row["id"])) for row in source_rows]
    # One company's own feed going quiet is normal: most companies have no news most days (10-21:
    # SanDisk's feed, two days without an entry, made the pipeline "degraded"). Those feeds count
    # as silent only when many go quiet at once, which would mean the feed itself changed.
    from ..crawler.runner import QUIET_BACKOFF_PREFIXES
    company_feed = [row["key"].startswith(QUIET_BACKOFF_PREFIXES) for row in source_rows]
    company_silent = sum(state == "silent" and own for state, own in zip(source_states, company_feed))
    company_silence_counts = company_silent > COMPANY_FEEDS_SILENT_SHARE * max(1, sum(company_feed))
    counted = [state for state, own in zip(source_states, company_feed)
               if not (own and state == "silent" and not company_silence_counts)]
    issues = sum(state != "ok" for state in counted)
    silent = sum(state == "silent" for state in counted)
    oldest_ready_age = (
        max(0, int((now - _fmt_dt(oldest_ready)).total_seconds()))
        if oldest_ready else None
    )
    # A job waiting behind a running job of its own kind (the next AI round, the next crawl) is
    # queued, not stuck. The worker renews a running job's lease while the process lives, so a hung
    # job does not expire here; the self-check reports one running over two hours (stuck_jobs).
    running_kinds = {job["kind"] for job in running_jobs}
    oldest_blocked = min((value["oldest_at"] for kind, value in ready_by_kind.items()
                          if kind not in running_kinds), default=None)
    job_delayed = (oldest_blocked is not None
                   and (now - _fmt_dt(oldest_blocked)).total_seconds() > PIPELINE_JOB_STALE_SECONDS)
    job_issues = failed_recently + expired_running
    dataset_owner_matches = dataset_row["owner_environment_id"] == ENVIRONMENT_ID
    worker = read_worker_heartbeat(expected_version=APP_VERSION)
    worker_required = DURABLE_JOBS_ENABLED
    readiness_issues = []
    if not dataset_owner_matches:
        readiness_issues.append("dataset_owner_mismatch")
    if worker_required and not worker.healthy:
        readiness_issues.append(f"worker_{worker.status}")
    pipeline_issues = []
    if issues > silent:
        pipeline_issues.append("source_failures_or_staleness")
    if silent:
        pipeline_issues.append("sources_silent")
    if job_issues:
        pipeline_issues.append("durable_job_failures")
    if job_delayed:
        pipeline_issues.append("durable_job_backlog_stale")
    if worker_required and not worker.healthy:
        pipeline_issues.append(f"worker_{worker.status}")
    return {
        "status": "degraded" if readiness_issues or pipeline_issues else "ok",
        "version": APP_VERSION,
        "runtime": {
            "environment": ENVIRONMENT,
            "environment_id": ENVIRONMENT_ID,
            "process_role": PROCESS_ROLE,
            "scheduler_enabled": SCHEDULER_ENABLED,
            "durable_jobs_enabled": DURABLE_JOBS_ENABLED,
            "curated_feed_enabled": CURATED_FEED_ENABLED,
            "api_catalog_enabled": config.API_CATALOG_ENABLED,
            "api_items_enabled": config.API_ITEMS_ENABLED,
            "api_events_enabled": config.API_EVENTS_ENABLED,
            "api_analyses_enabled": config.API_ANALYSES_ENABLED,
            "api_evidence_enabled": config.API_EVIDENCE_ENABLED,
            "api_signals_enabled": config.API_SIGNALS_ENABLED,
            "api_sync_enabled": config.API_SYNC_ENABLED,
            "topic_read_enabled": TOPIC_READ_ENABLED,
            "sqlite_version": sqlite3.sqlite_version,
            "sqlite_wal_reset_safe": sqlite_wal_reset_safe(),
            "external_heartbeat_configured": bool(config.EXTERNAL_HEARTBEAT_URL),
        },
        "readiness": {
            "status": "not_ready" if readiness_issues else "ready",
            "ready": not readiness_issues,
            "issues": readiness_issues,
            "worker_required": worker_required,
        },
        "pipeline": {
            "status": "degraded" if pipeline_issues else "ok",
            "issues": pipeline_issues,
        },
        "worker": worker.to_dict(),
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": max(0, int((now - STARTED_AT).total_seconds())),
        "items": {
            "total": item["total"], "pending_score": item["pending_score"],
            "pending_tmt": item["pending_tmt"], "hidden": item["hidden"],
            "derived_pending": derived_pending, "last_item_at": item["last_item_at"],
            "last_item_relative": _relative(item["last_item_at"]),
        },
        "sources": {
            "enabled": len(source_rows), "issues": issues, "silent": silent,
            "last_run_at": last_fetch,
        },
        "reports": {"total": reports["total"], "latest": reports["latest"]},
        "jobs": {
            "enabled": DURABLE_JOBS_ENABLED,
            "states": job_states,
            "failed_last_24h": failed_recently,
            "oldest_ready_at": oldest_ready,
            "oldest_ready_age_seconds": oldest_ready_age,
            "expired_running": expired_running,
            "running": running_jobs,
            "ready_by_kind": ready_by_kind,
            "retrying": retrying,
        },
        "ingest": {
            "states": ingest_states,
            "raw_records": ingest_evidence["records"],
            "observations": ingest_evidence["observations"],
            "last_finished_at": ingest_evidence["last_finished_at"],
            "source_time_states": source_time_states,
        },
        "dataset": {
            "dataset_id": dataset_row["dataset_id"],
            "epoch": dataset_row["current_epoch"],
            "high_water": change_high_water,
            "owner_environment_id": dataset_row["owner_environment_id"],
            "owner_matches_environment": dataset_owner_matches,
            "latest_checkpoint": dict(checkpoint) if checkpoint else None,
        },
        "checked_at": now.isoformat(),
    }


def _date_label(d: datetime) -> str:
    today = datetime.now(APP_TZ).date()
    if d.date() == today:
        return "今天"
    if d.date() == today - timedelta(days=1):
        return "昨天"
    weekdays = "一二三四五六日"
    return f"{d.month}月{d.day}日（周{weekdays[d.weekday()]}）"


def _decorate(rows) -> list[dict]:
    """把 DB 行加工成视图对象：时间、公司标签、所属热点簇等。"""
    ids = [r["id"] for r in rows]
    publications: dict[int, dict[str, dict]] = {}
    cluster_map: dict[int, dict] = {}
    topic_map = {}
    if ids:
        marks = ",".join("?" * len(ids))
        with get_db() as db:
            if CURATION_READ_ENABLED:
                publications = published_curation(db, ids)
            for m in db.execute(
                f"""SELECT cm.item_id, cl.id AS cluster_id, cl.source_count, cl.item_count
                    FROM story_items cm JOIN stories cl ON cl.id = cm.story_id
                    WHERE cm.item_id IN ({marks})""", ids):
                cluster_map[m["item_id"]] = dict(m)
            for t in db.execute(f"""SELECT it.item_id,t.slug,t.name FROM item_topics it
                                JOIN topics t ON t.slug=it.topic_slug WHERE t.enabled=1
                                AND it.item_id IN ({marks}) ORDER BY t.position""",ids):
                topic_map.setdefault(t['item_id'],[]).append(dict(t))
    with get_db() as db:
        company_rows = {r["slug"]: dict(r) for r in
                        db.execute("SELECT slug, name, name_zh, ticker FROM companies")}

    keys = rows[0].keys() if rows else []
    out = []
    for r in rows:
        r = display_curation(dict(r), publications.get(r["id"]))
        slugs = json.loads(r["companies"] or "[]")
        dt = _fmt_dt(r["published_at"])
        cl = cluster_map.get(r["id"])
        title_zh = (r["title_zh"] or "") if "title_zh" in keys else ""
        title_zh = "" if title_zh == "-" else title_zh
        out.append(dict(
            id=r["id"], url=r["url"],
            title=title_zh or r["title"],
            title_orig=r["title"],
            translated=bool(title_zh and title_zh != "-"),
            summary=r["summary"],
            score=(r["score"] if (r["score"] is not None and r["score"] >= 0) else None),
            official=bool(r["official"]),
            via=r["via"], channel=r["channel"],
            source_name=publisher(dict(r))[1], crawl_source=r["source_name"],
            topics=topic_map.get(r["id"],[]), event_type=r["event_type"] or "",
            event_label=EVENT_NAMES.get(r["event_type"] or "", ""),
            reason=(r["reason"] or "") if "reason" in keys else "",
            ai_cat=(r["ai_cat"] or "") if "ai_cat" in keys else "",
            curation_needs_review=r["curation_needs_review"],
            hms=dt.strftime("%H:%M"), date_key=dt.date().isoformat(),
            companies=[dict(slug=s, label=(company_rows.get(s) or {}).get("name_zh")
                            or (company_rows.get(s) or {}).get("name") or s) for s in slugs],
            cluster_id=cl["cluster_id"] if cl else None,
            extra_sources=max(0,cl["source_count"] - 1) if cl else 0,
            story_count=cl["item_count"] if cl else 0,
        ))
    return out


def _first_per_story(cte: str, base: str, rank_score: str) -> str:
    """Each story's best eligible item, newest first, paged by LIMIT ? OFFSET ?.

    This is exactly ROW_NUMBER() OVER (PARTITION BY COALESCE(story_id,'item:'||id) ORDER BY
    published_at DESC, official DESC, COALESCE(score,-1) DESC, id DESC)=1, but an item is kept
    unless an eligible member of its own story ranks higher. The feed can then stream in
    published order and stop at the page instead of ranking every eligible item first.
    ``base`` must return each item at most once; items without a story stay singletons
    (story ids are never 'item:<id>'). CROSS JOIN pins the check to start from the story's
    members through idx_story_items_story.
    """
    return (cte + ", " if cte else "WITH ") + f"""eligible AS NOT MATERIALIZED ({base})
        SELECT eligible.*, 1 AS story_rank FROM eligible
        WHERE NOT EXISTS (
            SELECT 1 FROM story_items peer CROSS JOIN eligible better
            WHERE peer.story_id=eligible.story_id AND better.id=peer.item_id
              AND (better.published_at>eligible.published_at
                OR (better.published_at=eligible.published_at
                  AND (better.official>eligible.official
                    OR (better.official=eligible.official
                      AND (COALESCE(better.{rank_score},-1)>COALESCE(eligible.{rank_score},-1)
                        OR (COALESCE(better.{rank_score},-1)=COALESCE(eligible.{rank_score},-1)
                          AND better.id>eligible.id)))))))
        ORDER BY eligible.published_at DESC,eligible.id DESC LIMIT ? OFFSET ?"""


def _query_items(channel: str = "all", company: str = "", event: str = "", cat: str = "",
                 mode: str = "selected", limit: int = 60, offset: int = 0):
    """Selected is a deduplicated event feed; all preserves every visible report."""
    cte, curation_join, visible, score_expr, category_expr = portal_curation_sql(
        CURATION_READ_ENABLED, streamed=True)
    where = f" WHERE {visible}"
    params: list = []
    if mode == "selected" and CURATED_FEED_ENABLED:
        where += " AND " + _selected_clause(score_expr=score_expr)
    if channel and channel != "all":
        where += " AND i.channel=?"
        params.append(channel)
    if cat:
        where += f" AND {category_expr}=?"
        params.append(cat)
    if company:
        # A set lookup: a correlated EXISTS ran once per candidate row (up to 0.5 s for a rare company).
        where += (" AND i.id IN (SELECT ic.item_id FROM item_companies ic JOIN companies c "
                  "ON c.id=ic.company_id WHERE c.slug=?)")
        params.append(company)
    if event:
        where += " AND i.event_type=?"
        params.append(event)

    base = f"""SELECT i.*, s.name AS source_name, si.story_id
              {', cv.score AS curation_rank_score' if CURATION_READ_ENABLED else ''}
              FROM items i JOIN sources s ON s.id=i.source_id
              LEFT JOIN story_items si ON si.item_id=i.id""" + curation_join + where
    if mode == "selected":
        sql = _first_per_story(
            cte, base, "curation_rank_score" if CURATION_READ_ENABLED else "score")
    else:
        sql = cte + base + " ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return rows


def _top_clusters(limit: int = 10, channel: str = "all", topic: str = "", days: int = 2) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cte, curation_join, visible, _, _ = portal_curation_sql(CURATION_READ_ENABLED, streamed=True)
    filters, filter_params = story_member_filters(channel, topic, curation_join, visible)
    if CURATION_READ_ENABLED and not filters:
        # A channel or topic filter already requires a visible member.
        filters.append(f"""EXISTS(SELECT 1 FROM story_items si
                           CROSS JOIN items i ON i.id=si.item_id {curation_join}
                           WHERE si.story_id=st.id AND {visible})""")
    fetched = min(limit * 2, 100)  # room for entries folded into a better-ranked one
    with get_db() as db:
        db.execute("BEGIN")  # Read projection readiness and rows from one snapshot.
        use_curated = CURATION_HOT_ENABLED and hot_metrics_usable(db)
        if use_curated:
            rows = curated_top_clusters(
                db, limit=fetched, channel=channel, topic=topic, cutoff=cutoff,
            )
        else:
            rows = db.execute(
                cte + f"""SELECT st.*,st.last_at AS updated_at FROM stories st
                   WHERE st.redirect_to IS NULL AND st.item_count>0 AND st.last_at>=?
                   {''.join(' AND ' + check for check in filters)}
                   ORDER BY (st.source_count>=2) DESC,st.heat DESC,st.id LIMIT ?""",
                (cutoff, *filter_params, fetched)).fetchall()
        names = {r["slug"]: dict(r) for r in db.execute("SELECT slug,name,name_zh FROM companies")}
        groups = same_event_groups(db, [r["id"] for r in rows])
    # Stories the model judged to report one event (app/hot_pairs.py) take one place, under the
    # better-ranked one; the others are listed with it.
    kept, related = [], {}
    for r in rows:
        first = groups.get(r["id"], r["id"])
        if first != r["id"] and first in related:
            related[first].append(dict(id=r["id"], title=r["title"], item_count=r["item_count"]))
            continue
        related[r["id"]] = []
        kept.append(r)
    out = []
    for rank,r in enumerate(kept[:limit],1):
        value = dict(r)
        value.update(rank=rank,heat=int(r['heat']*100),metrics_pending=CURATION_HOT_ENABLED and not use_curated,
                     related=related[r["id"]],
                     companies=[dict(slug=s,label=(names.get(s) or {}).get('name_zh')
                     or (names.get(s) or {}).get('name') or s) for s in json.loads(r['company_slugs'])])
        out.append(value)
    return out


# The home page's side column: the last 24 hours at a glance, cached briefly so it adds nothing to
# most page loads. Keyed by database so tests and restores never see another database's numbers.
OVERVIEW_TTL_SECONDS = 60
_overview_cache: dict[str, tuple[float, dict]] = {}


def _home_overview() -> dict:
    key = str(database_module.DB_PATH)
    cached = _overview_cache.get(key)
    if cached and time.monotonic() - cached[0] < OVERVIEW_TTL_SECONDS:
        return cached[1]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_db() as db:
        total, relevant = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(tmt IS NULL OR tmt!=0), 0) FROM items WHERE fetched_at>=?",
            (cutoff,)).fetchone()
        sources, failing = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(fail_count>0), 0) FROM sources WHERE enabled=1").fetchone()
        names = {r["slug"]: r["name_zh"] or r["name"] for r in db.execute("SELECT slug,name,name_zh FROM companies")}
        companies = [dict(slug=slug, label=names.get(slug, slug), count=count) for slug, count in db.execute(
            """SELECT j.value, COUNT(*) FROM items i, json_each(i.companies) j
               WHERE i.fetched_at>=? AND (i.tmt IS NULL OR i.tmt!=0) AND json_valid(i.companies)
               GROUP BY j.value ORDER BY COUNT(*) DESC, j.value LIMIT 6""", (cutoff,))]
    value = dict(total=total, relevant=relevant, sources=sources, failing=failing, companies=companies)
    _overview_cache[key] = (time.monotonic(), value)
    return value


def _fold_same_events(items: list[dict]) -> list[dict]:
    """Cards whose stories the model judged one event (app/hot_pairs.py) show once, as the newest
    of them, with the others listed under it. The judgements already exist for the hot stories,
    which are most of the selected feed's first page; this costs no model call."""
    stories = list(dict.fromkeys(it["cluster_id"] for it in items if it.get("cluster_id")))
    if len(stories) < 2:
        return items
    with get_db() as db:
        groups = same_event_groups(db, stories)
    kept, first_card = [], {}
    for it in items:
        root = groups.get(it.get("cluster_id"), it.get("cluster_id"))
        if root is not None and root in first_card:
            first_card[root].setdefault("same_event", []).append(dict(id=it["cluster_id"], title=it["title"]))
            continue
        if root is not None:
            first_card[root] = it
        kept.append(it)
    return kept


@app.get("/", response_class=HTMLResponse)
def index(request: Request, channel: str = "all", company: str = "", event: str = "",
          cat: str = "", mode: str = "selected", page: int = Query(1, le=MAX_PAGE)):
    page = max(1, page)
    mode = mode if mode in ("selected", "all") else "selected"
    page_size = 30 if mode == "selected" else 60
    rows = _query_items(channel=channel, company=company, event=event, cat=cat, mode=mode,
                        limit=page_size + 1, offset=(page - 1) * page_size)
    has_next = len(rows) > page_size
    rows = rows[:page_size]
    items = _fold_same_events(_decorate(rows))
    days: list[dict] = []
    for it in items:
        if days and days[-1]["key"] == it["date_key"]:
            days[-1]["rows"].append(it)
        else:
            dt = _fmt_dt(
                next(r["published_at"] for r in rows if r["id"] == it["id"]))
            days.append(dict(key=it["date_key"], label=_date_label(dt), rows=[it]))

    companies, events = [], []
    if channel == "stock":
        with get_db() as db:
            companies = db.execute(
                "SELECT slug, name, name_zh, ticker, market FROM companies"
                " WHERE origin='watchlist' OR covered=1 ORDER BY market, name"
            ).fetchall()
        events = sorted(EVENT_NAMES.items())

    with get_db() as db:
        last_fetch = db.execute("SELECT MAX(fetched_at) AS m FROM items").fetchone()["m"]

    return templates.TemplateResponse(request, "index.html", dict(
        days=days, tabs=CHANNEL_TABS, channel=channel, company=company, event=event,
        mode=mode, cat=cat,
        companies=companies, events=events, clusters=_top_clusters(8, channel),
        page=page, has_next=has_next,
        last_update=_relative(last_fetch), overview=_home_overview(),
        fresh_since=last_fetch if page == 1 else None,
    ))


# Installable app (PWA): "Add to Home Screen" on a phone, "Add to Dock" or Install on a Mac. The
# service worker must be served from the root to control every page; it only adds an offline page
# for navigations and never caches news, so nothing shown can be stale.
WEB_MANIFEST = {
    "name": "Infohub",
    "short_name": "Infohub",
    "description": "科技与市场情报中枢",
    "lang": "zh-CN",
    "start_url": "/?mode=selected",
    "scope": "/",
    "display": "standalone",
    "background_color": "#070b12",
    "theme_color": "#070b12",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
        {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}
SERVICE_WORKER = """const CACHE = 'infohub-shell-%s';
const SHELL = ['/static/offline.html', '/static/icons/icon.svg'];
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => caches.match('/static/offline.html')));
});
"""


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    return JSONResponse(WEB_MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return Response(SERVICE_WORKER % APP_VERSION, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/hot", response_class=HTMLResponse)
def hot(request: Request):
    return templates.TemplateResponse(request, "hot.html", dict(clusters=_top_clusters(50)))


WEEKDAYS = "一二三四五六日"


def _report_lead(markdown: str, limit: int = 110) -> str:
    """The report's opening sentence (its first plain paragraph) for the list page."""
    import re
    for line in (markdown or "").splitlines():
        text = line.strip()
        # Headings, quotes, tables and list items; "**bold**" opening a paragraph is still text.
        if not text or text.startswith(("#", ">", "|")) or re.match(r"([-*+]\s|\d+[.、])", text):
            continue
        text = re.sub(r"[*_`]+", "", text)
        if not usable(text):
            continue  # a garbled overview is not shown (text_quality)
        return text if len(text) <= limit else text[:limit - 1] + "…"
    return ""


def _daily_rows(reports: dict) -> list[dict]:
    """Newest first, with a weekday, and a note where whole days have no report."""
    rows, previous = [], None
    for _, row in sorted(reports.items(), reverse=True):
        day = datetime.strptime(row["date"], "%Y-%m-%d").date()
        if previous is not None and (previous - day).days > 1:
            first, last = day + timedelta(days=1), previous - timedelta(days=1)
            span = first.strftime("%m-%d") + ("" if first == last else " 至 " + last.strftime("%m-%d"))
            rows.append({"gap": span})
        rows.append(dict(row, weekday="周" + WEEKDAYS[day.weekday()], created_rel=_relative(row["created_at"])))
        previous = day
    return rows


@app.get("/daily", response_class=HTMLResponse)
def daily_list(request: Request):
    with get_db() as db:
        legacy = db.execute("""SELECT date, created_at, substr(content, 1, 1500) AS head
                               FROM daily_reports ORDER BY date DESC""").fetchall()
        # "旧版" only means something next to versioned reports; alone these are simply the reports.
        label = "旧版日报" if REPORT_READ_ENABLED else "模型生成"
        reports = {row["date"]: {"date": row["date"], "created_at": row["created_at"],
                                 "source_label": label, "lead": _report_lead(row["head"])} for row in legacy}
        if REPORT_READ_ENABLED:
            for row in published_calendar_dates(db):
                reports[row["date"]] = {"date": row["date"], "created_at": row["created_at"], "lead": "",
                                        "source_label": f"版本 {row['version']} · "
                                                        + ("模型生成" if row["mode"] == "llm" else "结构化摘要")}
    return templates.TemplateResponse(request, "daily_list.html", dict(reports=_daily_rows(reports)))


@app.get("/daily/{date}", response_class=HTMLResponse)
def daily_detail(request: Request, date: str):
    report = None
    notice = ""
    with get_db() as db:
        if REPORT_READ_ENABLED:
            try:
                report = published_calendar_report(db, date)
            except (ValueError, KeyError, TypeError):
                notice = "新版日报校验失败，已显示旧版内容。"
        legacy = db.execute("SELECT * FROM daily_reports WHERE date=?", (date,)).fetchone()
    if report is None and legacy is None:
        return RedirectResponse("/daily", status_code=302)
    if report is None:
        content, source_label = legacy["content"], ("旧版日报" if REPORT_READ_ENABLED else "模型生成")
    else:
        content = report["content"]
        source_label = f"版本 {report['version']} · " + ("模型生成" if report["mode"] == "llm" else "结构化摘要")
    return templates.TemplateResponse(request, "daily_detail.html",
                                      dict(date=date, html=render_report(content),
                                           source_label=source_label, notice=notice, report=report))


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", page: int = Query(1, ge=1, le=200)):
    q = q.strip()
    if len(q) > 120:
        raise HTTPException(400, "搜索词最多 120 个字符")
    items = []
    page_size = 30
    notice = ""
    has_next = False
    if q:
        with get_db() as db:
            db.execute("BEGIN")  # Pin readiness and result reads to one SQLite snapshot.
            rows: list = []
            limit, offset = page_size + 1, (page - 1) * page_size
            use_curated = CURATION_SEARCH_ENABLED and search_index_usable(db)
            if use_curated:
                rows = search_curated(db, q, limit=limit, offset=offset)
            else:
                if CURATION_SEARCH_ENABLED:
                    notice = "搜索索引正在更新，当前显示旧搜索结果；新发布内容可能暂未包含。"
                if len(q) >= 3:  # trigram 分词最少 3 字符
                    try:
                        rows = db.execute(
                            """SELECT i.*, s.name AS source_name FROM items_fts
                               JOIN items i ON i.id = items_fts.rowid
                               JOIN sources s ON s.id = i.source_id
                               WHERE items_fts MATCH ? AND COALESCE(i.tmt, 1) != 0
                               ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?""",
                            (f'"{q.replace(chr(34), chr(34) * 2)}"', limit, offset)).fetchall()
                        if not rows and offset and db.execute(
                            """SELECT 1 FROM items_fts JOIN items i ON i.id=items_fts.rowid
                               WHERE items_fts MATCH ? AND COALESCE(i.tmt,1)!=0 LIMIT 1""",
                            (f'"{q.replace(chr(34), chr(34) * 2)}"',),
                        ).fetchone():
                            rows = ()  # This page is past the FTS result set.
                    except sqlite3.OperationalError:
                        rows = []
                if rows == []:
                    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    rows = db.execute(
                        """SELECT i.*, s.name AS source_name FROM items i
                           JOIN sources s ON s.id=i.source_id
                           WHERE COALESCE(i.tmt, 1) != 0 AND (i.title LIKE ? ESCAPE '\\'
                           OR i.title_zh LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\')
                           ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?""",
                        (like, like, like, limit, offset)).fetchall()
        has_next = len(rows) > page_size
        items = _decorate(rows[:page_size])
    return templates.TemplateResponse(request, "search.html", dict(
        q=q, items=items, page=page, has_next=has_next, notice=notice))


@app.get("/saved", response_class=HTMLResponse)
def saved(request: Request, ids: str = ""):
    parsed = []
    for value in ids.split(",")[:200]:
        try:
            item_id = int(value)
        except ValueError:
            continue
        if item_id > 0 and item_id not in parsed:
            parsed.append(item_id)
    rows = []
    if parsed:
        cte, curation_join, visible, _, _ = portal_curation_sql(CURATION_READ_ENABLED)
        marks = ",".join("?" * len(parsed))
        with get_db() as db:
            rows = db.execute(cte + f"""SELECT i.*,s.name AS source_name FROM items i
                JOIN sources s ON s.id=i.source_id
                {curation_join}
                WHERE i.id IN ({marks}) AND {visible}
                ORDER BY i.published_at DESC,i.id DESC""", parsed).fetchall()
    days = []
    for item in _decorate(rows):
        if not days or days[-1]["key"] != item["date_key"]:
            days.append(dict(key=item["date_key"],
                label=_date_label(datetime.fromisoformat(item["date_key"])), rows=[]))
        days[-1]["rows"].append(item)
    return templates.TemplateResponse(request, "saved.html", dict(days=days, has_ids=bool(ids)))


@app.get("/health", response_class=HTMLResponse)
def health(request: Request):
    with get_db() as db:
        rows = db.execute(
            """SELECT id, key, name, channel, tier, type, url, enabled, interval_minutes,
                      fail_count, last_success_at, last_run_at, last_error,
                      COALESCE(json_extract(CASE WHEN json_valid(state) THEN state END,'$.quiet_runs'),0) AS quiet_runs
               FROM sources WHERE enabled=1
               ORDER BY CASE tier WHEN 'official' THEN 0 WHEN 'media' THEN 1
               WHEN 'info' THEN 2 ELSE 3 END, channel, name""").fetchall()
        counts = {r["channel"]: r["n"] for r in db.execute(
            "SELECT channel, COUNT(*) AS n FROM items GROUP BY channel")}
        activity = source_activity(db, [r["id"] for r in rows])
    now = datetime.now(timezone.utc)
    sources = []
    for r in rows:
        seen = activity.get(r["id"])
        status = _source_status(r, now, seen)
        sources.append(dict(
            r, last_success_rel=_relative(r["last_success_at"]), status=status,
            effective_interval=_effective_interval(r),
            last_new_rel=_relative(seen.last_new_at.isoformat()) if seen else "暂无",
            quiet_limit_hours=(round(seen.quiet_limit.total_seconds() / 3600)
                               if seen and seen.quiet_limit else None),
        ))
    system = _system_snapshot()
    # One Nasdaq feed per covered company (356 on 10-19) would bury the rest of the table: they get
    # a summary, their problems stay visible, and the full list folds away.
    from ..crawler.runner import QUIET_BACKOFF_PREFIXES
    stock_feeds = [s for s in sources if s["key"].startswith(QUIET_BACKOFF_PREFIXES)]
    stock_summary = dict(total=len(stock_feeds),
                         ok=sum(s["status"] == "ok" for s in stock_feeds),
                         slowed=sum(s["status"] == "ok" and s["effective_interval"] != s["interval_minutes"]
                                    for s in stock_feeds),
                         problems=[s for s in stock_feeds if s["status"] != "ok"])
    return templates.TemplateResponse(request, "health.html", dict(
        disk=_disk_outlook(),
        sources=[s for s in sources if not s["key"].startswith(QUIET_BACKOFF_PREFIXES)],
        stock_feeds=stock_feeds, stock_summary=stock_summary,
        pipeline_labels=PIPELINE_ISSUE_LABELS, counts=counts, system=system,
        now=datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M")))


@app.get("/api/health")
def api_health():
    """Compatibility endpoint: readiness status plus the full pipeline snapshot."""
    try:
        snapshot = _system_snapshot()
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        snapshot = _unready_snapshot(exc)
    status_code = 200 if snapshot["readiness"]["ready"] else 503
    return JSONResponse(snapshot, status_code=status_code)


@app.get(
    "/api/v1/entities", response_model=EntityListResponse,
    openapi_extra=_ENTITY_LIST_OPENAPI,
)
def api_v1_entities(request: Request):
    """Return the reviewed current entity catalog with a key-bound live cursor."""
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {
        "limit", "cursor", "q", "type", "identifier_namespace",
        "identifier_value", "exchange",
    }
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    if str(limit) != raw_limit or not 1 <= limit <= 100:
        return v1_error(422, "invalid_parameter", request_id)
    query = request.query_params.get("q")
    query = query.strip() if query is not None else None
    if query == "":
        query = None
    if query is not None and len(query) > 200:
        return v1_error(422, "invalid_parameter", request_id)
    entity_type = request.query_params.get("type")
    if entity_type is not None and entity_type not in ENTITY_TYPES:
        return v1_error(422, "invalid_parameter", request_id)
    identifier_namespace = request.query_params.get("identifier_namespace")
    identifier_value = request.query_params.get("identifier_value")
    exchange = request.query_params.get("exchange")
    if identifier_namespace is not None:
        identifier_namespace = identifier_namespace.strip().lower()
    if identifier_value is not None:
        identifier_value = identifier_value.strip()
    if exchange is not None:
        exchange = exchange.strip().upper()
    if bool(identifier_namespace) != bool(identifier_value):
        return v1_error(422, "invalid_parameter", request_id)
    if any(value == "" for value in (identifier_namespace, identifier_value, exchange)
           if value is not None):
        return v1_error(422, "invalid_parameter", request_id)
    if ((identifier_namespace and len(identifier_namespace) > 64)
            or (identifier_value and len(identifier_value) > 256)
            or (exchange and len(exchange) > 32)):
        return v1_error(422, "invalid_parameter", request_id)
    if exchange and identifier_namespace != "exchange_ticker":
        return v1_error(422, "invalid_parameter", request_id)
    if identifier_namespace == "exchange_ticker" and not exchange:
        return v1_error(422, "invalid_parameter", request_id)
    if identifier_namespace in {"ticker", "exchange_ticker"}:
        identifier_value = identifier_value.upper()
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_entities(
                db, request.state.api_principal, request_id=request_id, limit=limit,
                cursor=request.query_params.get("cursor"), query=query,
                entity_type=entity_type, identifier_namespace=identifier_namespace,
                identifier_value=identifier_value, exchange=exchange,
            )
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (CatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/entities/{id}", response_model=EntityResponse,
    openapi_extra=_ENTITY_DETAIL_OPENAPI,
)
def api_v1_entity(id: str, request: Request, response: Response):
    """Resolve one current or logical-history entity view."""
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"version_id", "as_of", "knowledge_checkpoint_id"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    if not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    version_id = request.query_params.get("version_id")
    as_of = request.query_params.get("as_of")
    checkpoint_id = request.query_params.get("knowledge_checkpoint_id")
    if version_id is not None and (not version_id or len(version_id) > 128):
        return v1_error(422, "invalid_parameter", request_id)
    if version_id and as_of:
        return v1_error(422, "invalid_parameter", request_id)
    if checkpoint_id is not None:
        if not checkpoint_id or len(checkpoint_id) > 128 or version_id:
            return v1_error(422, "invalid_parameter", request_id)
        return v1_error(422, "unsupported_history", request_id)
    if as_of is not None:
        try:
            as_of = format_utc(parse_utc(as_of))
        except (ValueError, TypeError, OverflowError):
            return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_entity(
                db, request_id=request_id, entity_id=id,
                version_id=version_id, as_of=as_of,
            )
            etag = entity_etag(result, request.state.api_principal)
    except CatalogNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except RestrictedCatalog:
        return v1_error(403, "restricted_content", request_id)
    except (CatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag
            or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/topics", response_model=TopicListResponse,
    openapi_extra=_TOPIC_LIST_OPENAPI,
)
def api_v1_topics(request: Request):
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"limit", "cursor", "group"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    group = request.query_params.get("group")
    if str(limit) != raw_limit or not 1 <= limit <= 100 or (
        group is not None and group not in TOPIC_GROUPS
    ):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_topics(
                db, request.state.api_principal, request_id=request_id, limit=limit,
                cursor=request.query_params.get("cursor"), group=group,
            )
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (
        TopicStatisticsAdmissionError, TopicStatisticsUnavailable,
        sqlite3.Error, OSError, KeyError, TypeError, ValueError,
    ):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/topics/{id}", response_model=TopicResponse,
    openapi_extra=_TOPIC_DETAIL_OPENAPI,
)
def api_v1_topic(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_topic(db, request_id=request_id, topic_id=id)
            etag = topic_etag(result, request.state.api_principal)
    except TopicStatisticsNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except RestrictedTopic:
        return v1_error(403, "restricted_content", request_id)
    except (
        TopicStatisticsAdmissionError, TopicStatisticsUnavailable,
        sqlite3.Error, OSError, KeyError, TypeError, ValueError,
    ):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/sources", response_model=SourceListResponse,
    openapi_extra=_SOURCE_LIST_OPENAPI,
)
def api_v1_sources(request: Request):
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"limit", "cursor", "q", "channel", "tier"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    query = request.query_params.get("q")
    query = query.strip() if query is not None else None
    if query == "":
        query = None
    channel = request.query_params.get("channel")
    tier = request.query_params.get("tier")
    if (str(limit) != raw_limit or not 1 <= limit <= 100
            or (query is not None and len(query) > 200)
            or (channel is not None and channel not in SOURCE_CHANNELS)
            or (tier is not None and tier not in SOURCE_TIERS)):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_sources(
                db, request.state.api_principal, request_id=request_id,
                limit=limit, cursor=request.query_params.get("cursor"),
                query=query, channel=channel, tier=tier,
            )
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (SourceCatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/sources/{id}", response_model=SourceResponse,
    openapi_extra=_SOURCE_DETAIL_OPENAPI,
)
def api_v1_source(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_source(db, request_id=request_id, source_id=id)
            etag = source_etag(result, request.state.api_principal)
    except SourceNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except (SourceCatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/publishers", response_model=PublisherListResponse,
    openapi_extra=_PUBLISHER_LIST_OPENAPI,
)
def api_v1_publishers(request: Request):
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"limit", "cursor", "q"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    query = request.query_params.get("q")
    query = query.strip() if query is not None else None
    if query == "":
        query = None
    if (str(limit) != raw_limit or not 1 <= limit <= 100
            or (query is not None and len(query) > 200)):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_publishers(
                db, request.state.api_principal, request_id=request_id,
                limit=limit, cursor=request.query_params.get("cursor"), query=query,
            )
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (PublisherCatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/publishers/{id}", response_model=PublisherResponse,
    openapi_extra=_PUBLISHER_DETAIL_OPENAPI,
)
def api_v1_publisher(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_CATALOG_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_publisher(db, request_id=request_id, publisher_id=id)
            etag = publisher_etag(result, request.state.api_principal)
    except PublisherNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except RestrictedPublisher:
        return v1_error(403, "restricted_content", request_id)
    except (PublisherCatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/items", response_model=ItemListResponse,
    openapi_extra=_ITEM_LIST_OPENAPI,
)
def api_v1_items(request: Request):
    request_id = request.state.request_id
    if not config.API_ITEMS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {
        "limit", "cursor", "q", "kind", "language", "source_id", "publisher_id",
    }
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    query = request.query_params.get("q")
    query = query.strip() if query is not None else None
    if query == "":
        query = None
    kind = request.query_params.get("kind")
    language = request.query_params.get("language")
    source_id = request.query_params.get("source_id")
    publisher_id = request.query_params.get("publisher_id")
    if any(value == "" for value in (language, source_id, publisher_id) if value is not None):
        return v1_error(422, "invalid_parameter", request_id)
    if (str(limit) != raw_limit or not 1 <= limit <= 100
            or (query is not None and len(query) > 200)
            or (kind is not None and kind not in DOCUMENT_KINDS)
            or (language is not None and len(language) > 32)
            or (source_id is not None and len(source_id) > 128)
            or (publisher_id is not None and len(publisher_id) > 128)):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_items(
                db, request.state.api_principal, request_id=request_id,
                limit=limit, cursor=request.query_params.get("cursor"), query=query,
                kind=kind, language=language, source_id=source_id,
                publisher_id=publisher_id,
            )
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (
        ItemCatalogUnavailable, RestrictedItem, DuplicateItemAlias,
        sqlite3.Error, OSError, KeyError, TypeError, ValueError,
    ):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/items/{id}", response_model=ItemResponse,
    openapi_extra=_ITEM_DETAIL_OPENAPI,
)
def api_v1_item(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_ITEMS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 96:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_item(db, request_id=request_id, item_id=id)
            etag = item_etag(result, request.state.api_principal)
    except ItemNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except RestrictedItem:
        return v1_error(403, "restricted_content", request_id)
    except (DuplicateItemAlias, ItemCatalogUnavailable, sqlite3.Error, OSError,
            KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/items/{id}/versions", response_model=ItemVersionHistoryResponse,
    openapi_extra=_ITEM_VERSION_LIST_OPENAPI,
)
def api_v1_item_versions(id: str, request: Request):
    request_id = request.state.request_id
    if not config.API_ITEMS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"limit", "cursor"}
    if (not id or len(id) > 96 or set(request.query_params) - allowed
            or any(len(request.query_params.getlist(name)) != 1
                   for name in request.query_params)):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    if str(limit) != raw_limit or not 1 <= limit <= 100:
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_item_versions(
                db, request.state.api_principal, request_id=request_id,
                item_id=id, limit=limit, cursor=request.query_params.get("cursor"),
            )
    except ItemNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except RestrictedItem:
        return v1_error(403, "restricted_content", request_id)
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (DuplicateItemAlias, ItemCatalogUnavailable, sqlite3.Error, OSError,
            KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/items/{id}/evidence", response_model=ItemEvidenceResponse,
    openapi_extra=_ITEM_EVIDENCE_LIST_OPENAPI,
)
def api_v1_item_evidence(id: str, request: Request):
    request_id = request.state.request_id
    if not config.API_ITEMS_ENABLED or not config.API_EVIDENCE_ENABLED:
        return v1_error(503, "not_ready", request_id)
    principal = request.state.api_principal
    if not principal.allows("read:items"):
        return v1_error(403, "insufficient_scope", request_id)
    allowed = {"version_id", "limit", "cursor"}
    if (not id or len(id) > 96 or set(request.query_params) - allowed
            or any(len(request.query_params.getlist(name)) != 1
                   for name in request.query_params)):
        return v1_error(422, "invalid_parameter", request_id)
    version_id = request.query_params.get("version_id")
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    if (not version_id or len(version_id) > 128 or str(limit) != raw_limit
            or not 1 <= limit <= 100):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_item_evidence(
                db, principal, request_id=request_id, item_id=id,
                version_id=version_id, limit=limit,
                cursor=request.query_params.get("cursor"),
            )
    except EvidenceNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except EvidenceRestricted:
        return v1_error(403, "restricted_content", request_id)
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (EvidenceUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/evidence/{id}", response_model=EvidenceResponse,
    openapi_extra=_EVIDENCE_DETAIL_OPENAPI,
)
def api_v1_evidence(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_EVIDENCE_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_evidence(db, request_id=request_id, evidence_id=id)
            etag = evidence_etag(result, request.state.api_principal)
    except EvidenceNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except EvidenceRestricted:
        return v1_error(403, "restricted_content", request_id)
    except (EvidenceUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/events", response_model=EventListResponse,
    openapi_extra=_EVENT_LIST_OPENAPI,
)
def api_v1_events(request: Request):
    request_id = request.state.request_id
    if not config.API_EVENTS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"limit", "cursor", "q", "type", "state", "entity_id", "topic_id"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    query = request.query_params.get("q")
    query = query.strip() if query is not None else None
    if query == "":
        query = None
    event_type = request.query_params.get("type")
    public_state = request.query_params.get("state")
    entity_id = request.query_params.get("entity_id")
    topic_id = request.query_params.get("topic_id")
    if any(value == "" for value in (entity_id, topic_id) if value is not None):
        return v1_error(422, "invalid_parameter", request_id)
    if (
        str(limit) != raw_limit or not 1 <= limit <= 100
        or (query is not None and len(query) > 200)
        or (event_type is not None and event_type not in EVENT_TYPES)
        or (public_state is not None and public_state not in PUBLIC_STATES)
        or (entity_id is not None and len(entity_id) > 128)
        or (topic_id is not None and len(topic_id) > 128)
    ):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_events(
                db, request.state.api_principal, request_id=request_id,
                limit=limit, cursor=request.query_params.get("cursor"), query=query,
                event_type=event_type, public_state=public_state,
                entity_id=entity_id, topic_id=topic_id,
            )
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (EventCatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/events/{id}", response_model=EventResponse,
    openapi_extra=_EVENT_DETAIL_OPENAPI,
)
def api_v1_event(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_EVENTS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_event(db, request_id=request_id, event_id=id)
            etag = event_etag(result, request.state.api_principal)
    except EventNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except (EventCatalogUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.get(
    "/api/v1/events/{id}/evidence", response_model=EventEvidenceResponse,
    openapi_extra=_EVENT_EVIDENCE_LIST_OPENAPI,
)
def api_v1_event_evidence(id: str, request: Request):
    request_id = request.state.request_id
    if not config.API_EVENTS_ENABLED:
        return v1_error(503, "not_ready", request_id)

    allowed = {"limit", "cursor", "role", "fact_id"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    role = request.query_params.get("role")
    fact_id = request.query_params.get("fact_id")
    if (
        not id or len(id) > 128 or str(limit) != raw_limit or not 1 <= limit <= 100
        or (role is not None and role not in EVIDENCE_ROLES)
        or (fact_id is not None and (not fact_id or len(fact_id) > 128))
    ):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            return list_event_evidence(
                db, request.state.api_principal, request_id=request_id, event_id=id,
                limit=limit, cursor=request.query_params.get("cursor"), role=role,
                fact_id=fact_id,
            )
    except EventNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (EventCatalogUnavailable, EventEvidenceUnavailable, sqlite3.Error, OSError,
            KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/analyses/{id}", response_model=AnalysisResponse,
    openapi_extra=_ANALYSIS_DETAIL_OPENAPI,
)
def api_v1_analysis(id: str, request: Request, response: Response):
    request_id = request.state.request_id
    if not config.API_ANALYSES_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    conditional = [
        value for key, value in request.scope["headers"]
        if key.lower() == b"if-none-match"
    ]
    if len(conditional) > 1 or (conditional and len(conditional[0]) > 512):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = get_analysis(db, request_id=request_id, analysis_id=id)
            etag = analysis_etag(result, request.state.api_principal)
    except AnalysisNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except (AnalysisUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)
    if conditional:
        try:
            validators = [value.strip() for value in conditional[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            return v1_error(422, "invalid_parameter", request_id)
        if "*" in validators or any(
            validator == etag or (validator.startswith("W/") and validator[2:] == etag)
            for validator in validators
        ):
            return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@app.post(
    "/api/v1/sync/snapshots", status_code=202, response_model=SnapshotResponse,
    openapi_extra={"x-required-scopes": ["read:sync"]},
)
async def api_v1_create_snapshot(request: Request):
    request_id = request.state.request_id
    if not config.API_SYNC_ENABLED or not config.DURABLE_JOBS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params:
        return v1_error(422, "invalid_parameter", request_id)
    keys = [value for key, value in request.scope["headers"]
            if key.lower() == b"idempotency-key"]
    if len(keys) != 1:
        return v1_error(422, "invalid_parameter", request_id)
    try:
        idempotency_key = keys[0].decode("ascii")
        raw = b""
        async for chunk in request.stream():  # stop reading at the limit, not after the whole body
            raw += chunk
            if len(raw) > 4096:
                raise ValueError("body too large")
        body = SnapshotCreate.model_validate_json(raw)
        with get_db() as db:
            result = create_snapshot(
                db, principal=request.state.api_principal, request_id=request_id,
                idempotency_key=idempotency_key, request=body,
            )
    except (ValidationError, UnicodeDecodeError, ValueError):
        return v1_error(422, "invalid_parameter", request_id)
    except PermissionError:
        return v1_error(403, "insufficient_scope", request_id)
    except SnapshotConflict:
        return v1_error(409, "idempotency_conflict", request_id)
    except SnapshotLimitReached:
        return v1_error(429, "snapshot_limit_reached", request_id, retry_after=300)
    except SnapshotNotReady:
        return v1_error(503, "not_ready", request_id)
    except (SnapshotUnavailable, sqlite3.Error, OSError, KeyError, TypeError):
        return v1_error(503, "not_ready", request_id)
    return result


@app.get(
    "/api/v1/sync/snapshots/{id}", response_model=SnapshotResponse,
    openapi_extra={"x-required-scopes": ["read:sync"]},
)
def api_v1_get_snapshot(id: str, request: Request):
    request_id = request.state.request_id
    if not config.API_SYNC_ENABLED or not config.DURABLE_JOBS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if request.query_params or not id or len(id) > 128:
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            return get_snapshot(
                db, principal=request.state.api_principal,
                request_id=request_id, snapshot_id=id,
            )
    except SnapshotNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except SnapshotConflict:
        return v1_error(409, "epoch_changed", request_id)
    except (SnapshotUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/sync/snapshots/{id}/pages", response_model=SnapshotPageResponse,
    openapi_extra={"x-required-scopes": ["read:sync"]},
)
def api_v1_read_snapshot_page(id: str, request: Request):
    request_id = request.state.request_id
    if not config.API_SYNC_ENABLED or not config.DURABLE_JOBS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"resource", "limit", "cursor"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    resource = request.query_params.get("resource")
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    if (not id or len(id) > 128 or resource not in SYNC_RESOURCES
            or str(limit) != raw_limit or not 1 <= limit <= 100):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            return read_snapshot_page(
                db, principal=request.state.api_principal, request_id=request_id,
                snapshot_id=id, resource=resource, limit=limit,
                cursor=request.query_params.get("cursor"),
            )
    except SnapshotNotFound:
        return v1_error(404, "resource_not_found", request_id)
    except SnapshotExpired:
        return v1_error(410, "snapshot_expired", request_id)
    except SnapshotNotReady:
        return v1_error(409, "snapshot_not_ready", request_id)
    except SnapshotConflict:
        return v1_error(409, "epoch_changed", request_id)
    except PermissionError:
        return v1_error(403, "insufficient_scope", request_id)
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorFilterMismatch:
        return v1_error(400, "filter_mismatch", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (SnapshotUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


@app.get(
    "/api/v1/changes", response_model=ChangesResponse,
    openapi_extra={"x-required-scopes": ["read:sync"]},
)
def api_v1_changes(request: Request):
    request_id = request.state.request_id
    if not config.API_SYNC_ENABLED:
        return v1_error(503, "not_ready", request_id)
    allowed = {"cursor", "limit"}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        return v1_error(422, "invalid_parameter", request_id)
    cursor = request.query_params.get("cursor")
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    if (not cursor or len(cursor) > 4096 or str(limit) != raw_limit
            or not 1 <= limit <= 100):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        with get_db() as db:
            return list_changes(
                db, principal=request.state.api_principal, request_id=request_id,
                cursor=cursor, limit=limit,
            )
    except PermissionError:
        return v1_error(403, "insufficient_scope", request_id)
    except CursorExpired:
        return v1_error(410, "cursor_expired", request_id)
    except CursorEpochChanged:
        return v1_error(409, "epoch_changed", request_id)
    except CursorError:
        return v1_error(400, "invalid_cursor", request_id)
    except (ChangeFeedUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return v1_error(503, "not_ready", request_id)


def _unready_snapshot(exc: Exception) -> dict:
    return {
        "status": "unavailable",
        "version": APP_VERSION,
        "runtime": {
            "environment": ENVIRONMENT,
            "environment_id": ENVIRONMENT_ID,
            "process_role": PROCESS_ROLE,
            "scheduler_enabled": SCHEDULER_ENABLED,
            "durable_jobs_enabled": DURABLE_JOBS_ENABLED,
            "curated_feed_enabled": CURATED_FEED_ENABLED,
            "api_catalog_enabled": config.API_CATALOG_ENABLED,
            "api_items_enabled": config.API_ITEMS_ENABLED,
            "api_events_enabled": config.API_EVENTS_ENABLED,
            "api_analyses_enabled": config.API_ANALYSES_ENABLED,
            "api_evidence_enabled": config.API_EVIDENCE_ENABLED,
            "api_signals_enabled": config.API_SIGNALS_ENABLED,
            "api_sync_enabled": config.API_SYNC_ENABLED,
        },
        "readiness": {
            "status": "not_ready",
            "ready": False,
            "issues": ["database_unavailable"],
            "worker_required": DURABLE_JOBS_ENABLED,
        },
        "pipeline": {"status": "unavailable", "issues": ["database_unavailable"]},
        "error": type(exc).__name__,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/live")
def api_live():
    """Process liveness only; it deliberately does not touch SQLite or the worker."""
    now = datetime.now(timezone.utc)
    return {
        "status": "live",
        "version": APP_VERSION,
        "process_role": PROCESS_ROLE,
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": max(0, int((now - STARTED_AT).total_seconds())),
        "checked_at": now.isoformat(),
    }


@app.get("/api/ready")
def api_ready():
    """Release readiness: current database identity and current worker version."""
    try:
        snapshot = _system_snapshot()
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        snapshot = _unready_snapshot(exc)
    payload = {
        "status": snapshot["readiness"]["status"],
        "ready": snapshot["readiness"]["ready"],
        "issues": snapshot["readiness"]["issues"],
        "version": snapshot["version"],
        "worker": snapshot.get("worker"),
        "checked_at": snapshot["checked_at"],
    }
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


# Off-site copy for the owner's Mac (D25): newest nightly bundle, its tar stream, and the receipt
# the Mac posts after verifying every hash. Operational, so not part of the published contract.
_NEWS_PARAMS = {"scope", "cik", "theme", "since", "as_of", "min_score", "limit", "after", "include_routine"}


@app.get("/api/v1/signals/news", include_in_schema=False)
def api_v1_signals_news(request: Request):
    """News by company (CIK list) or by industry/macro theme, point in time (app/news_signals.py)."""
    from ..news_signals import NewsQueryError, query_news
    request_id = request.state.request_id
    if not config.API_SIGNALS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    if not request.state.api_principal.allows("read:signals"):
        return v1_error(403, "insufficient_scope", request_id)
    params = request.query_params
    if (set(params) - _NEWS_PARAMS
            or any(len(params.getlist(name)) != 1 for name in params if name not in ("cik", "theme"))
            or "since" not in params):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        limit = int(params.get("limit", "200"))
        min_score = int(params["min_score"]) if "min_score" in params else None
        include_routine = {"true": True, "false": False}[params.get("include_routine", "false")]
    except (ValueError, KeyError):
        return v1_error(422, "invalid_parameter", request_id)
    cik_values = [value for raw in params.getlist("cik") for value in raw.split(",") if value]
    theme_values = [value for raw in params.getlist("theme") for value in raw.split(",") if value]
    scope = params.get("scope", "company" if cik_values else "")
    try:
        with get_db() as db:
            db.execute("BEGIN")  # one snapshot for the whole page
            result = query_news(
                db, scope=scope, ciks=cik_values, themes=theme_values, since=params["since"],
                as_of=params.get("as_of"), min_score=min_score, limit=limit, after=params.get("after"),
                include_routine=include_routine)
    except NewsQueryError:
        return v1_error(422, "invalid_parameter", request_id)
    except (sqlite3.Error, OSError):
        return v1_error(503, "not_ready", request_id)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/backups/latest", include_in_schema=False)
def api_v1_backup_latest(request: Request):
    request_id = request.state.request_id
    if not config.API_BACKUPS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    try:
        latest = offsite_backup.latest_bundle()
    except (OSError, ValueError):
        return v1_error(503, "temporarily_unavailable", request_id)
    if latest is None:
        return v1_error(404, "resource_not_found", request_id)
    return JSONResponse(latest)


@app.get("/api/v1/backups/{name}/archive", include_in_schema=False)
def api_v1_backup_archive(name: str, request: Request):
    request_id = request.state.request_id
    if not config.API_BACKUPS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    try:
        bundle = offsite_backup.bundle_path(name)
    except offsite_backup.OffsiteError:
        return v1_error(404, "resource_not_found", request_id)
    return StreamingResponse(offsite_backup.tar_stream(bundle), media_type="application/x-tar",
                             headers={"Content-Disposition": f'attachment; filename="{bundle.name}.tar"'})


@app.post("/api/v1/backups/{name}/receipt", include_in_schema=False)
async def api_v1_backup_receipt(name: str, request: Request):
    request_id = request.state.request_id
    if not config.API_BACKUPS_ENABLED:
        return v1_error(503, "not_ready", request_id)
    try:
        body = await request.json()
    except ValueError:
        return v1_error(422, "invalid_parameter", request_id)
    if not isinstance(body, dict) or set(body) != {"database_sha256"} or not isinstance(body["database_sha256"], str):
        return v1_error(422, "invalid_parameter", request_id)
    try:
        receipt = offsite_backup.record_receipt(
            name, body["database_sha256"], request.state.api_principal.consumer_id)
    except offsite_backup.OffsiteError:
        return v1_error(409, "receipt_mismatch", request_id)
    return JSONResponse(receipt)


# The sidebar's mood face (app/mood.py), on every page: cached like the home overview.
MOOD_TTL_SECONDS = 60
_mood_cache: dict[str, tuple[float, dict | None]] = {}


def _mood_face() -> dict | None:
    key = str(database_module.DB_PATH)
    cached = _mood_cache.get(key)
    if cached and time.monotonic() - cached[0] < MOOD_TTL_SECONDS:
        return cached[1]
    from ..mood import summary
    try:
        with get_db() as db:
            result = summary(db)
    except sqlite3.Error:
        result = None  # an older schema or a busy database: the page renders without the face
    _mood_cache[key] = (time.monotonic(), result)
    return result


templates.env.globals["mood_face"] = _mood_face

# The external heartbeat that would page the owner is not configured (D25 left it to the owner), so a
# problem the self-check finds is also shown on top of every page: the portal is what they open.
SELF_CHECK_STALE_AFTER = timedelta(hours=3)


def _system_alerts() -> list[str]:
    result = recorded_self_check()
    if not result:
        return []
    alerts = [problem.get("detail") or problem.get("code") for problem in result.get("problems") or []
              if isinstance(problem, dict)]
    try:
        age = datetime.now(timezone.utc) - _fmt_dt(result["checked_at"])
    except (KeyError, TypeError, ValueError):
        return alerts
    if age > SELF_CHECK_STALE_AFTER:
        alerts.append(f"系统自检已 {int(age.total_seconds() // 3600)} 小时没有更新，后台可能停了")
    return alerts


templates.env.globals["system_alerts"] = _system_alerts


def _disk_outlook() -> dict | None:
    """Free disk space and roughly how long until the alert line, at the faster of two paces:
    - the database's typical daily growth, the median of the last 14 recorded days, so a one-off
      import (10-17: +1.1 GB) does not set it;
    - how fast the disk's free space fell over the last week, which counts every other program on
      the host too (10-27 to 10-29: 4.7 GB, while the database grew about 80 MB a day)."""
    result = recorded_self_check() or {}
    disk = result.get("disk")
    if not isinstance(disk, dict) or "free_mb" not in disk:
        return None
    with get_db() as db:
        rows = db.execute(
            "SELECT day, bytes, disk_free_mb FROM database_size_daily ORDER BY day DESC LIMIT 15").fetchall()[::-1]
    sizes = [row[1] for row in rows]
    growth = sorted(later - earlier for earlier, later in zip(sizes, sizes[1:]) if later >= earlier)
    per_day_mb = growth[len(growth) // 2] / (1024 * 1024) if growth else None
    readings = [(row[0], row[2]) for row in rows[-8:] if row[2] is not None]
    drop_mb = drop_days = None
    if len(readings) >= 2:
        (first_day, first), (last_day, last) = readings[0], readings[-1]
        span = (datetime.fromisoformat(last_day) - datetime.fromisoformat(first_day)).days
        if span > 0 and first > last:
            drop_mb, drop_days = (first - last) / span, span
    pace = max(filter(None, (per_day_mb, drop_mb)), default=None)
    days_left = None
    if pace and pace > 0:
        days_left = max(0, int((disk["free_mb"] - disk.get("alert_mb", 0)) / pace))
    return dict(disk, per_day_mb=round(per_day_mb, 1) if per_day_mb else None,
                drop_mb=round(drop_mb) if drop_mb else None, drop_days=drop_days, days_left=days_left)


@app.get("/fragment/mood", response_class=HTMLResponse, include_in_schema=False)
def mood_fragment(request: Request):
    """The face alone, so an open page can update it without a reload (every 5 minutes)."""
    response = templates.TemplateResponse(request, "_mood.html", {})
    response.headers["Cache-Control"] = "no-store"
    return response


FRESH_COUNT_CAP = 1000


@app.get("/api/fresh", include_in_schema=False)
def api_fresh(since: str = Query(..., min_length=10, max_length=40)):
    """How many relevant items arrived after ``since`` (the newest fetch time the page showed)."""
    try:
        datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "since must be an ISO 8601 time") from None
    with get_db() as db:
        count = db.execute(
            """SELECT COUNT(*) FROM (SELECT 1 FROM items WHERE fetched_at>? AND COALESCE(tmt,1)!=0 LIMIT ?)""",
            (since, FRESH_COUNT_CAP)).fetchone()[0]
    return JSONResponse({"new": count, "capped": count >= FRESH_COUNT_CAP}, headers={"Cache-Control": "no-store"})


# D31 tier evaluation (docs/RUNBOOK.md "覆盖范围"): items, model usage and database size per day.
COVERAGE_STATS_TTL_SECONDS = 300
_coverage_stats_cache: dict[str, tuple[float, dict]] = {}


@app.get("/api/coverage")
def api_coverage():
    key = str(database_module.DB_PATH)
    cached = _coverage_stats_cache.get(key)
    if cached and time.monotonic() - cached[0] < COVERAGE_STATS_TTL_SECONDS:
        return cached[1]
    from ..coverage_stats import summary
    try:
        result = summary()
    except (OSError, sqlite3.Error) as exc:
        return JSONResponse({"status": "unavailable", "error": type(exc).__name__}, status_code=503)
    _coverage_stats_cache[key] = (time.monotonic(), result)
    return result


@app.get("/api/pipeline")
def api_pipeline():
    """Operational freshness and backlog; degraded data does not make web unreadable."""
    try:
        snapshot = _system_snapshot()
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        snapshot = _unready_snapshot(exc)
        return JSONResponse(snapshot, status_code=503)
    return {
        "status": snapshot["pipeline"]["status"],
        "issues": snapshot["pipeline"]["issues"],
        "version": snapshot["version"],
        "worker": snapshot["worker"],
        "sources": snapshot["sources"],
        "jobs": snapshot["jobs"],
        "ingest": snapshot["ingest"],
        "items": snapshot["items"],
        "reports": snapshot["reports"],
        # Recorded by the worker after each crawl cycle; the same result the heartbeat reports.
        "self_check": recorded_self_check(),
        "checked_at": snapshot["checked_at"],
    }


@app.get("/about-heat", response_class=HTMLResponse)
def about_heat():
    return RedirectResponse("/hot", status_code=302)


def _curated_topic_stats(db):
    """Every topic's totals under curation read, checking each item's publications once.

    An item sits in about 1.8 topics, and reading its curation values per assignment repeated
    the same publication checks: 1.6 s for the rehearsal-size topic index, 0.74 s this way.
    The totals, selected counts and latest times equal the per-assignment form in _topic_stats,
    and the outer GROUP BY/ORDER BY is kept so topics with equal positions keep their order.
    """
    cte, _, _, _, _ = portal_curation_sql(True, streamed=True)
    selected = _selected_clause(score_expr="cv.score") if CURATED_FEED_ENABLED else "1=1"
    return [dict(r) for r in db.execute(cte + f""", topic_item_values AS MATERIALIZED (
            SELECT i.id, i.published_at, ({selected}) AS is_selected
            FROM (SELECT DISTINCT item_id FROM item_topics) ti
            CROSS JOIN items i ON i.id=ti.item_id
            JOIN curation_values cv ON cv.item_id=i.id
            WHERE cv.visible=1
        ), topic_counts AS MATERIALIZED (
            SELECT it.topic_slug, COUNT(*) AS total,
                   SUM(CASE WHEN v.is_selected THEN 1 ELSE 0 END) AS selected,
                   MAX(v.published_at) AS last_at
            FROM item_topics it JOIN topic_item_values v ON v.id=it.item_id
            GROUP BY it.topic_slug)
        SELECT t.*,COALESCE(c.total,0) AS total,COALESCE(c.selected,0) AS selected,
               c.last_at AS last_at
        FROM topics t LEFT JOIN topic_counts c ON c.topic_slug=t.slug
        WHERE t.enabled=1 GROUP BY t.slug ORDER BY t.position""")]


def _topic_stats(db, slug: str | None = None):
    """Per-topic totals; with ``slug``, only that topic's row (same values, fewer items)."""
    if TOPIC_READ_ENABLED:
        return published_portal_topics(db)
    if CURATION_READ_ENABLED and slug is None:
        return _curated_topic_stats(db)
    cte, _, _, score_expr, _ = portal_curation_sql(CURATION_READ_ENABLED, streamed=True)
    if CURATION_READ_ENABLED:
        # curation_values has one row per item; reading it per assignment keeps the planner
        # from materializing it for every item (it cannot sit flattened under a LEFT JOIN).
        score_expr = "(SELECT cv.score FROM curation_values cv WHERE cv.item_id=i.id)"
    selected = _selected_clause(score_expr=score_expr) if CURATED_FEED_ENABLED else "1=1"
    item_join = ("LEFT JOIN items i ON i.id=it.item_id AND "
                 "(SELECT cv.visible FROM curation_values cv WHERE cv.item_id=it.item_id)=1"
                 if CURATION_READ_ENABLED else
                 "LEFT JOIN items i ON i.id=it.item_id AND COALESCE(i.tmt,1)!=0")
    return [dict(r) for r in db.execute(cte + f"""SELECT t.*,COUNT(i.id) AS total,
        COALESCE(SUM(CASE WHEN i.id IS NOT NULL AND {selected} THEN 1 ELSE 0 END),0) AS selected,
        MAX(i.published_at) AS last_at FROM topics t
        LEFT JOIN item_topics it ON it.topic_slug=t.slug
        {item_join}
        WHERE t.enabled=1{' AND t.slug=?' if slug is not None else ''}
        GROUP BY t.slug ORDER BY t.position""", () if slug is None else (slug,))]


@app.get('/topics',response_class=HTMLResponse)
def topics_index(request: Request):
    try:
        with get_db() as db:
            db.execute("BEGIN")
            entries = _topic_stats(db)
    except (TopicStatisticsAdmissionError, TopicStatisticsUnavailable):
        raise HTTPException(503, '主题统计尚未通过发布审核')
    groups = [dict(key=key,name=name,description=desc,topics=[t for t in entries if t['group_key']==key])
              for key,name,desc in GROUPS]
    return templates.TemplateResponse(request,'topics.html',dict(groups=groups,total=len(entries)))


@app.get('/topics/{slug}',response_class=HTMLResponse)
def topic_detail(request: Request,slug: str,mode: str='selected',page: int=Query(1,ge=1,le=MAX_PAGE)):
    mode = mode if mode in ('all','selected') else 'selected'
    cte, curation_join, visible, score_expr, _ = portal_curation_sql(CURATION_READ_ENABLED, streamed=True)
    selected = " AND " + _selected_clause(score_expr=score_expr) if mode=='selected' and CURATED_FEED_ENABLED else ''
    try:
        with get_db() as db:
            db.execute("BEGIN")
            if TOPIC_READ_ENABLED:
                topic = published_portal_topic(db, slug)
                base = f"""SELECT i.*,s.name AS source_name,si.story_id
                    {', cv.score AS curation_rank_score' if CURATION_READ_ENABLED else ''}
                    FROM topic_statistics_state topic_state
                    JOIN topic_statistics_versions topic_statistic
                      ON topic_statistic.build_id=topic_state.current_build_id
                     AND topic_statistic.topic_version_id=?
                    JOIN topic_statistics_members topic_member
                      ON topic_member.statistics_id=topic_statistic.id
                     AND topic_member.member_type='document'
                    JOIN documents topic_document ON topic_document.id=topic_member.resource_id
                    JOIN document_versions topic_document_version
                      ON topic_document_version.id=topic_member.version_id
                     AND topic_document_version.document_id=topic_document.id
                    JOIN items i ON i.id=topic_document.legacy_item_id
                    JOIN sources s ON s.id=i.source_id
                    LEFT JOIN story_items si ON si.item_id=i.id
                    {curation_join}
                    WHERE topic_state.singleton=1 AND {visible} {selected}"""
                base_params = (topic["version_id"],)
            else:
                topic = next((t for t in _topic_stats(db, slug) if t['slug']==slug),None)
                if topic is None:
                    raise HTTPException(404,'主题不存在')
                # Newest items first, stopping at the page. Joining from the topic's assignments
                # instead ranked every member first: 0.9 s for the largest topic with curation
                # read. The unary + keeps the member list a filter, built once and checked before
                # an item's row is read, rather than the driver of the scan. An item has at most
                # one assignment per topic, so the rows are the same.
                base = f"""SELECT i.*,s.name AS source_name,si.story_id
                    {', cv.score AS curation_rank_score' if CURATION_READ_ENABLED else ''} FROM items i
                    JOIN sources s ON s.id=i.source_id
                    LEFT JOIN story_items si ON si.item_id=i.id
                    {curation_join}
                    WHERE +i.id IN (SELECT item_id FROM item_topics WHERE topic_slug=?)
                      AND {visible} {selected}"""
                base_params = (slug,)
            if mode == 'selected':
                sql = _first_per_story(
                    cte, base, 'curation_rank_score' if CURATION_READ_ENABLED else 'score')
            else:
                sql = cte + base + " ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?"
            rows = db.execute(sql,(*base_params,21,(page-1)*20)).fetchall()
    except TopicStatisticsNotFound:
        raise HTTPException(404,'主题不存在')
    except (TopicStatisticsAdmissionError, TopicStatisticsUnavailable):
        raise HTTPException(503,'主题统计尚未通过发布审核')
    days = []
    for item in _decorate(rows[:20]):
        if not days or days[-1]['key']!=item['date_key']:
            days.append(dict(key=item['date_key'],label=_date_label(datetime.fromisoformat(item['date_key'])),rows=[]))
        days[-1]['rows'].append(item)
    return templates.TemplateResponse(request,'topic_detail.html',dict(
        topic=topic,days=days,
        clusters=[] if TOPIC_READ_ENABLED else _top_clusters(5,topic=slug,days=14),
        page=page,mode=mode,
        has_next=len(rows)>20,last_update=_relative(topic['last_at']) if topic['last_at'] else '暂无收录'))


# The opening of an item's fetched article text (D27); the full text is never served.
STORY_EXCERPT_CHARS = 300


@app.get('/story/{story_id}',response_class=HTMLResponse)
def story_detail(request: Request,story_id: str,page: int=Query(1,ge=1,le=MAX_PAGE)):
    cte, curation_join, visible, _, _ = portal_curation_sql(CURATION_READ_ENABLED)
    with get_db() as db:
        story = db.execute('SELECT * FROM stories WHERE id=?',(story_id,)).fetchone()
        seen = set()
        while story and story['redirect_to']:
            if story['id'] in seen:
                raise HTTPException(500,'事件重定向异常')
            seen.add(story['id'])
            story = db.execute('SELECT * FROM stories WHERE id=?',(story['redirect_to'],)).fetchone()
        if not story or not story['item_count']:
            raise HTTPException(404,'事件不存在或暂无公开报道')
        if story['id'] != story_id:
            return RedirectResponse('/story/'+story['id'],status_code=302)
        rows = db.execute(cte + f"""SELECT i.*,s.name AS source_name,si.match_reason,si.match_score
            FROM story_items si JOIN items i ON i.id=si.item_id JOIN sources s ON s.id=i.source_id
            {curation_join}
            WHERE si.story_id=? AND {visible}
            ORDER BY i.published_at DESC,i.id DESC LIMIT 51 OFFSET ?""",(story_id,(page-1)*50)).fetchall()
        publications = published_curation(db, (r['id'] for r in rows[:50])) if CURATION_READ_ENABLED else {}
        article_openings = article_excerpts(db, (r['id'] for r in rows[:50]), STORY_EXCERPT_CHARS)
        source_rows = [dict(r) for r in db.execute(cte + f"""SELECT i.*,s.name AS source_name FROM story_items si
            JOIN items i ON i.id=si.item_id JOIN sources s ON s.id=i.source_id
            {curation_join}
            WHERE si.story_id=? AND {visible}""",(story_id,))]
        tags = db.execute(cte + f"""SELECT DISTINCT t.slug,t.name FROM story_items si
            JOIN item_topics it ON it.item_id=si.item_id JOIN topics t ON t.slug=it.topic_slug
            JOIN items i ON i.id=si.item_id {curation_join}
            WHERE si.story_id=? AND t.enabled=1 AND {visible}
            ORDER BY t.position""",(story_id,)).fetchall()
    if not source_rows:
        raise HTTPException(404,'暂无公开报道')
    sources = {identity:label for identity,label,known in (publisher(r) for r in source_rows) if known}
    unknown = sum(not publisher(r)[2] for r in source_rows)
    reports = []
    for row in rows[:50]:
        data = display_curation(dict(row), publications.get(row['id']))
        data.update(title_display=display_title(data),publisher=publisher(data)[1],
                    published_label=_fmt_dt(row['published_at']).strftime('%m月%d日 %H:%M'),
                    article_opening=article_openings.get(row['id']))
        reports.append(data)
    story_view = dict(story)
    if CURATION_READ_ENABLED:
        story_view['item_count'] = len(source_rows)
    return templates.TemplateResponse(request,'story.html',dict(story=story_view,reports=reports,
        sources=list(sources.values()),unknown=unknown,tags=tags,official_count=sum(r['official'] for r in source_rows),
        first_label=_fmt_dt(story['first_at']).strftime('%m月%d日 %H:%M'),
        last_label=_fmt_dt(story['last_at']).strftime('%m月%d日 %H:%M'),page=page,has_next=len(rows)>50))
