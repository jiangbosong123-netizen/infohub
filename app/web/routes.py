from __future__ import annotations

"""FastAPI 页面路由：首页时间线 / 热点榜 / 日报 / 搜索 / 源状态。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..ai.daily import EVENT_NAMES, render_markdown
from ..config import APP_TZ, BASE_DIR
from ..database import get_db

app = FastAPI(title="行业情报站")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))

CHANNEL_TABS = [("all", "全部"), ("ai", "AI"), ("robot", "机器人"), ("stock", "股市")]


def _fmt_dt(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ)


def _relative(iso: str | None) -> str:
    if not iso:
        return "从未抓取"
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "刚刚"
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 48:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


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
    cluster_map: dict[int, dict] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        with get_db() as db:
            for m in db.execute(
                f"""SELECT cm.item_id, cl.id AS cluster_id, cl.source_count
                    FROM cluster_members cm JOIN clusters cl ON cl.id = cm.cluster_id
                    WHERE cm.item_id IN ({marks})""", ids):
                cluster_map[m["item_id"]] = dict(m)
    with get_db() as db:
        company_rows = {r["slug"]: dict(r) for r in
                        db.execute("SELECT slug, name, name_zh, ticker FROM companies")}

    out = []
    for r in rows:
        slugs = json.loads(r["companies"] or "[]")
        dt = _fmt_dt(r["published_at"])
        cl = cluster_map.get(r["id"])
        out.append(dict(
            id=r["id"], url=r["url"], title=r["title"], summary=r["summary"],
            score=(r["score"] if (r["score"] is not None and r["score"] >= 0) else None),
            official=bool(r["official"]),
            via=r["via"], channel=r["channel"],
            source_name=r["source_name"], event_type=r["event_type"] or "",
            event_label=EVENT_NAMES.get(r["event_type"] or "", ""),
            hms=dt.strftime("%H:%M"), date_key=dt.date().isoformat(),
            companies=[dict(slug=s, label=(company_rows.get(s) or {}).get("name_zh")
                            or (company_rows.get(s) or {}).get("name") or s) for s in slugs],
            cluster_id=cl["cluster_id"] if cl else None,
            extra_sources=(cl["source_count"] - 1) if cl else 0,
        ))
    return out


def _query_items(channel: str = "all", company: str = "", event: str = "",
                 limit: int = 60, offset: int = 0):
    sql = """SELECT i.*, s.name AS source_name FROM items i
             JOIN sources s ON s.id = i.source_id WHERE 1=1"""
    params: list = []
    if channel and channel != "all":
        sql += " AND i.channel=?"
        params.append(channel)
    if company:
        sql += (" AND EXISTS (SELECT 1 FROM item_companies ic JOIN companies c "
                "ON c.id=ic.company_id WHERE ic.item_id=i.id AND c.slug=?)")
        params.append(company)
    if event:
        sql += " AND i.event_type=?"
        params.append(event)
    sql += " ORDER BY i.published_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return rows


def _top_clusters(limit: int = 10) -> list[dict]:
    """热点榜：优先多信源事件；不够时用单源高热度补位。"""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM clusters WHERE source_count >= 2 ORDER BY heat DESC LIMIT ?",
            (limit,)).fetchall()
        if len(rows) < limit:
            rows += db.execute(
                "SELECT * FROM clusters WHERE source_count < 2 ORDER BY heat DESC LIMIT ?",
                (limit - len(rows),)).fetchall()
        names = {r["slug"]: dict(r) for r in db.execute("SELECT slug, name, name_zh FROM companies")}
    out = []
    for i, r in enumerate(rows, 1):
        slugs = json.loads(r["company_slugs"] or "[]")
        out.append(dict(
            rank=i, id=r["id"], title=r["title"], url=r["url"], channel=r["channel"],
            heat=int(r["heat"] * 100), source_count=r["source_count"],
            companies=[dict(slug=s, label=(names.get(s) or {}).get("name_zh")
                            or (names.get(s) or {}).get("name") or s) for s in slugs],
        ))
    return out


@app.get("/", response_class=HTMLResponse)
def index(request: Request, channel: str = "all", company: str = "", event: str = "",
          page: int = 1):
    page = max(1, page)
    rows = _query_items(channel=channel, company=company, event=event,
                        limit=60, offset=(page - 1) * 60)
    items = _decorate(rows)
    days: list[dict] = []
    for it in items:
        if days and days[-1]["key"] == it["date_key"]:
            days[-1]["rows"].append(it)
        else:
            dt = datetime.fromisoformat(
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
        companies=companies, events=events, clusters=_top_clusters(8),
        page=page, has_next=len(items) == 60,
        last_update=_relative(last_fetch),
    ))


@app.get("/hot", response_class=HTMLResponse)
def hot(request: Request):
    clusters = _top_clusters(50)
    with get_db() as db:
        for cl in clusters:
            members = db.execute(
                """SELECT i.title, i.url, i.published_at, i.official, i.score, s.name AS source_name
                   FROM cluster_members cm JOIN items i ON i.id=cm.item_id
                   JOIN sources s ON s.id=i.source_id WHERE cm.cluster_id=?
                   ORDER BY i.published_at DESC""", (cl["id"],)).fetchall()
            cl["members"] = [dict(m, hms=_fmt_dt(m["published_at"]).strftime("%m-%d %H:%M"))
                             for m in members]
    return templates.TemplateResponse(request, "hot.html", dict(clusters=clusters))


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
                           WHERE items_fts MATCH ? ORDER BY i.published_at DESC LIMIT 100""",
                        (f'"{q.replace(chr(34), chr(34) * 2)}"',)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = f"%{q}%"
                rows = db.execute(
                    """SELECT i.*, s.name AS source_name FROM items i
                       JOIN sources s ON s.id=i.source_id
                       WHERE i.title LIKE ? OR i.summary LIKE ?
                       ORDER BY i.published_at DESC LIMIT 100""", (like, like)).fetchall()
        items = _decorate(rows)
    return templates.TemplateResponse(request, "search.html", dict(q=q, items=items))


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
        status = "never" if not r["last_success_at"] else ("ok" if r["fail_count"] == 0 else "bad")
        sources.append(dict(r, last_success_rel=_relative(r["last_success_at"]), status=status))
    return templates.TemplateResponse(request, "health.html", dict(
        sources=sources, counts=counts, now=datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M")))


@app.get("/about-heat", response_class=HTMLResponse)
def about_heat():
    return RedirectResponse("/hot", status_code=302)
