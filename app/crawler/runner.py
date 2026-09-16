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
from ..documents import project_candidate
from ..ingest import RawObservation, begin_ingest_run, finish_ingest_run, observe_candidate
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
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(query), ""))


def _legacy_publication_projection(raw: dict) -> tuple[str, str]:
    """Keep the NOT NULL legacy page sortable without inventing source truth."""
    inserted = datetime.now(timezone.utc)
    observed = inserted
    try:
        candidate_observed = datetime.fromisoformat(
            str(raw.get("observed_at") or "").replace("Z", "+00:00")
        )
        if candidate_observed.tzinfo is None:
            raise ValueError("naive observed_at")
        observed = candidate_observed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        published = datetime.fromisoformat(
            str(raw.get("published_at") or "").replace("Z", "+00:00")
        )
        if published.tzinfo is None:
            raise ValueError("naive source timestamp")
        published = published.astimezone(timezone.utc)
        if published > observed + timedelta(minutes=10):
            raise ValueError("future source timestamp")
        return published.isoformat(), "source_published"
    except (TypeError, ValueError):
        basis = "connector_observed" if observed != inserted else "item_inserted"
        return observed.isoformat(), basis


def insert_item(
    source_key: str,
    raw: dict,
    via: str = "normal",
    *,
    observation: RawObservation | None = None,
) -> bool:
    """入库一条（去重）。返回是否新插入。raw 带 _error 时只记日志。"""
    if "_error" in raw:
        log.warning("源 %s 部分子任务失败: %s", source_key, raw["_error"])
        return False
    url = _normalize_url(raw.get("url") or "")
    title = (raw.get("title") or "").strip()
    if not url or not title:
        return False
    published_at, legacy_time_basis = _legacy_publication_projection(raw)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        src = db.execute(
            "SELECT id,channel,tier,type FROM sources WHERE key=?", (source_key,)
        ).fetchone()
        if not src:
            return False
        text = f"{title} {raw.get('summary') or ''}"
        slugs = list(dict.fromkeys((raw.get("companies") or []) + company_match.match_companies(text)))
        legacy_extra = dict(raw.get("extra") or {})
        legacy_extra["_legacy_time_basis"] = legacy_time_basis
        cur = db.execute(
            """INSERT OR IGNORE INTO items (source_id, url, title, title_en, summary, raw_summary, channel, event_type,
                                  score, heat, companies, official, via, published_at, fetched_at, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (src["id"], url, title, raw.get("title_en") or "", raw.get("summary") or "", raw.get("summary") or "",
             raw.get("channel") or src["channel"], raw.get("event_type") or "", None, 0,
             json.dumps(slugs, ensure_ascii=False), 1 if raw.get("official") or src["tier"] == "official" else 0,
             via, published_at, _now(), json.dumps(legacy_extra, ensure_ascii=False)),
        )
        inserted = bool(cur.rowcount)
        if inserted:
            item_id = cur.lastrowid
        else:
            existing = db.execute('SELECT id,companies FROM items WHERE url=?',(url,)).fetchone()
            item_id = existing['id']
            merged = list(dict.fromkeys(json.loads(existing['companies']) + slugs))
            if merged != json.loads(existing['companies']):
                db.execute('UPDATE items SET companies=? WHERE id=?',
                           (json.dumps(merged,ensure_ascii=False),item_id))
            slugs = merged
        db.execute("""INSERT INTO item_discoveries(item_id,source_id,first_seen_at,last_seen_at)
                      VALUES(?,?,?,?) ON CONFLICT(item_id,source_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                   (item_id,src['id'],_now(),_now()))
        for slug in slugs:
            row = db.execute("SELECT id FROM companies WHERE slug=?", (slug,)).fetchone()
            if row:
                db.execute("INSERT OR IGNORE INTO item_companies (item_id, company_id) VALUES (?,?)",
                           (item_id, row["id"]))
        if observation is not None:
            projection = project_candidate(
                db,
                source=src,
                legacy_item_id=item_id,
                candidate=raw,
                canonical_url=url,
                observation=observation,
            )
            if projection.created_version and not inserted:
                # ``items`` remains the compatibility projection used by the
                # portal until versioned reads are enabled in a later PR.
                values = [title, raw.get("summary") or "", raw.get("summary") or ""]
                assignment = "title=?,summary=?,raw_summary=?"
                if projection.published_at is not None:
                    assignment += ",published_at=?"
                    values.append(projection.published_at)
                values.append(item_id)
                db.execute(f"UPDATE items SET {assignment} WHERE id=?", values)
    return inserted


def run_source(source: dict) -> tuple[int, bool, str]:
    """抓取一个源。返回 (新条数, 是否成功, 备注)。"""
    fetcher = FETCHERS.get(source["type"])
    if fetcher is None:
        message = f"未知源类型 {source['type']}"
        _record(source["key"], ok=False, new=0, message=message)
        return 0, False, message
    ingest_run = begin_ingest_run(source)
    try:
        raws = fetcher(source)
    except Exception as exc:  # noqa: BLE001 - 源级失败，记健康状态
        log.warning("源 %s 抓取失败: %s", source["key"], exc)
        finish_ingest_run(
            ingest_run,
            status="failed",
            raw_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            byte_count=0,
            error_code=f"fetch_{type(exc).__name__}",
        )
        _record(source["key"], ok=False, new=0, message=str(exc)[:300])
        return 0, False, str(exc)[:300]

    errors = [r["_error"] for r in raws if "_error" in r]
    inserted = 0
    accepted = 0
    duplicates = 0
    rejected = 0
    observed_bytes = 0
    for ordinal, raw in enumerate(raws):
        if "_error" in raw:
            continue
        try:
            observation = observe_candidate(
                ingest_run, raw, ordinal=ordinal, observed_at=raw.get("observed_at")
            )
            observed_bytes += observation.size_bytes
            if insert_item(source["key"], raw, observation=observation):
                inserted += 1
            else:
                duplicates += 1
            accepted += 1
        except Exception as exc:
            rejected += 1
            errors.append(f"证据或入库失败: {type(exc).__name__}")
            log.exception("源 %s 条目证据或入库失败", source["key"])
    ok = not errors  # 部分失败也展示，已成功抓取的条目照常保留
    message = "; ".join(errors[-3:]) if errors else ""
    run_status = "succeeded" if ok else ("partial" if accepted else "failed")
    finish_ingest_run(
        ingest_run,
        status=run_status,
        raw_count=sum("_error" not in raw for raw in raws),
        accepted_count=accepted,
        duplicate_count=duplicates,
        rejected_count=rejected,
        byte_count=observed_bytes,
        error_code="candidate_rejected" if errors else None,
    )
    _record(source["key"], ok=ok, new=inserted, message=message,
            partial=bool(errors) and accepted > 0)
    return inserted, ok, message


def _record(key: str, ok: bool, new: int, message: str, partial: bool = False) -> None:
    with get_db() as db:
        if partial:
            # Some companies remain available: retain the base polling interval
            # so a broken company does not delay all the others for six hours.
            db.execute("""UPDATE sources SET last_run_at=?,fail_count=0,last_error=? WHERE key=?""",
                       (_now(),"部分失败："+message,key))
        elif ok:
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


def retry_interval(interval_minutes: int, fail_count: int) -> int:
    """Exponential backoff capped at six hours, never below the base interval."""
    return max(interval_minutes, min(interval_minutes * 2 ** min(fail_count, 8), 360))


def run_due_sources() -> dict:
    """跑所有到期的常规源（reconcile 层单独调度），完成后重建热点聚类。"""
    now = datetime.now(timezone.utc)
    due = []
    for s in all_sources():
        if s.get("tier") == "reconcile":
            continue
        with get_db() as db:
            row = db.execute("SELECT enabled, last_run_at, fail_count, interval_minutes FROM sources WHERE key=?",
                             (s["key"],)).fetchone()
        if not row or not row["enabled"]:
            continue
        if row["last_run_at"]:
            last = datetime.fromisoformat(row["last_run_at"])
            if now < last + timedelta(minutes=retry_interval(row["interval_minutes"], row["fail_count"])):
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
                    inserted, ok, message = fut.result()
                    results.append(dict(key=key, inserted=inserted, **({"error": message} if not ok else {})))
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
            previous = db.execute("SELECT id,channel FROM sources WHERE key=?", (s["key"],)).fetchone()
            db.execute(
                """INSERT INTO sources (key, name, channel, tier, type, url, company_slug,
                                        enabled, interval_minutes)
                   VALUES (?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(key) DO UPDATE SET name=excluded.name, url=excluded.url,
                       channel=excluded.channel, tier=excluded.tier, type=excluded.type,
                       company_slug=excluded.company_slug, interval_minutes=excluded.interval_minutes""",
                (s["key"], s["name"], s["channel"], s.get("tier", "media"), s["type"],
                 s.get("url", ""), s.get("company_slug", ""), s.get("interval_minutes", 30)))
            if previous and previous["channel"] != s["channel"]:
                # Source taxonomy is canonical. Reclassify history and let the
                # derived trigger rebuild topic/event membership incrementally.
                db.execute("UPDATE items SET channel=? WHERE source_id=?",
                           (s["channel"], previous["id"]))
        db.execute(f"UPDATE sources SET enabled=0 WHERE key NOT IN ({','.join('?' * len(keys))})", keys)
    company_match.invalidate_cache()
