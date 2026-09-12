from __future__ import annotations

"""抓取调度核心：到期源并发抓取 → 统一入库（URL 归一去重）→ 源健康记录 → 重建热点聚类。

防漏设计：RSS/SEC 每次拉最新几十条靠 URL 去重增量入库；港交所按最近 3 天日期窗拉取；
任何源失败只累计 fail_count 并退避，不影响其他源，恢复后自动续上。
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .. import company_match
from ..database import get_db
from . import fastnews, hkex_source, html_source, rss_source, sec_source, sina_source
from . import googlenews
from .sources import all_sources

log = logging.getLogger(__name__)

FETCHERS = {
    "rss": rss_source.fetch_rss,
    "html": html_source.fetch_html,
    "sec": sec_source.fetch_sec,
    "hkex": hkex_source.fetch_hkex,
    "sina": sina_source.fetch_sina,
    "cls": fastnews.fetch_cls,
    "wscn_live": fastnews.fetch_wscn_live,
    "googlenews": googlenews.fetch_google_news,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(query), ""))


def insert_item(source_key: str, raw: dict, via: str = "normal") -> bool:
    """入库一条（去重）。返回是否新插入。raw 带 _error 时只记日志。"""
    if "_error" in raw:
        log.warning("源 %s 部分子任务失败: %s", source_key, raw["_error"])
        return False
    url = _normalize_url(raw["url"])
    title = (raw.get("title") or "").strip()
    if not url or not title:
        return False
    with get_db() as db:
        if db.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone():
            return False
        src = db.execute("SELECT id, channel FROM sources WHERE key=?", (source_key,)).fetchone()
        if not src:
            return False
        text = f"{title} {raw.get('summary') or ''}"
        slugs = raw.get("companies") or company_match.match_companies(text)
        cur = db.execute(
            """INSERT INTO items (source_id, url, title, title_en, summary, channel, event_type,
                                  score, heat, companies, official, via, published_at, fetched_at, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (src["id"], url, title, raw.get("title_en") or "", raw.get("summary") or "",
             raw.get("channel") or src["channel"], raw.get("event_type") or "", None, 0,
             json.dumps(slugs, ensure_ascii=False), 1 if raw.get("official") else 0,
             via, raw["published_at"], _now(), json.dumps(raw.get("extra") or {}, ensure_ascii=False)),
        )
        item_id = cur.lastrowid
        for slug in slugs:
            row = db.execute("SELECT id FROM companies WHERE slug=?", (slug,)).fetchone()
            if row:
                db.execute("INSERT OR IGNORE INTO item_companies (item_id, company_id) VALUES (?,?)",
                           (item_id, row["id"]))
    return True


def run_source(source: dict) -> tuple[int, bool, str]:
    """抓取一个源。返回 (新条数, 是否成功, 备注)。"""
    fetcher = FETCHERS.get(source["type"])
    if fetcher is None:
        return 0, False, f"未知源类型 {source['type']}"
    try:
        raws = fetcher(source)
    except Exception as exc:  # noqa: BLE001 - 源级失败，记健康状态
        log.warning("源 %s 抓取失败: %s", source["key"], exc)
        _record(source["key"], ok=False, new=0, message=str(exc)[:300])
        return 0, False, str(exc)[:300]

    errors = [r["_error"] for r in raws if "_error" in r]
    inserted = 0
    for raw in raws:
        if insert_item(source["key"], raw):
            inserted += 1
    ok = not raws or len(errors) < len(raws)  # 全部子任务失败才算源失败
    message = "; ".join(errors[-3:]) if errors else ""
    _record(source["key"], ok=ok, new=inserted, message=message)
    return inserted, ok, message


def _record(key: str, ok: bool, new: int, message: str) -> None:
    with get_db() as db:
        if ok:
            db.execute(
                """UPDATE sources SET last_run_at=?, last_success_at=?, fail_count=0,
                                      last_error=NULL WHERE key=?""",
                (_now(), _now(), key))
        else:
            db.execute(
                """UPDATE sources SET last_run_at=?, fail_count=fail_count+1,
                                      last_error=? WHERE key=?""",
                (_now(), message, key))
        db.execute("INSERT INTO fetch_log (source_id, ran_at, ok, new_items, message) "
                   "SELECT id, ?, ?, ?, ? FROM sources WHERE key=?",
                   (_now(), 1 if ok else 0, new, message, key))


def run_due_sources() -> dict:
    """跑所有到期的常规源（reconcile 层单独调度），完成后重建热点聚类。"""
    now = datetime.now(timezone.utc)
    due = []
    for s in all_sources():
        if s.get("tier") == "reconcile":
            continue
        with get_db() as db:
            row = db.execute("SELECT enabled, last_run_at FROM sources WHERE key=?",
                             (s["key"],)).fetchone()
        if not row or not row["enabled"]:
            continue
        if row["last_run_at"]:
            last = datetime.fromisoformat(row["last_run_at"])
            if now < last + timedelta(minutes=s["interval_minutes"]):
                continue
        due.append(s)

    results = []
    if due:
        # 同域名的请求错开 3 秒提交，避免触发目标站限流（如 Yahoo 的 429）
        from urllib.parse import urlsplit
        import time as _time
        next_slot: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for s in due:
                host = urlsplit(s.get("url", "")).netloc or s["key"]
                gap = 3.0 if "yahoo" in host else 0.3
                wait = max(0.0, next_slot.get(host, 0.0) - _time.monotonic())
                if wait:
                    _time.sleep(wait)
                next_slot[host] = _time.monotonic() + gap
                futures[pool.submit(run_source, s)] = s["key"]
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    results.append(dict(key=key, inserted=fut.result()[0]))
                except Exception as exc:  # noqa: BLE001
                    log.exception("源 %s 执行异常", key)
                    results.append(dict(key=key, error=str(exc)))
    from .. import ranking
    try:
        ranking.rebuild_clusters()
    except Exception:  # noqa: BLE001
        log.exception("热点聚类重建失败")
    return dict(ran=len(due), results=results)


def upsert_sources() -> None:
    """把源注册表同步进数据库（保留健康状态，新增源自动注册，移除源自动停用）。"""
    keys = []
    with get_db() as db:
        for s in all_sources():
            keys.append(s["key"])
            db.execute(
                """INSERT INTO sources (key, name, channel, tier, type, url, company_slug,
                                        enabled, interval_minutes)
                   VALUES (?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(key) DO UPDATE SET name=excluded.name, url=excluded.url,
                       channel=excluded.channel, tier=excluded.tier, type=excluded.type,
                       company_slug=excluded.company_slug, interval_minutes=excluded.interval_minutes""",
                (s["key"], s["name"], s["channel"], s.get("tier", "media"), s["type"],
                 s.get("url", ""), s.get("company_slug", ""), s.get("interval_minutes", 30)))
        db.execute(f"UPDATE sources SET enabled=0 WHERE key NOT IN ({','.join('?' * len(keys))})", keys)
    company_match.invalidate_cache()
