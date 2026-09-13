from __future__ import annotations

#!/usr/bin/env python3
"""命令行工具：

  python cli.py init-db     # 建表 + 导入 watchlist 公司 + 注册信息源
  python cli.py crawl       # 立即抓一轮所有到期源（首跑灌数据）
  python cli.py reconcile   # 立即跑一次 Google News 对账补漏
  python cli.py ai          # 对未处理条目跑一轮 AI（摘要/评分）
  python cli.py report [YYYY-MM-DD]  # 生成某日日报（默认昨天）
  python cli.py reindex     # 更新主题索引与持久事件（不调用模型）
  python cli.py serve       # 启动网站 + 定时任务
"""
import json
import logging
import sys

import yaml

from app import company_match, config
from app.database import get_db, init_schema


def _load_companies() -> int:
    from pathlib import Path
    data = yaml.safe_load(config.WATCHLIST_PATH.read_text(encoding="utf-8"))
    n = 0
    with get_db() as db:
        for c in data.get("companies", []):
            db.execute(
                """INSERT INTO companies (slug, name, name_zh, ticker, code, market, aliases)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(slug) DO UPDATE SET name=excluded.name,
                       name_zh=excluded.name_zh, ticker=excluded.ticker, code=excluded.code,
                       market=excluded.market, aliases=excluded.aliases""",
                (c["slug"], c["name"], c.get("name_zh", ""), c.get("ticker", ""),
                 c.get("code", ""), c["market"], json.dumps(c.get("aliases", []), ensure_ascii=False)))
            n += 1
    company_match.invalidate_cache()
    return n


def cmd_init_db() -> None:
    init_schema()
    from app.crawler.runner import upsert_sources
    n = _load_companies()
    upsert_sources()
    from app.stories import refresh_derived
    refresh_derived()
    print(f"数据库初始化完成：{n} 家公司，源注册表已同步。")


def cmd_crawl() -> None:
    from app.crawler.runner import run_due_sources
    result = run_due_sources()
    print(f"本轮抓取 {result['ran']} 个源。")
    for r in result["results"]:
        if "error" in r:
            print(f"  ✗ {r['key']}: {r['error']}")
        else:
            print(f"  ✓ {r['key']}: 新增 {r['inserted']} 条")


def cmd_reconcile() -> None:
    from app.crawler.googlenews import run_reconcile
    stats = run_reconcile()
    print("对账完成（fetched=命中新闻 inserted=补录条数 media_24h=常规源24h条数）：")
    for slug, s in stats.items():
        if "error" in s:
            print(f"  {slug}: 抓取失败 {s['error'][:80]}")
        else:
            print(f"  {slug}: fetched={s['fetched']} inserted={s['inserted']} media_24h={s['media_24h']}")


def cmd_ai() -> None:
    from app.ai.pipeline import backfill_titles, backfill_tmt, process_pending
    if not config.llm_enabled():
        print("未配置 LLM（.env 里的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），跳过。")
        return
    total, rounds = 0, 0
    while rounds < 10:
        n = process_pending(limit=20)
        total += n
        rounds += 1
        if n < 20:
            break
    print(f"AI 处理完成，本轮更新 {total} 条。")
    translated = backfill_titles()
    print(f"标题补翻 {translated} 条。")
    judged = backfill_tmt()
    print(f"TMT 补判定 {judged} 条。")
    from app.stories import refresh_derived
    refresh_derived()


def cmd_report(date: str | None) -> None:
    from app.ai.daily import generate_daily
    d = generate_daily(date)
    print(f"日报已生成：{d}" if d else "当天没有数据，未生成。")


def _prune_logs() -> None:
    """每日清理：fetch_log 只留 14 天（items 长期保留）。"""
    from datetime import datetime, timedelta, timezone
    from app.database import get_db
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    with get_db() as db:
        n = db.execute("DELETE FROM fetch_log WHERE ran_at < ?", (cutoff,)).rowcount
    if n:
        print(f"已清理 {n} 条过期抓取日志")


def cmd_serve() -> None:
    cmd_init_db()
    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.ai.daily import generate_daily
    from app.ai.pipeline import process_pending
    from app.crawler.googlenews import run_reconcile
    from app.crawler.runner import run_due_sources
    from app.web.routes import app

    sched = BackgroundScheduler(timezone=str(config.APP_TZ),
                                job_defaults={"misfire_grace_time": 3600})  # Mac 睡醒后补跑错过的任务
    from datetime import datetime as _dt
    sched.add_job(run_due_sources, "interval", minutes=config.CRAWL_TICK_MINUTES,
                  id="crawl", max_instances=1, coalesce=True,
                  next_run_time=_dt.now(config.APP_TZ))  # 启动即抓一轮
    def _ai_tick() -> None:
        # 清空式处理：把积压全部清完再休息，避免抓取高峰时翻译/过滤跟不上
        for _ in range(12):
            if process_pending(limit=30) < 30:
                break
        from app.ai.pipeline import backfill_tmt
        backfill_tmt(max_batches=12)   # 及时过滤掉非 TMT 噪声
        from app.ai.pipeline import backfill_titles
        backfill_titles(max_batches=12)
        from app.stories import refresh_derived
        refresh_derived()

    sched.add_job(_ai_tick, "interval", minutes=15,
                  id="ai", max_instances=1, coalesce=True)
    sched.add_job(run_reconcile, "cron", hour=config.RECONCILE_HOUR, minute=config.RECONCILE_MINUTE,
                  id="reconcile")
    sched.add_job(generate_daily, "cron", hour=config.REPORT_HOUR, minute=config.REPORT_MINUTE,
                  id="report")
    sched.add_job(_prune_logs, "cron", hour=4, minute=5, id="prune")
    sched.start()
    print(f"定时任务已启动：抓取每 {config.CRAWL_TICK_MINUTES} 分钟 · AI 每 15 分钟 · "
          f"对账 {config.RECONCILE_HOUR:02d}:{config.RECONCILE_MINUTE:02d} · 日报 {config.REPORT_HOUR:02d}:{config.REPORT_MINUTE:02d}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=config.WEB_PORT, log_level="info")
    finally:
        sched.shutdown(wait=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init-db":
        cmd_init_db()
    elif cmd == "crawl":
        cmd_crawl()
    elif cmd == "reconcile":
        cmd_reconcile()
    elif cmd == "ai":
        cmd_ai()
    elif cmd == "report":
        cmd_report(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "reindex":
        from app.stories import refresh_derived
        init_schema()
        print(refresh_derived())
    elif cmd == "serve":
        cmd_serve()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
