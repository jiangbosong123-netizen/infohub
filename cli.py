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
  python cli.py db-bundle-backup [DEST] # 同批备份 SQLite 与其引用的 CAS 文件
  python cli.py db-bundle-verify PATH   # 校验完整备份包
  python cli.py db-bundle-restore BUNDLE DEST  # 恢复到全新隔离目录，不切换线上库
  python cli.py db-bundle-smoke PATH     # 用临时副本验收门户读路径
  python cli.py db-coverage [PATH]      # 只读统计旧数据到新模型的实际覆盖
  python cli.py db-legacy-compare BEFORE AFTER  # 核对迁移前后旧表的原有列和行
  python cli.py db-event-audit [PATH]   # 只读验收旧 story 的候选事件投影
  python cli.py db-curation-audit [PATH] # 只读验收旧 AI 字段的初始离线导入
  python cli.py db-projection-audit [PATH] # 只读验收门户搜索与热点派生投影
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
  python cli.py raw-verify             # 全量校验原始载荷 CAS 引用和哈希
  python cli.py evidence-verify        # 校验原文、NLP 和日报的全部 CAS 引用
  python cli.py legacy-backfill [N]    # 可续跑迁移旧记录，每事务批 N 条（maintenance only）
  python cli.py legacy-topic-backfill [N]  # 冻结并可续跑迁移旧主题归类（maintenance only）
  python cli.py topic-review-preview ASSIGNMENT_ID  # 查看主题断言当前人工决定
  python cli.py topic-review-queue [AFTER|none] [LIMIT] [TOPIC_ID|all]
  python cli.py topic-review-coverage
  python cli.py topic-review ASSIGNMENT_ID accepted|rejected EXPECTED_PREVIOUS|none REASON
  python cli.py topic-review-sample-create PER_TOPIC_LIMIT SEED
  python cli.py topic-review-sample-report BATCH_ID
  python cli.py topic-review-sample-queue BATCH_ID [AFTER_ORDINAL|none] [LIMIT] [pending|all]
  python cli.py topic-review-console BATCH_ID [PORT]  # 仅本机的逐条抽样审核页面
  python cli.py topic-sample-gate-preview BATCH_ID
  python cli.py topic-sample-gate-review BATCH_ID approved|rejected EXPECTED|none OVERALL_DECIDED_BPS TOPIC_DECIDED_BPS OVERALL_ACCEPTANCE_BPS TOPIC_ACCEPTANCE_BPS REASON
  python cli.py topic-statistics-advance [N]  # 推进至多 N 个主题统计（maintenance only，可续跑）
  python cli.py topic-admission-preview PUBLICATION_ID
  python cli.py topic-admission-review PUBLICATION_ID approved|rejected EXPECTED|none MIN_BPS true|false SAMPLE_EVALUATION|none REASON
  python cli.py legacy-event-project   # 将旧 story 映射为 shadow candidate event（maintenance only）
  python cli.py legacy-curation-enqueue [AFTER_ID] [LIMIT]  # 分页排入旧策展转换任务（maintenance only）
  python cli.py legacy-curation-process [N]  # 处理最多 N 个离线转换任务（maintenance only）
  python cli.py curation-search-advance [N]  # 建立/刷新至多 N 条搜索文档（maintenance only，可续跑）
  python cli.py curation-hot-advance [N]     # 建立/刷新至多 N 个热点统计（maintenance only，可续跑）
  python cli.py report-snapshot YYYY-MM-DD    # 冻结自然日日报素材，不生成报告（maintenance only）
  python cli.py report-publish SNAPSHOT_ID     # 从冻结素材发布带引用结构化日报（maintenance only）
  python cli.py report-review-preview ATTEMPT_ID  # 查看模型草稿、冻结证据与复核摘要（maintenance only）
  python cli.py report-review ATTEMPT_ID approved|rejected DIGEST REASON  # 记录人工决定（maintenance only）
  python cli.py report-publish-reviewed REVIEW_ID  # 发布已人工批准的模型日报（maintenance only）
  python cli.py api-admin ACTION [ARGS]  # 本机管理 API 消费者和密钥（只在签发时输出明文）
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
    from app.catalog import sync_identity_catalog
    with get_db() as db:
        catalog = sync_identity_catalog(db)
    print(
        f"数据库初始化完成：{n} 家公司，源注册表已同步；"
        f"身份目录包含 {catalog.companies_seen} 个旧公司映射、"
        f"{catalog.topics_seen} 个主题和 {catalog.publishers_seen} 个发布方。"
    )


def cmd_db_status(path: str | None = None) -> None:
    from app.db_admin import report_json, verify_database
    print(report_json(verify_database(path or config.DB_PATH)))


def cmd_db_backup(destination: str | None = None) -> None:
    from app.db_admin import backup_database, report_json
    print(report_json(backup_database(destination=destination)))


def cmd_db_bundle_backup(destination: str | None = None) -> None:
    from app.evidence_backup import create_backup_bundle
    print(json.dumps(create_backup_bundle(destination), ensure_ascii=False, indent=2))


def cmd_db_bundle_verify(path: str) -> None:
    from app.evidence_backup import verify_backup_bundle
    print(json.dumps(verify_backup_bundle(path), ensure_ascii=False, indent=2))


def cmd_db_bundle_restore(bundle: str, destination: str) -> None:
    from app.evidence_backup import restore_backup_bundle
    print(json.dumps(restore_backup_bundle(bundle, destination), ensure_ascii=False, indent=2))


def cmd_db_bundle_smoke(path: str) -> None:
    from app.portal_smoke import smoke_restored_bundle
    report = smoke_restored_bundle(path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "ok":
        raise SystemExit(1)


def cmd_db_coverage(path: str | None = None) -> None:
    from app.data_coverage import audit_data_coverage
    print(json.dumps(audit_data_coverage(path or config.DB_PATH), ensure_ascii=False, indent=2))


def cmd_db_legacy_compare(before: str, after: str) -> None:
    from app.legacy_compare import compare_legacy_snapshots
    result = compare_legacy_snapshots(before, after)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


def cmd_db_event_audit(path: str | None = None) -> None:
    from app.event_projection_audit import audit_event_projection
    result = audit_event_projection(path or config.DB_PATH)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


def cmd_db_curation_audit(path: str | None = None) -> None:
    from app.curation_import_audit import audit_curation_import
    result = audit_curation_import(path or config.DB_PATH)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


def cmd_db_projection_audit(path: str | None = None) -> None:
    from app.curation_projection_audit import audit_curation_projections
    result = audit_curation_projections(path or config.DB_PATH)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


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


def cmd_raw_verify() -> None:
    from app.ingest import audit_payloads
    report = audit_payloads()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.healthy:
        raise SystemExit(1)


def cmd_evidence_verify() -> None:
    from app.ingest import audit_evidence_payloads
    report = audit_evidence_payloads()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.healthy:
        raise SystemExit(1)


def cmd_legacy_backfill(batch_size: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "legacy-backfill requires INFOHUB_PROCESS_ROLE=maintenance"
        )
    from app.db_admin import verify_database
    from app.legacy_backfill import backfill_legacy_batch
    verify_database(config.DB_PATH, require_current=True)
    report = backfill_legacy_batch(batch_size)
    while report.status != "completed":
        report = backfill_legacy_batch(batch_size)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def cmd_legacy_topic_backfill(batch_size: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "legacy-topic-backfill requires INFOHUB_PROCESS_ROLE=maintenance"
        )
    from app.db_admin import verify_database
    from app.legacy_topic_backfill import backfill_legacy_topics_batch
    verify_database(config.DB_PATH, require_current=True)
    report = backfill_legacy_topics_batch(batch_size)
    while report.status != "completed":
        report = backfill_legacy_topics_batch(batch_size)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_review_preview(assignment_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("topic-review-preview requires maintenance role")
    from app.db_admin import verify_database
    from app.topic_assignment_reviews import review_preview
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        print(json.dumps(review_preview(db, assignment_id).to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_review_queue(after: str, limit: int, topic_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("topic-review-queue requires maintenance role")
    from app.db_admin import verify_database
    from app.topic_assignment_reviews import review_queue
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        rows = review_queue(
            db, after_sequence=0 if after == "none" else int(after),
            limit=limit, topic_id=None if topic_id == "all" else topic_id,
        )
    print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))


def cmd_topic_review_coverage() -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("topic-review-coverage requires maintenance role")
    from app.db_admin import verify_database
    from app.topic_assignment_reviews import topic_review_coverage
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        rows = topic_review_coverage(db)
    print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))


def cmd_topic_review(
    assignment_id: str, decision: str, expected_previous: str, reason: str
) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("topic-review requires maintenance role")
    import getpass
    from app.db_admin import verify_database
    from app.topic_assignment_reviews import record_topic_assignment_review
    verify_database(config.DB_PATH, require_current=True)
    expected = None if expected_previous == "none" else expected_previous
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        result = record_topic_assignment_review(
            db, assignment_id=assignment_id, decision=decision,
            expected_previous_review_id=expected, reviewer_id=getpass.getuser(), reason=reason,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_review_sample_create(per_topic_limit: int, seed: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-review-sample-create requires maintenance role"
        )
    import getpass
    from app.db_admin import verify_database
    from app.topic_review_sampling import create_sample_batch
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        result = create_sample_batch(
            db, seed=seed, per_topic_limit=per_topic_limit,
            created_by=getpass.getuser(),
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_review_sample_report(batch_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-review-sample-report requires maintenance role"
        )
    from app.db_admin import verify_database
    from app.topic_review_sampling import sample_report
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        result = sample_report(db, batch_id)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_review_sample_queue(
    batch_id: str, after: str, limit: int, mode: str
) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-review-sample-queue requires maintenance role"
        )
    if mode not in {"pending", "all"}:
        raise ValueError("sample queue mode must be pending or all")
    from app.db_admin import verify_database
    from app.topic_review_sampling import sample_queue
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        rows = sample_queue(
            db, batch_id, after_ordinal=-1 if after == "none" else int(after),
            limit=limit, pending_only=mode == "pending",
        )
    print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))


def cmd_topic_review_console(batch_id: str, port: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-review-console requires maintenance role"
        )
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    import getpass
    import secrets
    import uvicorn
    from app.db_admin import verify_database
    from app.topic_review_console import create_topic_review_console
    from app.topic_review_sampling import get_sample_batch
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        get_sample_batch(db, batch_id)
    review_app = create_topic_review_console(
        batch_id=batch_id,
        csrf_token=secrets.token_urlsafe(32),
        reviewer_id=getpass.getuser(),
    )
    print(f"本机审核工作台：http://127.0.0.1:{port}/")
    print(f"审核批次：{batch_id} · 审核人：{getpass.getuser()}")
    uvicorn.run(review_app, host="127.0.0.1", port=port, log_level="info")


def cmd_topic_sample_gate_preview(batch_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-sample-gate-preview requires maintenance role"
        )
    from app.db_admin import verify_database
    from app.topic_review_sample_gate import sample_gate_preview
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        result = sample_gate_preview(db, batch_id)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_sample_gate_review(
    batch_id: str, decision: str, expected_previous: str,
    minimum_decided_bps: int, minimum_topic_decided_bps: int,
    minimum_acceptance_bps: int, minimum_topic_acceptance_bps: int,
    reason: str,
) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-sample-gate-review requires maintenance role"
        )
    import getpass
    from app.db_admin import verify_database
    from app.topic_review_sample_gate import record_sample_evaluation
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        result = record_sample_evaluation(
            db, batch_id=batch_id, decision=decision,
            expected_previous_evaluation_id=(
                None if expected_previous == "none" else expected_previous
            ),
            minimum_decided_bps=minimum_decided_bps,
            minimum_topic_decided_bps=minimum_topic_decided_bps,
            minimum_acceptance_bps=minimum_acceptance_bps,
            minimum_topic_acceptance_bps=minimum_topic_acceptance_bps,
            evaluator_id=getpass.getuser(), reason=reason,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_statistics_advance(limit: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-statistics-advance requires maintenance role"
        )
    from app.db_admin import verify_database
    from app.topic_statistics import advance_topic_statistics
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(advance_topic_statistics(limit).to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_admission_preview(publication_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-admission-preview requires maintenance role"
        )
    from app.db_admin import verify_database
    from app.topic_statistics_admission import admission_preview
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        result = admission_preview(db, publication_id)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_topic_admission_review(
    publication_id: str, decision: str, expected_previous: str,
    minimum_bps: int, allow_zero_raw: str, sample_evaluation: str, reason: str,
) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "topic-admission-review requires maintenance role"
        )
    if allow_zero_raw not in {"true", "false"}:
        raise ValueError("ALLOW_ZERO must be true or false")
    import getpass
    from app.db_admin import verify_database
    from app.topic_statistics_admission import record_admission_review
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        result = record_admission_review(
            db, publication_id=publication_id, decision=decision,
            expected_previous_review_id=(
                None if expected_previous == "none" else expected_previous
            ),
            minimum_decided_assignment_bps=minimum_bps,
            allow_zero_members=allow_zero_raw == "true",
            sample_evaluation_id=(
                None if sample_evaluation == "none" else sample_evaluation
            ),
            reviewer_id=getpass.getuser(), reason=reason,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_legacy_event_project() -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError(
            "legacy-event-project requires INFOHUB_PROCESS_ROLE=maintenance"
        )
    from app.db_admin import verify_database
    from app.event_candidates import project_legacy_stories
    verify_database(config.DB_PATH, require_current=True)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        report = project_legacy_stories(db)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def cmd_legacy_curation_enqueue(after_id: int, limit: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("legacy-curation-enqueue requires maintenance role")
    from app.db_admin import verify_database
    from app.legacy_curation_import import enqueue_legacy_curation_batch
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(enqueue_legacy_curation_batch(after_item_id=after_id, limit=limit),
                     ensure_ascii=False, indent=2))


def cmd_legacy_curation_process(limit: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("legacy-curation-process requires maintenance role")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    from dataclasses import asdict
    from app.db_admin import verify_database
    from app.legacy_curation_import import process_one_legacy_curation_import
    verify_database(config.DB_PATH, require_current=True)
    results = []
    for _ in range(limit):
        result = process_one_legacy_curation_import(worker_id="maintenance-legacy-import")
        if result is None:
            break
        results.append(asdict(result))
    print(json.dumps({"processed": len(results), "results": results}, ensure_ascii=False, indent=2))


def cmd_curation_search_advance(limit: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("curation-search-advance requires maintenance role")
    from app.curation_search import advance_search_index
    from app.db_admin import verify_database
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(advance_search_index(limit).to_dict(), ensure_ascii=False, indent=2))


def cmd_curation_hot_advance(limit: int) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("curation-hot-advance requires maintenance role")
    from app.curation_hot_metrics import advance_hot_metrics
    from app.db_admin import verify_database
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(advance_hot_metrics(limit).to_dict(), ensure_ascii=False, indent=2))


def cmd_report_snapshot(date: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("report-snapshot requires maintenance role")
    from app.db_admin import verify_database
    from app.report_inputs import freeze_calendar_daily
    verify_database(config.DB_PATH, require_current=True)
    result = freeze_calendar_daily(date)
    print(json.dumps(result or {"status": "no_input"}, ensure_ascii=False, indent=2))


def cmd_report_publish(snapshot_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("report-publish requires maintenance role")
    from app.db_admin import verify_database
    from app.report_versions import publish_structured_report
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(publish_structured_report(snapshot_id), ensure_ascii=False, indent=2))


def cmd_report_review_preview(attempt_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("report-review-preview requires maintenance role")
    from app.db_admin import verify_database
    from app.report_review import review_preview
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(review_preview(attempt_id), ensure_ascii=False, indent=2))


def cmd_report_review(attempt_id: str, decision: str, digest: str, reason: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("report-review requires maintenance role")
    import getpass
    from app.db_admin import verify_database
    from app.report_review import record_manual_review
    verify_database(config.DB_PATH, require_current=True)
    result = record_manual_review(
        attempt_id=attempt_id, decision=decision, expected_digest=digest,
        reviewer_id=getpass.getuser(), reason=reason,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_report_publish_reviewed(review_id: str) -> None:
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("report-publish-reviewed requires maintenance role")
    from app.db_admin import verify_database
    from app.report_llm_publish import publish_reviewed_report
    verify_database(config.DB_PATH, require_current=True)
    print(json.dumps(publish_reviewed_report(review_id), ensure_ascii=False, indent=2))


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
    elif cmd == "db-bundle-backup":
        cmd_db_bundle_backup(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "db-bundle-verify" and len(sys.argv) == 3:
        cmd_db_bundle_verify(sys.argv[2])
    elif cmd == "db-bundle-restore" and len(sys.argv) == 4:
        cmd_db_bundle_restore(sys.argv[2], sys.argv[3])
    elif cmd == "db-bundle-smoke" and len(sys.argv) == 3:
        cmd_db_bundle_smoke(sys.argv[2])
    elif cmd == "db-coverage" and len(sys.argv) <= 3:
        cmd_db_coverage(sys.argv[2] if len(sys.argv) == 3 else None)
    elif cmd == "db-legacy-compare" and len(sys.argv) == 4:
        cmd_db_legacy_compare(sys.argv[2], sys.argv[3])
    elif cmd == "db-event-audit" and len(sys.argv) <= 3:
        cmd_db_event_audit(sys.argv[2] if len(sys.argv) == 3 else None)
    elif cmd == "db-curation-audit" and len(sys.argv) <= 3:
        cmd_db_curation_audit(sys.argv[2] if len(sys.argv) == 3 else None)
    elif cmd == "db-projection-audit" and len(sys.argv) <= 3:
        cmd_db_projection_audit(sys.argv[2] if len(sys.argv) == 3 else None)
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
    elif cmd == "raw-verify":
        cmd_raw_verify()
    elif cmd == "evidence-verify":
        cmd_evidence_verify()
    elif cmd == "legacy-backfill":
        cmd_legacy_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 250)
    elif cmd == "legacy-topic-backfill":
        cmd_legacy_topic_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 250)
    elif cmd == "topic-review-preview" and len(sys.argv) == 3:
        cmd_topic_review_preview(sys.argv[2])
    elif cmd == "topic-review-queue":
        cmd_topic_review_queue(
            sys.argv[2] if len(sys.argv) > 2 else "none",
            int(sys.argv[3]) if len(sys.argv) > 3 else 50,
            sys.argv[4] if len(sys.argv) > 4 else "all",
        )
    elif cmd == "topic-review-coverage":
        cmd_topic_review_coverage()
    elif cmd == "topic-review" and len(sys.argv) >= 6:
        cmd_topic_review(sys.argv[2], sys.argv[3], sys.argv[4], " ".join(sys.argv[5:]))
    elif cmd == "topic-review-sample-create" and len(sys.argv) >= 4:
        cmd_topic_review_sample_create(int(sys.argv[2]), " ".join(sys.argv[3:]))
    elif cmd == "topic-review-sample-report" and len(sys.argv) == 3:
        cmd_topic_review_sample_report(sys.argv[2])
    elif cmd == "topic-review-sample-queue" and len(sys.argv) >= 3:
        cmd_topic_review_sample_queue(
            sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "none",
            int(sys.argv[4]) if len(sys.argv) > 4 else 50,
            sys.argv[5] if len(sys.argv) > 5 else "pending",
        )
    elif cmd == "topic-review-console" and len(sys.argv) >= 3:
        cmd_topic_review_console(
            sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 8011,
        )
    elif cmd == "topic-sample-gate-preview" and len(sys.argv) == 3:
        cmd_topic_sample_gate_preview(sys.argv[2])
    elif cmd == "topic-sample-gate-review" and len(sys.argv) >= 10:
        cmd_topic_sample_gate_review(
            sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]),
            int(sys.argv[6]), int(sys.argv[7]), int(sys.argv[8]),
            " ".join(sys.argv[9:]),
        )
    elif cmd == "topic-statistics-advance":
        cmd_topic_statistics_advance(int(sys.argv[2]) if len(sys.argv) > 2 else 25)
    elif cmd == "topic-admission-preview" and len(sys.argv) == 3:
        cmd_topic_admission_preview(sys.argv[2])
    elif cmd == "topic-admission-review" and len(sys.argv) >= 9:
        cmd_topic_admission_review(
            sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]),
            sys.argv[6], sys.argv[7], " ".join(sys.argv[8:]),
        )
    elif cmd == "legacy-event-project":
        cmd_legacy_event_project()
    elif cmd == "legacy-curation-enqueue":
        cmd_legacy_curation_enqueue(int(sys.argv[2]) if len(sys.argv) > 2 else 0,
                                    int(sys.argv[3]) if len(sys.argv) > 3 else 100)
    elif cmd == "legacy-curation-process":
        cmd_legacy_curation_process(int(sys.argv[2]) if len(sys.argv) > 2 else 100)
    elif cmd == "curation-search-advance":
        cmd_curation_search_advance(int(sys.argv[2]) if len(sys.argv) > 2 else 200)
    elif cmd == "curation-hot-advance":
        cmd_curation_hot_advance(int(sys.argv[2]) if len(sys.argv) > 2 else 100)
    elif cmd == "report-snapshot" and len(sys.argv) == 3:
        cmd_report_snapshot(sys.argv[2])
    elif cmd == "report-publish" and len(sys.argv) == 3:
        cmd_report_publish(sys.argv[2])
    elif cmd == "report-review-preview" and len(sys.argv) == 3:
        cmd_report_review_preview(sys.argv[2])
    elif cmd == "report-review" and len(sys.argv) >= 6:
        cmd_report_review(sys.argv[2], sys.argv[3], sys.argv[4], " ".join(sys.argv[5:]))
    elif cmd == "report-publish-reviewed" and len(sys.argv) == 3:
        cmd_report_publish_reviewed(sys.argv[2])
    elif cmd == "api-admin":
        from app.api_key_admin import main as api_admin_main
        raise SystemExit(api_admin_main(sys.argv[2:]))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except config.RuntimeConfigurationError as exc:
        print(f"运行配置错误：{exc}", file=sys.stderr)
        sys.exit(2)
