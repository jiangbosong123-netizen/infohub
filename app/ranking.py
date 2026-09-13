from __future__ import annotations

"""热度算法与热点事件聚类。

热度 = 基础分(AI 评分或默认 0.55) × 时间衰减(半衰期 18h) × 官方加成
热点簇 = 同频道内标题相似（股市另要求命中同一家公司）的多条报道聚合，
        簇热度 = 最高条目热度 × 多信源加成（最多 6 家计满）。
"""
from datetime import datetime, timezone


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
    if row["score"] is not None and row["score"] >= 0:
        base = row["score"] / 100
    else:
        # 无 AI 评分时的默认基础分：例行文件压低，普通资讯居中
        base = 0.35 if (row["event_type"] or "") in ("insider", "other") else 0.5
    bonus = 1.25 if row["official"] else 1.0
    return round(base * bonus * _decay(row["published_at"], datetime.now(timezone.utc)), 4)


AI_CAT_NAMES = {"model": "模型", "product": "产品", "industry": "行业",
                "paper": "论文", "opinion": "观点"}


def rebuild_clusters(window_h: int = 48) -> int:
    """Compatibility entry point: refresh persistent events instead of deleting clusters.

    window_h is retained for callers; presentation windows are applied on read.
    """
    from .stories import refresh_derived
    return refresh_derived()['stories']
