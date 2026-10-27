"""Same-event pairs among the hot stories, judged by the model (10-21).

Story matching is lexical (stories.py, titles-v5). Two reports of one event in other words, such
as "Joel Kaplan leaving Meta after 15 years" and "Meta's Joel Kaplan to step down next year", stay
two stories, and on 10-20 four events filled two or three places each in the hot list's top 30.
The worker asks the model about pairs of hot stories that share a company and began within 48
hours of each other; each answer is kept, so a pair is asked once. The hot list then shows a pair
judged the same as one entry. Stories are not merged: a story_id stays what consumers count.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from itertools import combinations

from . import config
from .database import get_db

log = logging.getLogger(__name__)

HOT_LIMIT = 40
WINDOW = timedelta(days=2)
PAIR_HOURS = 48
MAX_PAIRS = 60  # per run; the rest wait for the next one
PROMPT = """判断每一对新闻是否在报道同一件具体的事：同一主体的同一个动作或事件，例如同一笔交易、同一次人事变动、
同一次故障、同一份文件的披露、同一次发布。同一件事的后续进展（如交易从传闻到确认）算同一件。
同一家公司的两件不同的事（两笔不同的交易、两款不同的产品、两份不同的报告）不算。拿不准算不是。
只输出 JSON 数组，不要任何其他文字：[{"pair": 1, "same": true}]"""


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hot_stories(db, limit: int = HOT_LIMIT, now: datetime | None = None) -> list[dict]:
    """The hot list's stories in its own order (multi-source first, then heat)."""
    cutoff = ((now or datetime.now(timezone.utc)) - WINDOW).isoformat()
    rows = db.execute(
        """SELECT st.id, st.title, st.first_at, st.company_slugs, i.summary AS anchor_summary FROM stories st
           LEFT JOIN items i ON i.id=st.anchor_item_id
           WHERE st.redirect_to IS NULL AND st.item_count>0 AND st.last_at>=?
           ORDER BY (st.source_count>=2) DESC, st.heat DESC, st.id LIMIT ?""", (cutoff, limit)).fetchall()
    return [dict(row) for row in rows]


def _key(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first < second else (second, first)


def candidate_pairs(db, stories: list[dict]) -> list[tuple[dict, dict]]:
    """Unjudged pairs that share a company and began within PAIR_HOURS, best-ranked first."""
    judged = {(row[0], row[1]) for row in db.execute("SELECT story_a, story_b FROM story_pair_judgements")}
    pairs = []
    for left, right in combinations(stories, 2):
        if _key(left["id"], right["id"]) in judged:
            continue
        if not set(json.loads(left["company_slugs"] or "[]")) & set(json.loads(right["company_slugs"] or "[]")):
            continue
        if abs((_time(left["first_at"]) - _time(right["first_at"])).total_seconds()) > PAIR_HOURS * 3600:
            continue
        pairs.append((left, right))
    return pairs[:MAX_PAIRS]


def _ask(pairs: list[tuple[dict, dict]]) -> dict[int, bool]:
    from openai import OpenAI

    from .ai.usage import call

    def side(story):
        return {"title": story["title"], "time": story["first_at"][:16],
                "summary": (story.get("anchor_summary") or "")[:120]}

    payload = [{"pair": number, "a": side(left), "b": side(right)}
               for number, (left, right) in enumerate(pairs, 1)]
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=120)
    response = call("story_pairs", lambda: client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.1, extra_body={"thinking": {"type": "disabled"}},
        messages=[{"role": "system", "content": PROMPT},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]), items=len(pairs))
    text = response.choices[0].message.content or ""
    answers = json.loads(text[text.find("["):text.rfind("]") + 1])
    return {entry["pair"]: entry["same"] for entry in answers if isinstance(entry, dict)
            and type(entry.get("pair")) is int and 1 <= entry["pair"] <= len(pairs)
            and type(entry.get("same")) is bool}


def refresh(now: datetime | None = None) -> dict:
    if not config.llm_enabled():
        return {"skipped": "no model"}
    with get_db() as db:
        pairs = candidate_pairs(db, hot_stories(db, now=now))
    if not pairs:
        return {"asked": 0, "same": 0}
    try:
        answers = _ask(pairs)
    except (ValueError, KeyError, TypeError) as exc:  # an unreadable answer: ask again next run
        log.warning("hot pairs: unreadable answer (%s)", type(exc).__name__)
        return {"asked": len(pairs), "same": 0, "unreadable": True}
    except Exception as exc:  # noqa: BLE001 - the model is down or out of balance: just skip this run
        # On 10-27 an empty balance failed this job 34 times a day as "retries exhausted", next to
        # the ai_stalled alert that already says why. The hot list only stays unfolded meanwhile.
        from .ai.usage import failure_kind
        kind = failure_kind(exc)
        log.warning("hot pairs: model unavailable (%s), trying next run", kind)
        return {"asked": len(pairs), "same": 0, "model_error": kind}
    judged_at = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        for number, (left, right) in enumerate(pairs, 1):
            if number in answers:
                db.execute("""INSERT OR IGNORE INTO story_pair_judgements(story_a,story_b,same,judged_at,model)
                              VALUES(?,?,?,?,?)""", (*_key(left["id"], right["id"]), int(answers[number]),
                                                     judged_at, config.LLM_MODEL or ""))
    return {"asked": len(pairs), "answered": len(answers), "same": sum(answers.values())}


def same_event_groups(db, story_ids: list[str]) -> dict[str, str]:
    """For each story, the first story of ``story_ids`` (in order) judged the same event."""
    if len(story_ids) < 2:
        return {}
    marks = ",".join("?" * len(story_ids))
    pairs = db.execute(
        f"""SELECT story_a, story_b FROM story_pair_judgements
            WHERE same=1 AND story_a IN ({marks}) AND story_b IN ({marks})""", (*story_ids, *story_ids)).fetchall()
    order = {story_id: index for index, story_id in enumerate(story_ids)}
    parent = {story_id: story_id for story_id in story_ids}

    def root(story_id):
        while parent[story_id] != story_id:
            parent[story_id] = parent[parent[story_id]]
            story_id = parent[story_id]
        return story_id

    for first, second in pairs:
        a, b = root(first), root(second)
        if a != b:
            keep, drop = (a, b) if order[a] < order[b] else (b, a)
            parent[drop] = keep
    return {story_id: root(story_id) for story_id in story_ids}
