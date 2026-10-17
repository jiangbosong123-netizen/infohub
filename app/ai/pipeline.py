from __future__ import annotations

"""AI 策展流水线：英文源翻译/浓缩为中文摘要 + 0-100 重要性评分 + 股市事件类型。

配置 .env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后启用；
未配置时 process_pending 直接返回 0，整站退化为纯聚合模式（热度算法照常工作）。
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from openai import OpenAI

from .. import config
from ..crawler.article_text import excerpts, waiting_for_text_clause
from ..database import get_db
from ..redaction import redact
from ..stories import is_stock_move_title
from . import usage

log = logging.getLogger(__name__)

VALID_EVENTS = {"earnings", "insider", "buyback", "ma", "personnel", "product",
                "regulation", "rating", "offering", "other"}

# The tmt field follows relevance-definition-v2 (D26): technology plus markets and macro.
SYSTEM_PROMPT = """你是科技与财经资讯编辑，为科技与财经信息聚合站处理条目。对输入的每条资讯输出：
1. title_zh：中文标题。英文标题翻译成简洁的中文新闻标题；中文标题精简润色，保留关键实体与数字。
2. tmt：布尔值。是否属于本站关注范围——科技（AI、机器人、半导体、智能硬件与消费电子、互联网平台、软件与云、智能汽车、电信，科技公司动态与股价事件，针对科技行业的政策监管）或财经与市场（货币政策与央行、宏观数据、大盘与指数、国债与汇率、能源与大宗商品、贸易与关税、财政政策，任何公司的财报、股价异动、并购融资，产业数据，会影响市场的地缘政治）算 true；不影响市场的政治军事新闻、社会民生、文娱体育游戏、购物促销与生活方式、医疗健康、只是与关注公司同名的内容（如苹果派、ARM 浮动利率房贷）算 false。
3. score：0-100 重要性评分。对科技行业格局、相关公司股价或整体市场影响越大分越高（重大财报/并购/监管/重磅产品、央行利率决议、关键宏观数据、重大地缘与能源冲击 80-100，一般行业与市场动态 50-79，例行行情波动与数据、例行文件、水文软文 <50）。
4. summary_zh：用简体中文写 1-2 句摘要。英文内容翻译提炼，中文内容直接浓缩。客观、信息密度优先，别写成广告腔。
5. reason：一句话推荐理由（30-60 字），说明这条为什么值得看、事件的影响是什么。tmt 为 false 的条目给空字符串。
6. event_type：仅当该条 channel 为 "stock" 时，从 [earnings,insider,buyback,ma,personnel,product,regulation,rating,offering,other] 选最贴切的一个；其余情况给 null。
7. ai_cat：仅当该条 channel 为 "ai" 且 tmt 为 true 时，从 [model, product, industry, paper, opinion] 选最贴切的一个（大模型发布/更新=model，应用与硬件产品=product，行业格局/投融资/政策=industry，论文与技术突破=paper，观点/评论=opinion）；其余情况给 null。
8. mood：整数 -2 到 2，这条消息对所涉公司、行业或市场是利空还是利好：明显利空 -2，偏利空 -1，中性或说不清 0，偏利好 1，明显利好 2。

只输出 JSON 数组，不要任何其他文字：
[{"id": 1, "title_zh": "…", "tmt": true, "score": 78, "summary_zh": "...", "reason": "...", "event_type": null, "ai_cat": null, "mood": 1}]"""


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


# A watched-company name keeps an item the model judged out of scope, unless the model also scored
# it at most this low: in the legacy data those were name collisions (an Apple festival, a hockey
# player named Claude, a game called Twin Oracle Point) and shopping roundups. Official filings are
# always kept; an item the model has not scored is kept until it is.
COMPANY_KEEP_MIN_SCORE = 31


def _keep_tmt(result, row, score=None) -> int:
    if row["official"] or result["tmt"]:
        return 1
    if row["companies"] != "[]":
        return int(score is None or score >= COMPANY_KEEP_MIN_SCORE)
    return 0


# An identical title within REUSE_HOURS, on the same channel and with the same company links, is
# the same article (a wire copy, one press release in several feeds): it takes the earlier item's
# judgement instead of another model call. In the legacy data 9.2% of non-official items were such
# repeats (some of them Google News copies, now stored once at ingestion); the model's own
# judgements of them differed by a median 5 points and in 50 of 8,125 relevance verdicts. 6,324
# of the 6,923 other repeats came within 6 hours. A longer window reached templates that repeat
# daily with new news ("Why is Arm stock sliding today?" on Monday and on Wednesday), whose
# summaries must not be shared; daily stock-move titles are never reused.
REUSE_HOURS = 6
REUSE_MIN_TITLE_CHARS = 12


def _reuse_judgements(db, rows) -> set[int]:
    """Give pending items the judgement of an earlier identical item; return the ids done."""
    reused = set()
    for row in rows:
        if (row["official"] or len(row["title"].strip()) < REUSE_MIN_TITLE_CHARS
                or is_stock_move_title(row["title"])):
            continue
        published = datetime.fromisoformat(str(row["published_at"]).replace("Z", "+00:00"))
        window = timedelta(hours=REUSE_HOURS)
        earlier = db.execute(
            """SELECT id,score,summary,title_zh,event_type,tmt,reason,ai_cat FROM items
               WHERE title=? AND id<>? AND official=0 AND channel=? AND companies=?
                 AND score>=0 AND tmt IS NOT NULL AND published_at BETWEEN ? AND ?
               ORDER BY id LIMIT 1""",
            (row["title"], row["id"], row["channel"], row["companies"],
             (published - window).isoformat(), (published + window).isoformat()),
        ).fetchone()
        if earlier is None:
            continue
        # Same channel, companies and official flag: _keep_tmt would give the same tmt.
        db.execute(
            """UPDATE items SET score=?, summary=?, title_zh=?, event_type=?, tmt=?, reason=?, ai_cat=?,
                      extra=json_set(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END,
                                     '$.ai_reused_from', ?)
               WHERE id=? AND score IS NULL""",
            (earlier["score"], earlier["summary"] or row["summary"], earlier["title_zh"],
             row["event_type"] or earlier["event_type"], earlier["tmt"], earlier["reason"],
             earlier["ai_cat"], earlier["id"], row["id"]),
        )
        db.execute("""INSERT OR REPLACE INTO item_moods(item_id,mood,judged_at)
                      SELECT ?,mood,judged_at FROM item_moods WHERE item_id=?""", (row["id"], earlier["id"]))
        reused.add(row["id"])
    return reused


def _call_llm(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
    resp = usage.call("score", lambda: client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},  # 结构化策展任务，关闭深度思考提速省钱
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]), items=len(payload))
    return _extract_json(resp.choices[0].message.content or "")


def _store_mood(db, item_id: int, mood) -> None:
    """The model's view of whether an item is good or bad news (-2..2), for the portal's mood face.
    A rough, unreviewed signal: it is not a tone result (D09, D23) and never feeds a publication."""
    if type(mood) is not int or not -2 <= mood <= 2:
        return
    db.execute("INSERT OR REPLACE INTO item_moods(item_id,mood,judged_at) VALUES(?,?,?)",
               (item_id, mood, datetime.now(timezone.utc).isoformat()))


def _is_content_filter(exc: Exception) -> bool:
    text = str(exc)
    return "1301" in text or "contentFilter" in text


def _judge(payload: list[dict], alone: bool = False) -> tuple[list, list[int]]:
    """Judge a batch; returns (results, ids the content filter refuses even alone).

    A content-filter refusal or an unreadable answer splits the batch in halves, so one bad item
    costs a few extra calls instead of the whole batch (or 30 single calls). Network, quota and
    authentication errors are not split: they would fail again, so the batch stays pending.
    An item is marked refused only after a call of its own was refused; a batch that was one item
    to begin with gets that call as a retry, since the filter also reacts to the model's answer.
    """
    try:
        return _call_llm(payload), []
    except Exception as exc:  # noqa: BLE001
        filtered = _is_content_filter(exc)
        if not filtered and not isinstance(exc, ValueError):
            log.warning("AI 流水线失败: %s", redact(str(exc)))
            return [], []
        if len(payload) == 1:
            if filtered and not alone:
                return _judge(payload, alone=True)
            if filtered:
                return [], [payload[0]["id"]]
            log.warning("AI 单条回答无法解析，保留待重试: %s", payload[0]["id"])
            return [], []
        log.info("AI 批次%s，拆成两半重试（%d 条）", "触发内容过滤" if filtered else "回答无法解析", len(payload))
        half = len(payload) // 2
        first, second = _judge(payload[:half], len(payload[:half]) == 1), _judge(payload[half:], len(payload[half:]) == 1)
        return first[0] + second[0], first[1] + second[1]


# Routine official filings (extra.routine: SEC insider forms 3/4/5/144, HKEX next-day returns) carry
# a Chinese title and summary from their connector and never reach the selected feed (D25). The
# model added little for them: 3,641 judged by 10-16 averaged 23 (max 55) with boilerplate reasons,
# yet they were a third of that day's calls. They now take a fixed judgement instead.
ROUTINE_SCORE = 25


def _rule_judgements(db, rows) -> set[int]:
    done = set()
    for row in rows:
        try:
            routine = bool(json.loads(row["extra"] or "{}").get("routine"))
        except (TypeError, ValueError, AttributeError):
            routine = False
        if not routine:
            continue
        db.execute(
            """UPDATE items SET score=?, tmt=1, reason='', ai_cat=NULL,
                      title_zh=CASE WHEN title_zh='' THEN title ELSE title_zh END,
                      extra=json_set(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END, '$.ai_rule', 'routine')
               WHERE id=? AND score IS NULL""",
            (ROUTINE_SCORE, row["id"]))
        done.add(row["id"])
    return done


def process_pending(limit: int = 20, parallel: int = 1) -> int:
    """处理未评分条目（新旧条目按配额处理，避免旧条目被持续的新消息挤出），返回成功更新条数。

    ``parallel`` batches of ``limit`` items go to the model at once; each is judged (and split on
    a content-filter refusal) on its own, so one slow or failing request does not hold the others.
    """
    if not config.llm_enabled():
        return 0
    batch = max(1, min(limit, 100))
    limit = batch * max(1, min(parallel, 8))
    # A title-only item waits a while for its article text (D27) so it is judged on more than its headline.
    waiting, params = waiting_for_text_clause(datetime.now(timezone.utc))
    with get_db() as db:
        query = ("SELECT i.id,i.title,i.summary,i.raw_summary,i.channel,i.event_type,i.official,i.companies,"
                 "i.published_at,i.extra FROM items i WHERE i.score IS NULL" + waiting + " ORDER BY i.id {} LIMIT ?")
        # Reserve a quarter of each batch for the oldest backlog, while keeping
        # most capacity for fresh news. A busy feed cannot starve old items.
        oldest = db.execute(query.format('ASC'),(*params, max(1,limit//4))).fetchall()
        latest = db.execute(query.format('DESC'),(*params, limit)).fetchall()
        rows = list({r['id']:r for r in oldest+latest}.values())[:limit]
        reused = _rule_judgements(db, rows)
        rows = [r for r in rows if r["id"] not in reused]
        reused |= _reuse_judgements(db, rows)
        rows = [r for r in rows if r["id"] not in reused]
        texts = excerpts(db, (r["id"] for r in rows))
    if not rows:
        return len(reused)

    payload = [dict(id=r["id"], channel=r["channel"], title=r["title"],
                    excerpt=texts.get(r["id"]) or (r["raw_summary"] if r["raw_summary"] is not None else r["summary"] or "")[:200])
               for r in rows]
    chunks = [payload[start:start + batch] for start in range(0, len(payload), batch)]
    if len(chunks) == 1:
        outcomes = [_judge(chunks[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(chunks), thread_name_prefix="ai-judge") as pool:
            outcomes = list(pool.map(_judge, chunks))
    # Each answer may only name items its own request carried: ids run on across chunk boundaries,
    # and a model that copies a neighbouring id must not overwrite another chunk's item.
    row_chunks = [rows[start:start + batch] for start in range(0, len(rows), batch)]
    results = [result for (judged, _), chunk in zip(outcomes, row_chunks)
               for result in _valid_results(judged, chunk)]
    skipped = [item_id for _, refused in outcomes for item_id in refused]
    if skipped:  # refused by the content filter even alone: mark -1 so it never blocks the queue
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
                 _keep_tmt(r, row, score),
                 _text(r.get("reason"), 200),
                 _category(r, row),
                 item_id))
            _store_mood(db, item_id, r.get("mood"))
            updated += 1
    return updated + len(reused)


def backfill_tmt(days: int = 0, max_batches: int = 60) -> int:
    """给未判定 TMT 的条目补判定（+推荐理由/子分类），默认不限时间，返回处理条数。"""
    if not config.llm_enabled():
        return 0
    total = 0
    for _ in range(max_batches):
        with get_db() as db:
            # Unscored items get tmt with their score (process_pending); judging them here too
            # would pay for the same verdict twice.
            sql = """SELECT id, title, title_zh, summary, channel, event_type, official, companies, score FROM items
                     WHERE tmt IS NULL AND score IS NOT NULL"""
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
                log.warning("TMT 补判定失败: %s", redact(str(exc)))
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
                # 官方文件始终保留；公司关联条目除非 LLM 判否且评分很低（多为同名误匹配）
                tmt = _keep_tmt(r, row, row["score"] if row["score"] is not None and row["score"] >= 0 else None)
                db.execute("UPDATE items SET tmt=?, reason=?, ai_cat=? WHERE id=?",
                           (tmt, _text(r.get("reason"), 200),
                            _category(r, row),
                            item_id))
                total += 1
    return total


def _call_llm_tmt(payload: list[dict]) -> list:
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
    resp = usage.call("tmt", lambda: client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
        messages=[
            {"role": "system",
             "content": ("判断每条资讯是否属于本站关注范围，并给出一句话推荐理由。"
                         "tmt=true：科技（AI、机器人、半导体、智能硬件、互联网平台、软件云、智能汽车、电信、"
                         "科技公司动态）或财经与市场（央行与宏观数据、大盘指数、国债汇率、能源大宗商品、贸易关税、"
                         "公司财报与股价异动、会影响市场的地缘政治）；不影响市场的政治军事、社会民生、文娱体育、"
                         "购物与生活方式、医疗健康、只是与关注公司同名的内容=false。"
                         '只输出 JSON 数组：[{"id": 1, "tmt": true, "reason": "…", "ai_cat": null}]'
                         "（ai_cat 仅当明显是 AI 资讯时从 [model,product,industry,paper,opinion] 选，否则 null）")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]), items=len(payload))
    return _extract_json(resp.choices[0].message.content or "")


def backfill_titles(days: int = 4, max_batches: int = 40) -> int:
    """给最近 days 天已评分但缺中文标题的条目补翻译，返回更新条数（未评分的条目随评分一起得到标题）。"""
    if not config.llm_enabled():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    total = 0
    for _ in range(max_batches):
        with get_db() as db:
            rows = db.execute(
                """SELECT id, title, summary FROM items
                   WHERE title_zh='' AND published_at >= ? AND score IS NOT NULL
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
                log.warning("标题补翻失败: %s", redact(str(exc)))
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
    resp = usage.call("title", lambda: client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
        messages=[
            {"role": "system",
             "content": ("把每条新闻标题处理成简洁中文标题（title_zh）：英文翻译，中文润色。"
                         "保留公司名、数字、关键实体。只输出 JSON 数组："
                         '[{"id": 1, "title_zh": "…"}]')},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]), items=len(payload))
    return _extract_json(resp.choices[0].message.content or "")
