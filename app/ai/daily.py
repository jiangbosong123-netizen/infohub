from __future__ import annotations

"""每日日报：汇总指定日期的高分条目与热点簇，LLM 生成（无 Key 时退化为结构化摘要）。"""
import json
import logging
from datetime import datetime, timedelta

import markdown as md

from .. import config, ranking
from ..config import APP_TZ
from ..database import get_db

log = logging.getLogger(__name__)

CHANNEL_NAMES = {"ai": "AI", "robot": "机器人", "stock": "股市 · 科技企业"}
EVENT_NAMES = {
    "earnings": "财报", "insider": "内部人交易", "buyback": "回购", "ma": "并购",
    "personnel": "人事", "product": "产品", "regulation": "监管", "rating": "评级",
    "offering": "发行融资", "other": "其他",
}


def _day_bounds(date_str: str) -> tuple[str, str]:
    d0 = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=APP_TZ)
    return d0.isoformat(), (d0 + timedelta(days=1)).isoformat()


def _collect(date_str: str) -> dict:
    start, end = _day_bounds(date_str)
    with get_db() as db:
        items = db.execute(
            """SELECT i.title, i.title_zh, i.summary, i.score, i.channel, i.event_type,
                      i.official, i.published_at, i.url, s.name AS source_name, i.companies
               FROM items i JOIN sources s ON s.id = i.source_id
               WHERE i.published_at >= ? AND i.published_at < ?
               ORDER BY COALESCE(i.score, 50) DESC, i.heat DESC LIMIT 120""",
            (start, end)).fetchall()
        clusters = db.execute(
            """SELECT title, url, heat, source_count, company_slugs, channel
               FROM clusters WHERE updated_at >= ? AND updated_at < ? AND source_count >= 2
               ORDER BY heat DESC LIMIT 12""", (start, end)).fetchall()
    out_items = []
    for r in items:
        d = dict(r)
        d["title"] = r["title_zh"] or r["title"]  # 优先中文标题
        d.pop("title_zh", None)
        d["time"] = datetime.fromisoformat(r["published_at"]).astimezone(APP_TZ).strftime("%H:%M")
        out_items.append(d)
    return dict(items=out_items, clusters=[dict(r) for r in clusters])


def _digest_fallback(date_str: str, data: dict) -> str:
    """无 LLM 时的结构化摘要（纯聚合模式也能看）。"""
    lines = [f"# 行业日报 · {date_str}", ""]
    if data["clusters"]:
        lines += ["## 今日热点榜", ""]
        for i, c in enumerate(data["clusters"][:10], 1):
            ch = CHANNEL_NAMES.get(c["channel"], c["channel"])
            lines.append(f"{i}. **[{ch}]** [{c['title']}]({c['url']})"
                         f" — {c['source_count']} 家信源 · 热度 {int(c['heat'] * 100)}")
        lines.append("")
    by_channel: dict[str, list] = {}
    for it in data["items"]:
        by_channel.setdefault(it["channel"], []).append(it)
    for ch in ("stock", "ai", "robot"):
        its = by_channel.get(ch) or []
        if not its:
            continue
        lines += [f"## {CHANNEL_NAMES.get(ch, ch)}", ""]
        for it in its[:20]:
            hm = datetime.fromisoformat(it["published_at"]).astimezone(APP_TZ).strftime("%H:%M")
            tag = f"（{EVENT_NAMES.get(it['event_type'], '')}）" if it["event_type"] else ""
            score = f" · 评分 {it['score']}" if it["score"] is not None else ""
            lines.append(f"- **{hm}** [{it['title']}]({it['url']}) — {it['source_name']}{score}{tag}")
            if it["summary"]:
                lines.append(f"  {it['summary']}")
        lines.append("")
    return "\n".join(lines)


def generate_daily(date_str: str | None = None) -> str | None:
    """生成（或覆盖）某日日报，返回日期。默认覆盖「昨天」。"""
    date_str = date_str or (datetime.now(APP_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    data = _collect(date_str)
    if not data["items"] and not data["clusters"]:
        log.info("%s 无数据，跳过日报", date_str)
        return None

    if config.llm_enabled():
        try:
            content = _llm_report(date_str, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 日报失败，退化为摘要: %s", exc)
            content = _digest_fallback(date_str, data)
    else:
        content = _digest_fallback(date_str, data)

    with get_db() as db:
        db.execute(
            """INSERT INTO daily_reports (date, content, created_at) VALUES (?,?,?)
               ON CONFLICT(date) DO UPDATE SET content=excluded.content,
                                              created_at=excluded.created_at""",
            (date_str, content, datetime.now(APP_TZ).isoformat()))
    return date_str


def _llm_report(date_str: str, data: dict) -> str:
    from openai import OpenAI
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=180)
    prompt = (
        f"根据以下 {date_str} 的原始素材写一份中文行业日报（Markdown）。"
        "结构：一句话总览；三个板块（股市·科技企业 / AI / 机器人），每板块挑最重要的若干条，"
        "每条格式为「**HH:MM** 标题 — 一句话点评（说明为什么重要）」，时间用素材里的 time 字段，"
        "标题直接用素材里的中文标题；最后加「值得关注」一节列 2-3 个后续观察点。"
        "语言精炼，别堆砌，总长 800 字以内。\n\n素材：\n"
        + json.dumps(data, ensure_ascii=False))
    resp = client.chat.completions.create(
        model=config.LLM_MODEL, temperature=0.4,
        messages=[{"role": "user", "content": prompt}])
    text = resp.choices[0].message.content or ""
    return text.strip() or _digest_fallback(date_str, data)


def render_markdown(text: str) -> str:
    return md.markdown(text, extensions=["tables", "fenced_code"])
