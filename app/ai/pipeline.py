from __future__ import annotations

"""AI 策展流水线：英文源翻译/浓缩为中文摘要 + 0-100 重要性评分 + 股市事件类型。

配置 .env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后启用；
未配置时 process_pending 直接返回 0，整站退化为纯聚合模式（热度算法照常工作）。
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from openai import OpenAI

from .. import config
from ..database import get_db

log = logging.getLogger(__name__)

VALID_EVENTS = {"earnings", "insider", "buyback", "ma", "personnel", "product",
                "regulation", "rating", "offering", "other"}

SYSTEM_PROMPT = """你是科技与财经资讯编辑，为信息聚合站处理条目。对输入的每条资讯输出：
1. title_zh：中文标题。英文标题翻译成简洁的中文新闻标题；中文标题精简润色，保留关键实体与数字。
2. score：0-100 重要性评分。对科技行业格局、相关公司股价影响越大分越高（重大财报/并购/监管/重磅产品 80-100，一般行业动态 50-79，例行文件/水文/软文 <50）。
3. summary_zh：用简体中文写 1-2 句摘要。英文内容翻译提炼，中文内容直接浓缩。客观、信息密度优先，别写成广告腔。
4. event_type：仅当该条 channel 为 "stock" 时，从 [earnings,insider,buyback,ma,personnel,product,regulation,rating,offering,other] 选最贴切的一个；其余情况给 null。

只输出 JSON 数组，不要任何其他文字：
[{"id": 1, "title_zh": "…", "score": 78, "summary_zh": "...", "event_type": "ma"}]"""


def _extract_json(text: str):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("响应里没有 JSON 数组")
    return json.loads(text[start:end + 1])


def _call_llm(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=120)
    resp = client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},  # 结构化策展任务，关闭深度思考提速省钱
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    return _extract_json(resp.choices[0].message.content or "")


def _is_content_filter(exc: Exception) -> bool:
    text = str(exc)
    return "1301" in text or "contentFilter" in text


def process_pending(limit: int = 20) -> int:
    """处理最近未评分的条目（只看最近 3 天，旧库存不打分），返回成功更新条数。"""
    if not config.llm_enabled():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    with get_db() as db:
        rows = db.execute(
            """SELECT id, title, summary, channel, event_type FROM items
               WHERE score IS NULL AND published_at >= ?
               ORDER BY id DESC LIMIT ?""", (cutoff, limit)).fetchall()
    if not rows:
        return 0

    payload = [dict(id=r["id"], channel=r["channel"], title=r["title"],
                    excerpt=(r["summary"] or "")[:200]) for r in rows]
    try:
        results = _call_llm(payload)
    except Exception as exc:  # noqa: BLE001
        if not _is_content_filter(exc):
            log.warning("AI 流水线失败: %s", exc)
            return 0
        # 整批被内容过滤拦截：逐条处理，被过滤的单条标记 -1 跳过，避免永远卡住
        log.info("批次触发内容过滤，降级为逐条处理")
        results = []
        skipped = []
        for single in payload:
            try:
                results.extend(_call_llm([single]))
            except Exception:  # noqa: BLE001 - 单条被过滤就跳过这条
                skipped.append(single["id"])
        if skipped:
            with get_db() as db:
                for item_id in skipped:
                    db.execute("UPDATE items SET score=-1 WHERE id=?", (item_id,))

    updated = 0
    with get_db() as db:
        for r in results:
            try:
                item_id = int(r["id"])
                score = max(0, min(100, int(r["score"])))
            except (KeyError, TypeError, ValueError):
                continue
            summary = (r.get("summary_zh") or "").strip()[:500]
            title_zh = (r.get("title_zh") or "").strip()[:120]
            event = r.get("event_type")
            row = next(x for x in rows if x["id"] == item_id)
            etype = (event if event in VALID_EVENTS else "") if row["channel"] == "stock" else ""
            if row["event_type"]:  # 官方文件已有精确类型，不覆盖
                etype = row["event_type"]
            db.execute("UPDATE items SET score=?, summary=?, title_zh=?, event_type=? WHERE id=?",
                       (score, summary or row["summary"], title_zh, etype, item_id))
            updated += 1
    return updated


def backfill_titles(days: int = 4, max_batches: int = 40) -> int:
    """给最近 days 天已入库但缺中文标题的条目补翻译，返回更新条数。"""
    if not config.llm_enabled():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=120)
    total = 0
    for _ in range(max_batches):
        with get_db() as db:
            rows = db.execute(
                """SELECT id, title, summary FROM items
                   WHERE title_zh='' AND published_at >= ?
                   ORDER BY id DESC LIMIT 40""", (cutoff,)).fetchall()
        if not rows:
            break
        payload = [dict(id=r["id"], title=r["title"], excerpt=(r["summary"] or "")[:120])
                   for r in rows]
        try:
            results = _call_llm_titles(payload)
        except Exception as exc:  # noqa: BLE001
            if _is_content_filter(exc):
                # 单条兜底：被过滤的条目标记「无需翻译」避免卡死
                results, filtered = [], []
                for single in payload:
                    try:
                        results.extend(_call_llm_titles([single]))
                    except Exception:  # noqa: BLE001
                        filtered.append(single["id"])
                with get_db() as db:
                    for item_id in filtered:
                        db.execute("UPDATE items SET title_zh='-' WHERE id=?", (item_id,))
            else:
                log.warning("标题补翻失败: %s", exc)
                break
        with get_db() as db:
            for r in results:
                try:
                    item_id = int(r["id"])
                    title_zh = (r.get("title_zh") or "").strip()[:120]
                except (KeyError, TypeError, ValueError):
                    continue
                if title_zh:
                    db.execute("UPDATE items SET title_zh=? WHERE id=?", (title_zh, item_id))
                    total += 1
    return total


def _call_llm_titles(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=120)
    resp = client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
        messages=[
            {"role": "system",
             "content": ("把每条新闻标题处理成简洁中文标题（title_zh）：英文翻译，中文润色。"
                         "保留公司名、数字、关键实体。只输出 JSON 数组："
                         '[{"id": 1, "title_zh": "…"}]')},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    return _extract_json(resp.choices[0].message.content or "")
