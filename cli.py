from __future__ import annotations

#!/usr/bin/env python3
"""命令行工具：

  python cli.py init-db     # 建表 + 导入 watchlist 公司 + 注册信息源
  python cli.py crawl       # 立即抓一轮所有到期源（首跑灌数据）
  python cli.py reconcile   # 立即跑一次 Google News 对账补漏
  python cli.py ai          # 对未处理条目跑一轮 AI（摘要/评分）
  python cli.py report [YYYY-MM-DD]  # 生成某日日报（默认昨天）
  python cli.py reindex     # 更新主题索引与持久事件（不调用模型）
  python cli.py db-status [PATH]       # 只读检查数据库版本与完整性
  python cli.py db-backup [DEST]       # 创建并校验一致性备份
  python cli.py db-migrate             # 仅执行安全迁移（旧库会先备份）
  python cli.py db-verify [PATH]       # 严格验证当前版本数据库
  python cli.py runtime-config         # 显示当前环境、角色与数据路径（不含密钥）
  python cli.py jobs-status            # 显示持久任务各状态数量
  python cli.py dataset-status         # 显示数据集、epoch 与变化高水位
  python cli.py dataset-new-epoch EXPECTED_EPOCH REASON  # 恢复后切换同步代际
  python cli.py serve                  # 只启动网站，不建库、不迁移、不抓取
  python cli.py worker                 # 启动持久任务调度与执行进程
  python cli.py worker-health          # 检查当前版本 worker 心跳
  python cli.py prepare-release        # 安全迁移、同步静态配置并清除旧心跳
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


def cmd_db_status(path: str | None = None) -> None:
    from app.db_admin import report_json, verify_database
    print(report_json(verify_database(path or config.DB_PATH)))


def cmd_db_backup(destination: str | None = None) -> None:
    from app.db_admin import backup_database, report_json
    print(report_json(backup_database(destination=destination)))


def cmd_db_migrate() -> None:
    from app.db_admin import migrate_database, report_json
    print(report_json(migrate_database()))


def cmd_db_verify(path: str | None = None) -> None:
    from app.db_admin import report_json, verify_database
    print(report_json(verify_database(path or config.DB_PATH, require_current=True)))


def cmd_jobs_status() -> None:
    from app.db_admin import verify_database
    from app.jobs import job_counts
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps({
        "enabled": config.DURABLE_JOBS_ENABLED,
        "states": job_counts(),
    }, ensure_ascii=False, indent=2))


def cmd_dataset_status() -> None:
    from app.db_admin import verify_database
    from app.publication import get_dataset_identity
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(get_dataset_identity().to_dict(), ensure_ascii=False, indent=2))


def cmd_dataset_new_epoch(expected_epoch: str, reason: str) -> None:
    from app.publication import rotate_dataset_epoch
    result = rotate_dataset_epoch(expected_epoch=expected_epoch, reason=reason)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_crawl() -> None:
    config.require_network_tasks("crawl")
    from app.crawler.runner import run_due_sources
    result = run_due_sources()
    print(f"本轮抓取 {result['ran']} 个源。")
    for r in result["results"]:
        if "error" in r:
            print(f"  ✗ {r['key']}: {r['error']}")
        else:
            print(f"  ✓ {r['key']}: 新增 {r['inserted']} 条")


def cmd_reconcile() -> None:
    config.require_network_tasks("reconcile")
    from app.crawler.googlenews import run_reconcile
    stats = run_reconcile()
    print("对账完成（fetched=命中新闻 inserted=补录条数 media_24h=常规源24h条数）：")
    for slug, s in stats.items():
        if "error" in s:
            print(f"  {slug}: 抓取失败 {s['error'][:80]}")
        else:
            print(f"  {slug}: fetched={s['fetched']} inserted={s['inserted']} media_24h={s['media_24h']}")


def cmd_ai() -> None:
    config.require_network_tasks("ai")
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
    config.require_network_tasks("report")
    from app.ai.daily import generate_daily
    d = generate_daily(date)
    print(f"日报已生成：{d}" if d else "当天没有数据，未生成。")


def prune_fetch_logs() -> int:
    """每日清理：fetch_log 只留 14 天（items 长期保留）。"""
    from datetime import datetime, timedelta, timezone
    from app.database import get_db
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    with get_db() as db:
        n = db.execute("DELETE FROM fetch_log WHERE ran_at < ?", (cutoff,)).rowcount
    if n:
        print(f"已清理 {n} 条过期抓取日志")
    return n


def cmd_serve() -> None:
    if config.PROCESS_ROLE != "web":
        raise config.RuntimeConfigurationError(
            "serve command requires INFOHUB_PROCESS_ROLE=web"
        )
    from app.db_admin import verify_database
    verify_database(config.DB_PATH, require_current=True)
    import uvicorn
    from app.web.routes import app
    print(f"运行环境：{config.ENVIRONMENT_ID} ({config.ENVIRONMENT}) · "
          f"角色：{config.PROCESS_ROLE} · 数据库：{config.DB_PATH}")
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")


def cmd_worker_health() -> None:
    from app.runtime_health import read_worker_heartbeat
    status = read_worker_heartbeat()
    print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
    if not status.healthy:
        raise SystemExit(1)


def cmd_prepare_release() -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "prepare-release requires INFOHUB_PROCESS_ROLE=maintenance"
        )
    cmd_init_db()
    from app.runtime_health import clear_worker_heartbeat
    clear_worker_heartbeat()
    print("发布准备完成：数据库已验证，旧 worker 心跳已清除。")


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
    elif cmd == "db-status":
        cmd_db_status(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "db-backup":
        cmd_db_backup(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "db-migrate":
        cmd_db_migrate()
    elif cmd == "db-verify":
        cmd_db_verify(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "runtime-config":
        print(json.dumps(config.RUNTIME.public_manifest(), ensure_ascii=False, indent=2))
    elif cmd == "jobs-status":
        cmd_jobs_status()
    elif cmd == "dataset-status":
        cmd_dataset_status()
    elif cmd == "dataset-new-epoch" and len(sys.argv) >= 4:
        cmd_dataset_new_epoch(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "serve":
        cmd_serve()
    elif cmd == "worker":
        from app.worker import run_worker
        run_worker()
    elif cmd == "worker-health":
        cmd_worker_health()
    elif cmd == "prepare-release":
        cmd_prepare_release()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except config.RuntimeConfigurationError as exc:
        print(f"运行配置错误：{exc}", file=sys.stderr)
        sys.exit(2)
