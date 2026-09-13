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

SYSTEM_PROMPT = """你是科技与财经资讯编辑，为 TMT 信息聚合站处理条目。对输入的每条资讯输出：
1. title_zh：中文标题。英文标题翻译成简洁的中文新闻标题；中文标题精简润色，保留关键实体与数字。
2. tmt：布尔值。是否属于 TMT（科技/传媒/电信）领域——AI、机器人、半导体、智能硬件、互联网大厂、软件与云、智能汽车、电信、科技公司动态与股价事件算 true；宏观大盘、地缘政治、民生医疗、体育娱乐、农产品能源等传统行情算 false（政策/监管仅当直接围绕科技行业或科技公司时才算 true，如「美司法部审查英伟达并购」算，「美联储加息预期」不算）。
3. score：0-100 重要性评分。对科技行业格局、相关公司股价影响越大分越高（重大财报/并购/监管/重磅产品 80-100，一般行业动态 50-79，例行文件/水文/软文 <50）。
4. summary_zh：用简体中文写 1-2 句摘要。英文内容翻译提炼，中文内容直接浓缩。客观、信息密度优先，别写成广告腔。
5. reason：一句话推荐理由（30-60 字），说明这条为什么值得看、事件的影响是什么。非 TMT 条目给空字符串。
6. event_type：仅当该条 channel 为 "stock" 时，从 [earnings,insider,buyback,ma,personnel,product,regulation,rating,offering,other] 选最贴切的一个；其余情况给 null。
7. ai_cat：仅当该条 channel 为 "ai" 且 tmt 为 true 时，从 [model, product, industry, paper, opinion] 选最贴切的一个（大模型发布/更新=model，应用与硬件产品=product，行业格局/投融资/政策=industry，论文与技术突破=paper，观点/评论=opinion）；其余情况给 null。

只输出 JSON 数组，不要任何其他文字：
[{"id": 1, "title_zh": "…", "tmt": true, "score": 78, "summary_zh": "...", "reason": "...", "event_type": null, "ai_cat": null}]"""


def _extract_json(text: str):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("响应里没有 JSON 数组")
    result = json.loads(text[start:end + 1])
    if not isinstance(result, list):
        raise ValueError("响应必须是 JSON 数组")
    return result


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _valid_results(results, rows):
    """Only accept unique IDs from this batch; never trust model-provided IDs."""
    allowed = {row["id"] for row in rows}
    seen = set()
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        item_id = result.get("id")
        if type(item_id) is not int or item_id not in allowed or item_id in seen:
            continue
        seen.add(item_id)
        yield result


def _category(result, row):
    value = result.get("ai_cat")
    return value if row["channel"] == "ai" and isinstance(value, str) and value in {
        "model", "product", "industry", "paper", "opinion"} else ""


def _keep_tmt(result, row):
    return int(bool(row["official"] or row["companies"] != "[]" or result["tmt"]))


def _call_llm(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
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
    """处理未评分条目（新旧条目按配额处理，避免旧条目被持续的新消息挤出），返回成功更新条数。"""
    if not config.llm_enabled():
        return 0
    limit = max(1,min(limit,100))
    with get_db() as db:
        query = """SELECT id,title,summary,raw_summary,channel,event_type,official,companies
                   FROM items WHERE score IS NULL ORDER BY id {} LIMIT ?"""
        # Reserve a quarter of each batch for the oldest backlog, while keeping
        # most capacity for fresh news. A busy feed cannot starve old items.
        oldest = db.execute(query.format('ASC'),(max(1,limit//4),)).fetchall()
        latest = db.execute(query.format('DESC'),(limit,)).fetchall()
        rows = list({r['id']:r for r in oldest+latest}.values())[:limit]
    if not rows:
        return 0

    payload = [dict(id=r["id"], channel=r["channel"], title=r["title"],
                    excerpt=(r["raw_summary"] if r["raw_summary"] is not None else r["summary"] or "")[:200]) for r in rows]
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
            except Exception as single_exc:
                if _is_content_filter(single_exc):
                    skipped.append(single["id"])
                else:
                    log.warning("AI 单条暂时失败，保留待重试: %s", type(single_exc).__name__)
        if skipped:
            with get_db() as db:
                for item_id in skipped:
                    db.execute("UPDATE items SET score=-1 WHERE id=?", (item_id,))

    updated = 0
    with get_db() as db:
        for r in _valid_results(results, rows):
            if type(r.get("tmt")) is not bool or type(r.get("score")) is not int:
                continue
            try:
                item_id = int(r["id"])
                score = max(0, min(100, int(r["score"])))
            except (KeyError, TypeError, ValueError):
                continue
            summary = _text(r.get("summary_zh"), 500)
            title_zh = _text(r.get("title_zh"), 120)
            event = r.get("event_type")
            row = next(x for x in rows if x["id"] == item_id)
            etype = (event if isinstance(event, str) and event in VALID_EVENTS else "") if row["channel"] == "stock" else ""
            if row["event_type"]:  # 官方文件已有精确类型，不覆盖
                etype = row["event_type"]
            db.execute(
                """UPDATE items SET score=?, summary=?, title_zh=?, event_type=?,
                                      tmt=?, reason=?, ai_cat=? WHERE id=?""",
                (score, summary or row["summary"], title_zh, etype,
                 _keep_tmt(r, row),
                 _text(r.get("reason"), 200),
                 _category(r, row),
                 item_id))
            updated += 1
    return updated


def backfill_tmt(days: int = 0, max_batches: int = 60) -> int:
    """给未判定 TMT 的条目补判定（+推荐理由/子分类），默认不限时间，返回处理条数。"""
    if not config.llm_enabled():
        return 0
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
    total = 0
    for _ in range(max_batches):
        with get_db() as db:
            sql = """SELECT id, title, title_zh, summary, channel, event_type, official, companies FROM items
                     WHERE tmt IS NULL"""
            params: list = []
            if days:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                sql += " AND published_at >= ?"
                params.append(cutoff)
            sql += " ORDER BY id DESC LIMIT 40"
            rows = db.execute(sql, params).fetchall()
        if not rows:
            break
        payload = [dict(id=r["id"], channel=r["channel"],
                        title=r["title_zh"] or r["title"],
                        excerpt=(r["summary"] or "")[:120]) for r in rows]
        try:
            results = _call_llm_tmt(payload)
        except Exception as exc:  # noqa: BLE001
            if _is_content_filter(exc):
                results, filtered = [], []
                for single in payload:
                    try:
                        results.extend(_call_llm_tmt([single]))
                    except Exception as single_exc:
                        if _is_content_filter(single_exc):
                            filtered.append(single["id"])
                with get_db() as db:  # 无法判定的按官方/公司关联兜底，否则按非 TMT 隐藏
                    for item_id in filtered:
                        row = next(x for x in rows if x["id"] == item_id)
                        keep = 1 if (row["official"] or row["companies"] != "[]") else 0
                        db.execute("UPDATE items SET tmt=? WHERE id=?", (keep, item_id))
                        total += 1
            else:
                log.warning("TMT 补判定失败: %s", exc)
                break
        with get_db() as db:
            for r in _valid_results(results, rows):
                try:
                    item_id = int(r["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                row = next((x for x in rows if x["id"] == item_id), None)
                if not row:
                    continue
                if type(r.get("tmt")) is not bool:
                    continue
                tmt = _keep_tmt(r, row)
                # 官方文件与公司关联条目是盯盘刚需，即使 LLM 判否也保留
                if row["official"] or row["companies"] != "[]":
                    tmt = 1
                db.execute("UPDATE items SET tmt=?, reason=?, ai_cat=? WHERE id=?",
                           (tmt, _text(r.get("reason"), 200),
                            _category(r, row),
                            item_id))
                total += 1
    return total


def _call_llm_tmt(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
    resp = client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
        messages=[
            {"role": "system",
             "content": ("判断每条资讯是否属于 TMT（科技/传媒/电信）领域，并给出一句话推荐理由。"
                         "tmt=true 的条件：AI、机器人、半导体、智能硬件、互联网大厂、软件云、智能汽车、"
                         "电信、科技公司动态；宏观/地缘/民生/传统行业行情=false。"
                         '只输出 JSON 数组：[{"id": 1, "tmt": true, "reason": "…", "ai_cat": null}]'
                         "（ai_cat 仅当明显是 AI 资讯时从 [model,product,industry,paper,opinion] 选，否则 null）")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    return _extract_json(resp.choices[0].message.content or "")


def backfill_titles(days: int = 4, max_batches: int = 40) -> int:
    """给最近 days 天已入库但缺中文标题的条目补翻译，返回更新条数。"""
    if not config.llm_enabled():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
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
                    except Exception as single_exc:
                        if _is_content_filter(single_exc):
                            filtered.append(single["id"])
                with get_db() as db:
                    for item_id in filtered:
                        db.execute("UPDATE items SET title_zh='-' WHERE id=?", (item_id,))
            else:
                log.warning("标题补翻失败: %s", exc)
                break
        with get_db() as db:
            for r in _valid_results(results, rows):
                try:
                    item_id = int(r["id"])
                    title_zh = _text(r.get("title_zh"), 120)
                except (KeyError, TypeError, ValueError):
                    continue
                if title_zh:
                    db.execute("UPDATE items SET title_zh=? WHERE id=?", (title_zh, item_id))
                    total += 1
    return total


def _call_llm_titles(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
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
