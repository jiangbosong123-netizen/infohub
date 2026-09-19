from __future__ import annotations

"""FastAPI 页面路由：首页时间线 / 热点榜 / 日报 / 搜索 / 源状态。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..ai.daily import EVENT_NAMES, render_markdown
from ..config import (
    APP_TZ,
    APP_VERSION,
    BASE_DIR,
    CURATED_FEED_ENABLED,
    CURATION_READ_ENABLED,
    ENVIRONMENT,
    ENVIRONMENT_ID,
    DURABLE_JOBS_ENABLED,
    PIPELINE_JOB_STALE_SECONDS,
    PROCESS_ROLE,
    SCHEDULER_ENABLED,
)
from ..database import get_db
from ..curation_projection import display_curation, published_curation
from ..curation_query import portal_curation_sql
from ..provenance import publisher, display_title
from ..runtime_health import read_worker_heartbeat
from ..topics import GROUPS

app = FastAPI(title="行业情报站")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))

CHANNEL_TABS = [("all", "全部"), ("ai", "AI"), ("robot", "机器人"), ("stock", "股市")]
STARTED_AT = datetime.now(timezone.utc)


def _selected_clause(alias: str = "i", score_expr: str | None = None) -> str:
    """High-signal entries: strong AI score, official source, or corroborated event."""
    return f"""(COALESCE({score_expr or f'{alias}.score'}, 0) >= 70 OR {alias}.official=1 OR EXISTS (
        SELECT 1 FROM story_items selected_si
        JOIN stories selected_st ON selected_st.id=selected_si.story_id
        WHERE selected_si.item_id={alias}.id AND selected_st.redirect_to IS NULL
          AND selected_st.source_count>=2))"""


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


def _source_status(row, now: datetime | None = None) -> str:
    status = "bad" if row["fail_count"] else ("ok" if row["last_success_at"] else "never")
    if row["last_error"] and not row["fail_count"]:
        status = "partial"
    now = now or datetime.now(timezone.utc)
    if (status == "ok" and now - _fmt_dt(row["last_success_at"])
            > timedelta(minutes=max(15, row["interval_minutes"] * 3))):
        status = "stale"
    return status


def _system_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    now_utc = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with get_db() as db:
        item = db.execute("""SELECT COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END),0) AS pending_score,
            COALESCE(SUM(CASE WHEN tmt IS NULL THEN 1 ELSE 0 END),0) AS pending_tmt,
            COALESCE(SUM(CASE WHEN tmt=0 THEN 1 ELSE 0 END),0) AS hidden,
            MAX(fetched_at) AS last_item_at FROM items""").fetchone()
        derived_pending = db.execute("SELECT COUNT(*) FROM derived_dirty").fetchone()[0]
        reports = db.execute(
            "SELECT COUNT(*) AS total, MAX(date) AS latest FROM daily_reports").fetchone()
        source_rows = db.execute("""SELECT fail_count,last_success_at,last_error,interval_minutes
            FROM sources WHERE enabled=1""").fetchall()
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
    source_states = [_source_status(row, now) for row in source_rows]
    issues = sum(state != "ok" for state in source_states)
    oldest_ready_age = (
        max(0, int((now - _fmt_dt(oldest_ready)).total_seconds()))
        if oldest_ready else None
    )
    job_delayed = oldest_ready_age is not None and oldest_ready_age > PIPELINE_JOB_STALE_SECONDS
    job_issues = job_states["blocked"] + job_states["dead_letter"] + expired_running
    dataset_owner_matches = dataset_row["owner_environment_id"] == ENVIRONMENT_ID
    worker = read_worker_heartbeat(expected_version=APP_VERSION)
    worker_required = DURABLE_JOBS_ENABLED
    readiness_issues = []
    if not dataset_owner_matches:
        readiness_issues.append("dataset_owner_mismatch")
    if worker_required and not worker.healthy:
        readiness_issues.append(f"worker_{worker.status}")
    pipeline_issues = []
    if issues:
        pipeline_issues.append("source_failures_or_staleness")
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
            "enabled": len(source_rows), "issues": issues, "last_run_at": last_fetch,
        },
        "reports": {"total": reports["total"], "latest": reports["latest"]},
        "jobs": {
            "enabled": DURABLE_JOBS_ENABLED,
            "states": job_states,
            "oldest_ready_at": oldest_ready,
            "oldest_ready_age_seconds": oldest_ready_age,
            "expired_running": expired_running,
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


def _query_items(channel: str = "all", company: str = "", event: str = "", cat: str = "",
                 mode: str = "selected", limit: int = 60, offset: int = 0):
    """Selected is a deduplicated event feed; all preserves every visible report."""
    cte, curation_join, visible, score_expr, category_expr = portal_curation_sql(CURATION_READ_ENABLED)
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
        where += (" AND EXISTS (SELECT 1 FROM item_companies ic JOIN companies c "
                "ON c.id=ic.company_id WHERE ic.item_id=i.id AND c.slug=?)")
        params.append(company)
    if event:
        where += " AND i.event_type=?"
        params.append(event)

    base = f"""SELECT i.*, s.name AS source_name, si.story_id
              {', cv.score AS curation_rank_score' if CURATION_READ_ENABLED else ''}
              FROM items i JOIN sources s ON s.id=i.source_id
              LEFT JOIN story_items si ON si.item_id=i.id""" + curation_join + where
    if mode == "selected":
        sql = (cte + ", " if cte else "WITH ") + """eligible AS (""" + base + f"""), ranked AS (
            SELECT eligible.*, ROW_NUMBER() OVER (
                PARTITION BY COALESCE(story_id, 'item:' || id)
                ORDER BY published_at DESC, official DESC, COALESCE({'curation_rank_score' if CURATION_READ_ENABLED else 'score'},-1) DESC, id DESC
            ) AS story_rank FROM eligible)
            SELECT * FROM ranked WHERE story_rank=1
            ORDER BY published_at DESC,id DESC LIMIT ? OFFSET ?"""
    else:
        sql = cte + base + " ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return rows


def _top_clusters(limit: int = 10, channel: str = "all", topic: str = "", days: int = 2) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cte, curation_join, visible, _, _ = portal_curation_sql(CURATION_READ_ENABLED)
    any_visible = (f"AND EXISTS(SELECT 1 FROM story_items si JOIN items i ON i.id=si.item_id "
                   f"{curation_join} WHERE si.story_id=st.id AND {visible})"
                   if CURATION_READ_ENABLED else "")
    with get_db() as db:
        rows = db.execute(
            cte + f"""SELECT st.*,st.last_at AS updated_at FROM stories st
               WHERE st.redirect_to IS NULL AND st.item_count>0 AND st.last_at>=?
               {any_visible}
               AND (?='all' OR EXISTS(SELECT 1 FROM story_items si JOIN items i ON i.id=si.item_id
                   {curation_join} WHERE si.story_id=st.id AND i.channel=? AND {visible}))
               AND (?='' OR EXISTS(SELECT 1 FROM story_items si JOIN item_topics it ON it.item_id=si.item_id
                   JOIN items i ON i.id=si.item_id {curation_join}
                   WHERE si.story_id=st.id AND it.topic_slug=? AND {visible}))
               ORDER BY (st.source_count>=2) DESC,st.heat DESC,st.id LIMIT ?""",
            (cutoff,channel,channel,topic,topic,limit)).fetchall()
        names = {r["slug"]: dict(r) for r in db.execute("SELECT slug,name,name_zh FROM companies")}
    out = []
    for rank,r in enumerate(rows,1):
        value = dict(r)
        value.update(rank=rank,heat=int(r['heat']*100), companies=[dict(slug=s,label=(names.get(s) or {}).get('name_zh')
                     or (names.get(s) or {}).get('name') or s) for s in json.loads(r['company_slugs'])])
        out.append(value)
    return out


@app.get("/", response_class=HTMLResponse)
def index(request: Request, channel: str = "all", company: str = "", event: str = "",
          cat: str = "", mode: str = "selected", page: int = 1):
    page = max(1, page)
    mode = mode if mode in ("selected", "all") else "selected"
    page_size = 30 if mode == "selected" else 60
    rows = _query_items(channel=channel, company=company, event=event, cat=cat, mode=mode,
                        limit=page_size + 1, offset=(page - 1) * page_size)
    has_next = len(rows) > page_size
    rows = rows[:page_size]
    items = _decorate(rows)
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
                "SELECT slug, name, name_zh, ticker, market FROM companies ORDER BY market, name"
            ).fetchall()
        events = sorted(EVENT_NAMES.items())

    with get_db() as db:
        last_fetch = db.execute("SELECT MAX(fetched_at) AS m FROM items").fetchone()["m"]

    return templates.TemplateResponse(request, "index.html", dict(
        days=days, tabs=CHANNEL_TABS, channel=channel, company=company, event=event,
        mode=mode, cat=cat,
        companies=companies, events=events, clusters=_top_clusters(8, channel),
        page=page, has_next=has_next,
        last_update=_relative(last_fetch),
    ))


@app.get("/hot", response_class=HTMLResponse)
def hot(request: Request):
    return templates.TemplateResponse(request, "hot.html", dict(clusters=_top_clusters(50)))


@app.get("/daily", response_class=HTMLResponse)
def daily_list(request: Request):
    with get_db() as db:
        reports = db.execute("SELECT date, created_at FROM daily_reports ORDER BY date DESC").fetchall()
    return templates.TemplateResponse(request, "daily_list.html", dict(
        reports=[dict(r, created_rel=_relative(r["created_at"])) for r in reports]))


@app.get("/daily/{date}", response_class=HTMLResponse)
def daily_detail(request: Request, date: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM daily_reports WHERE date=?", (date,)).fetchone()
    if not row:
        return RedirectResponse("/daily", status_code=302)
    return templates.TemplateResponse(request, "daily_detail.html",
                                      dict(date=date, html=render_markdown(row["content"])))


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    q = q.strip()
    items = []
    if q:
        with get_db() as db:
            rows: list = []
            if len(q) >= 3:  # trigram 分词最少 3 字符
                try:
                    rows = db.execute(
                        """SELECT i.*, s.name AS source_name FROM items_fts
                           JOIN items i ON i.id = items_fts.rowid
                           JOIN sources s ON s.id = i.source_id
                           WHERE items_fts MATCH ? AND COALESCE(i.tmt, 1) != 0 ORDER BY i.published_at DESC LIMIT 100""",
                        (f'"{q.replace(chr(34), chr(34) * 2)}"',)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = db.execute(
                    """SELECT i.*, s.name AS source_name FROM items i
                       JOIN sources s ON s.id=i.source_id
                       WHERE COALESCE(i.tmt, 1) != 0 AND (i.title LIKE ? ESCAPE '\\'
                       OR i.title_zh LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\')
                       ORDER BY i.published_at DESC LIMIT 100""", (like, like, like)).fetchall()
        items = _decorate(rows)
    return templates.TemplateResponse(request, "search.html", dict(q=q, items=items))


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
            """SELECT key, name, channel, tier, type, url, enabled, interval_minutes,
                      fail_count, last_success_at, last_run_at, last_error
               FROM sources WHERE enabled=1
               ORDER BY CASE tier WHEN 'official' THEN 0 WHEN 'media' THEN 1
               WHEN 'info' THEN 2 ELSE 3 END, channel, name""").fetchall()
        counts = {r["channel"]: r["n"] for r in db.execute(
            "SELECT channel, COUNT(*) AS n FROM items GROUP BY channel")}
    sources = []
    for r in rows:
        status = _source_status(r)
        sources.append(dict(r, last_success_rel=_relative(r["last_success_at"]), status=status))
    system = _system_snapshot()
    return templates.TemplateResponse(request, "health.html", dict(
        sources=sources, counts=counts, system=system,
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
        "checked_at": snapshot["checked_at"],
    }


@app.get("/about-heat", response_class=HTMLResponse)
def about_heat():
    return RedirectResponse("/hot", status_code=302)


def _topic_stats(db):
    cte, _, _, score_expr, _ = portal_curation_sql(CURATION_READ_ENABLED)
    selected = _selected_clause(score_expr=score_expr) if CURATED_FEED_ENABLED else "1=1"
    item_join = ("LEFT JOIN curation_values cv ON cv.item_id=it.item_id "
                 "LEFT JOIN items i ON i.id=it.item_id AND cv.visible=1"
                 if CURATION_READ_ENABLED else
                 "LEFT JOIN items i ON i.id=it.item_id AND COALESCE(i.tmt,1)!=0")
    return [dict(r) for r in db.execute(cte + f"""SELECT t.*,COUNT(i.id) AS total,
        COALESCE(SUM(CASE WHEN i.id IS NOT NULL AND {selected} THEN 1 ELSE 0 END),0) AS selected,
        MAX(i.published_at) AS last_at FROM topics t
        LEFT JOIN item_topics it ON it.topic_slug=t.slug
        {item_join}
        WHERE t.enabled=1
        GROUP BY t.slug ORDER BY t.position""")]


@app.get('/topics',response_class=HTMLResponse)
def topics_index(request: Request):
    with get_db() as db:
        entries = _topic_stats(db)
    groups = [dict(key=key,name=name,description=desc,topics=[t for t in entries if t['group_key']==key])
              for key,name,desc in GROUPS]
    return templates.TemplateResponse(request,'topics.html',dict(groups=groups,total=len(entries)))


@app.get('/topics/{slug}',response_class=HTMLResponse)
def topic_detail(request: Request,slug: str,mode: str='selected',page: int=Query(1,ge=1)):
    mode = mode if mode in ('all','selected') else 'selected'
    cte, curation_join, visible, score_expr, _ = portal_curation_sql(CURATION_READ_ENABLED)
    selected = " AND " + _selected_clause(score_expr=score_expr) if mode=='selected' and CURATED_FEED_ENABLED else ''
    with get_db() as db:
        topic = next((t for t in _topic_stats(db) if t['slug']==slug),None)
        if topic is None:
            raise HTTPException(404,'主题不存在')
        base = f"""SELECT i.*,s.name AS source_name,si.story_id
            {', cv.score AS curation_rank_score' if CURATION_READ_ENABLED else ''} FROM item_topics it
            JOIN items i ON i.id=it.item_id JOIN sources s ON s.id=i.source_id
            LEFT JOIN story_items si ON si.item_id=i.id
            {curation_join}
            WHERE it.topic_slug=? AND {visible} {selected}"""
        if mode == 'selected':
            sql = (cte + ", " if cte else "WITH ") + """eligible AS (""" + base + f"""), ranked AS (
                SELECT eligible.*,ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(story_id,'item:' || id)
                    ORDER BY published_at DESC,official DESC,COALESCE({'curation_rank_score' if CURATION_READ_ENABLED else 'score'},-1) DESC,id DESC
                ) AS story_rank FROM eligible)
                SELECT * FROM ranked WHERE story_rank=1
                ORDER BY published_at DESC,id DESC LIMIT 21 OFFSET ?"""
        else:
            sql = cte + base + " ORDER BY i.published_at DESC,i.id DESC LIMIT 21 OFFSET ?"
        rows = db.execute(sql,(slug,(page-1)*20)).fetchall()
    days = []
    for item in _decorate(rows[:20]):
        if not days or days[-1]['key']!=item['date_key']:
            days.append(dict(key=item['date_key'],label=_date_label(datetime.fromisoformat(item['date_key'])),rows=[]))
        days[-1]['rows'].append(item)
    return templates.TemplateResponse(request,'topic_detail.html',dict(
        topic=topic,days=days,clusters=_top_clusters(5,topic=slug,days=14),page=page,mode=mode,
        has_next=len(rows)>20,last_update=_relative(topic['last_at']) if topic['last_at'] else '暂无收录'))


@app.get('/story/{story_id}',response_class=HTMLResponse)
def story_detail(request: Request,story_id: str,page: int=Query(1,ge=1)):
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
                    published_label=_fmt_dt(row['published_at']).strftime('%m月%d日 %H:%M'))
        reports.append(data)
    story_view = dict(story)
    if CURATION_READ_ENABLED:
        story_view['item_count'] = len(source_rows)
    return templates.TemplateResponse(request,'story.html',dict(story=story_view,reports=reports,
        sources=list(sources.values()),unknown=unknown,tags=tags,official_count=sum(r['official'] for r in source_rows),
        first_label=_fmt_dt(story['first_at']).strftime('%m月%d日 %H:%M'),
        last_label=_fmt_dt(story['last_at']).strftime('%m月%d日 %H:%M'),page=page,has_next=len(rows)>50))
