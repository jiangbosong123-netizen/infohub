from __future__ import annotations

"""热度算法与热点事件聚类。

热度 = 基础分(AI 评分或默认 0.55) × 时间衰减(半衰期 18h) × 官方加成
热点簇 = 同频道内标题相似（股市另要求命中同一家公司）的多条报道聚合，
        簇热度 = 最高条目热度 × 多信源加成（最多 6 家计满）。
"""
import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from .config import APP_TZ
from .database import get_db

SIM_THRESHOLD = 0.58


def _norm_title(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", (t or "").lower())


def _similar(a: str, b: str) -> float:
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _decay(published_at: str, now: datetime, half_life_h: float = 18.0) -> float:
    try:
        pub = datetime.fromisoformat(published_at)
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        hours = max(0.0, (now - pub).total_seconds() / 3600)
        return 0.5 ** (hours / half_life_h)
    except (ValueError, TypeError):
        return 0.5


def item_heat(row) -> float:
    if row["score"] is not None:
        base = row["score"] / 100
    else:
        # 无 AI 评分时的默认基础分：例行文件压低，普通资讯居中
        base = 0.35 if (row["event_type"] or "") in ("insider", "other", "") else 0.5
    bonus = 1.25 if row["official"] else 1.0
    return round(base * bonus * _decay(row["published_at"], datetime.now(timezone.utc)), 4)


AI_CAT_NAMES = {"model": "模型", "product": "产品", "industry": "行业",
                "paper": "论文", "opinion": "观点"}


def rebuild_clusters(window_h: int = 48) -> int:
    """重建最近 window_h 小时的热点簇。返回簇数量。"""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=window_h)).isoformat()
    with get_db() as db:
        rows = db.execute(
            """SELECT i.id, i.title, i.title_zh, i.url, i.channel, i.score, i.official,
                      i.published_at, i.event_type, i.tmt, i.companies, s.name AS source_name
               FROM items i JOIN sources s ON s.id = i.source_id
               WHERE i.published_at >= ? AND COALESCE(i.tmt, 1) != 0
               ORDER BY i.published_at DESC""",
            (since,)).fetchall()
        # 贪心聚类：新条目与已有簇的代表标题比较
        clusters: list[dict] = []
        for r in rows:
            slugs = set(json.loads(r["companies"] or "[]"))
            placed = False
            for cl in clusters:
                if cl["channel"] != r["channel"]:
                    continue
                if r["channel"] == "stock" and not (slugs & cl["slugs"]):
                    continue
                if _similar(r["title"], cl["title"]) >= SIM_THRESHOLD:
                    cl["members"].append(r)
                    cl["slugs"] |= slugs
                    placed = True
                    break
            if not placed:
                clusters.append(dict(title=r["title"], channel=r["channel"],
                                     members=[r], slugs=slugs))

        db.execute("DELETE FROM clusters")
        count = 0
        for cl in clusters:
            members = cl["members"]
            if len(members) == 1 and cl["channel"] == "stock" and not cl["slugs"]:
                continue  # 股市单条且无公司的散稿不进榜
            heats = [item_heat(m) for m in members]
            top = members[heats.index(max(heats))]
            sources = {m["source_name"] for m in members}
            heat = round(max(heats) * (0.7 + 0.3 * min(len(sources), 6)), 4)
            cur = db.execute(
                """INSERT INTO clusters (channel, title, url, heat, source_count,
                                         company_slugs, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (cl["channel"], top["title_zh"] or top["title"], top["url"], heat, len(sources),
                 json.dumps(sorted(cl["slugs"]), ensure_ascii=False),
                 max(m["published_at"] for m in members)))
            cid = cur.lastrowid
            db.executemany("INSERT OR IGNORE INTO cluster_members (cluster_id, item_id) VALUES (?,?)",
                           [(cid, m["id"]) for m in members])
            count += 1
    return count
