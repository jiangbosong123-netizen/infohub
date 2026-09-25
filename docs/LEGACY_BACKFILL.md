# 历史数据可续跑迁移（P07）

P07 为旧 `items`、`item_discoveries` 和 `daily_reports` 建立稳定映射，同时保持旧表逐字节可读。
它不把旧摘要声称为原始正文，也不把旧 `published_at` 声称为已核实来源时间。

## 冻结范围与写入结果

首次运行会在 `legacy_backfill_state` 冻结当时的最大 item/report ID。之后正常 P06 采集的新记录不进入
历史范围，避免一个永远向前移动的回填终点。旧数据按来源分区处理：

- 每个 item 保存一份 `payload_kind=legacy_excerpt` 的不可变 CAS 快照，包含当时 legacy 行；
- 新建或复用稳定 document；旧 snapshot 标 `content_origin=legacy_unknown`、
  `time_status=legacy_unverified`、`availability_basis=legacy_unknown` 和
  `point_in_time_eligible=0`，正式 `published_at` 保持 NULL；
- discovery 映射为同 document 的 locator，原 first/last 时间保留为未核实 legacy 线索；
- daily report 只建立稳定 `legacy_report` 身份与内容 hash，状态为 `pending_domain_upgrade`，等待 P21
  建立正式 report versions；
- `legacy_object_mappings` 保存旧键、目标 ID、旧快照 hash、质量状态与映射时间，触发器禁止改写或删除。

空标题/摘要仍会映射，内容范围为 `none`；无效 URL 使用 `urn:infohub:legacy-item:<id>` 内部 locator，
旧门户链接和旧行原值不变。无效或无时区 legacy 时间不猜时区，迁移版本始终保持
`legacy_unverified`。如果一个旧 item 已经由 P06 新证据生成 document，回填只把旧快照作为
`additional` input，不用旧内容覆盖当前证据版本。

## 中断与恢复

每个来源使用一个带确定性 trace 的 backfill ingest run，item ID 是 observation ordinal。CAS 或
observation 已写、document 尚未提交时崩溃，下一次会读取同一不可变 payload 继续；不会追加重复
observation 或版本。item 映射和 cursor 分开提交时，映射先完成，恢复时验证 hash 后再推进 cursor。

`python cli.py legacy-backfill 250` 仅允许 maintenance 角色。命令内部持续执行小事务直至完成；被终止
后运行同一命令即可续跑。最终 JSON 中以下三项必须全部为 0：

- `unexplained_items`
- `unexplained_discoveries`
- `unexplained_reports`

完成状态还会验证 mapping 的目标 document、locator 和 report identity 确实存在，不能只用映射行数
冒充覆盖。完整迁移后执行 `db-verify` 与 `raw-verify`。
另用 `db-legacy-compare BEFORE.db AFTER.db` 对冻结备份与迁移副本做原有列的逐行指纹核对，
避免只凭数量相同忽略旧字段被改写。该命令只读，详见
[旧表对比说明](LEGACY_COMPARE.md)。

## 生产步骤与回滚

1. 停止唯一 worker，保持 maintenance 独占写入；
2. 同批备份 SQLite 与现有 blobs，记录数据库和对象清单；
3. 执行 schema 7 迁移，再运行 `legacy-backfill`；
4. 要求三类 unexplained 均为 0、SQLite integrity 为 ok、外键为 0、CAS 全量验证通过；
5. 启动 worker，门户仍从旧 `items`/`daily_reports` 读取，先观察再进入后续切读 PR。

回滚时停止 backfill/worker并使用旧代码继续读 legacy 表。新 CAS、document、mapping 和 stable ID 必须
保留；不能删除后重跑生成另一组 ID。schema 7 只追加表和 document 质量列，不覆盖历史业务行。

2026-09-16 的完整本地生产副本演练覆盖 20,829 items、21,369 discoveries 和 5 reports，三类
unexplained 均为 0，20,829 个 CAS 全量校验通过，旧表逐行指纹未变，总耗时约 402 秒。详细机器可读
结果见 [P07 演练证据](evidence/p07-production-copy-rehearsal.json)。它不是新 Windows 导出副本验收，
也不代表 Windows 必然具有相同耗时；正式部署前仍需在新 Windows 副本复演。

2026-09-21 又在新 Mac 在线备份副本上演练至当前 schema 21：36,262 items、40,370 discoveries、
10 份旧日报全部映射，三项 unexplained 为 0；36,262 个 raw CAS 全部校验通过，11 张旧表原有列
指纹一致。演练发现 discovery 覆盖核对查询在较大数据库上被规划为嵌套全表扫描，单次耗时约
72 秒；固定 locator-first 顺序并按映射目标索引查找后，同副本单次约 0.03 秒。中断后从保存的
游标恢复，完成后再跑处理 0 条。完整结果及未完成的事件、分析和索引缺口见
[P22 演练证据](evidence/p22-isolated-backfill-rehearsal.json)。这些时间仅说明 Mac 副本情况，
不预测 Windows 生产维护窗口。
