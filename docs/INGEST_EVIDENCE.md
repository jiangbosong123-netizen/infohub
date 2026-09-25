# 不可变采集证据与 CAS

P06a 建立采集运行、来源配置快照、不可变载荷和重复观察的基础层。P06b 在同一基础上为 JSON/RSS
连接器保存逐条 API record / 解析 entry 和来源时间；HTML 列表仍是 generated metadata。P06c 将
这些证据投影成文档版本，但内容质量仍按实际取得的载荷标注，不能把本层记录冒充发布方全文。

## 当前写入链路

1. worker 为一次来源执行创建 `ingest_runs`，记录当时的 dataset epoch，并引用不可变的
   `source_config_versions`。配置内容经过 allowlist 和秘密参数脱敏；配置 hash 不变时复用同一版本。
2. fetcher 有逐条来源记录时，只把该 record 放入稳定 JSON envelope，标为 `feed_entry` 或
   `api_record`；没有时继续保存 allowlist 后的候选并标 `generated_metadata`。两者均为
   `retention_class=private-metadata`，不能冒充未取得的发布方全文。
3. 字节先写到 `BLOB_PATH/sha256/<前两位>/<sha256>` 临时文件，`fsync` 后在同目录原子改名；已有
   同 hash 文件会重新校验大小和 SHA-256。
4. 只有 CAS 文件通过校验后，事务才写 `raw_records` 和 `raw_observations`。证据写入失败时旧
   `items` 投影也不得发布该条目。
5. 同一来源、外部 locator 和 payload hash 复用一个 `raw_record`；每次执行仍追加 observation。
   同 URL 内容变化会产生新 raw record，旧字节不覆盖。
6. run 最终记录 succeeded/partial/failed、候选数、接收数、legacy 去重数、拒绝数和字节数。接收与
   拒绝严格划分候选数；请求级失败只影响 run 状态，不伪装成一条被拒绝的候选。

数据库触发器禁止更新或删除来源配置版本、raw record 和 observation；ingest run 只允许从
`running` 一次性转为终态，避免应用之外的误操作悄悄改写证据历史。

常规抓取与 Google News 对账均经过这条路径。当前 `request_count` 对普通来源按一次来源调用记录，
对账按公司调用数记录；SEC/HKEX 的分页水位仍要在后续连接器增量工作中细化。

## 完整性与秘密边界

- CAS 引用只允许留在配置的 blob 根目录内；路径逃逸、缺失、大小不符或 hash 不符都失败。
- URL 中常见 token/signature/key 参数和配置/extra 中的凭据字段在持久化前替换为
  `[redacted]`。请求 Authorization、Cookie 等头不会进入表或 blob。
- `RAW_PAYLOAD_MAX_BYTES` 默认 2 MiB。超限先拒绝，不能静默截断后声称完整。
- `python cli.py raw-verify` 全量核对每个 DB 引用、文件大小和 SHA-256；任一缺失或损坏返回非零。
- CAS 写成功而数据库事务失败时可能留下无引用 blob。它不影响读取；带至少 7 天保护期的 GC 在
  后续运维 PR 实现，当前不得人工按“看起来没引用”立即删除。

## 备份与恢复

SQLite 备份从 P06a 起不再代表完整数据集，必须和 blob 一起保全。推荐完整备份流程：

1. 暂停唯一 worker，保留 web 读取；
2. 执行 `db-bundle-backup [DEST]`。命令创建 SQLite 一致性快照，只复制该快照引用的
   CAS 文件，写清单、对副本逐项校验，再原子发布整个备份目录；
3. 将整个 `.bundle` 目录复制到另一存储位置，对复制后的目录执行
   `db-bundle-verify PATH`；
4. 运行 `db-bundle-restore BUNDLE DEST` 到一个不存在的隔离目录。命令恢复并校验
   `database.db` 与 `blobs/`，不会覆盖当前运行库或自动切换服务；
5. 运行 `db-bundle-smoke DEST`。它在一次性数据库副本上打开门户主要路由，
   返回逐页状态；正式切换后还需在实际服务上检查 worker 与 `/api/ready`。

原有 `db-backup` 只备份 SQLite，仍可用于迁移前的快速回滚点，不能单独充当完整证据备份。
`db-bundle-backup` 要求当前 schema，不会替旧数据库自动迁移，也不会自动暂停 worker。
若该备份早于已经对外发布的数据，正式切换前还需按数据集 epoch 规则处理外部同步游标；
仅恢复到隔离目录不改变当前数据集 epoch。
`db-bundle-smoke` 应在恢复目录尚未作为运行库打开时执行；应用打开 SQLite 后可能把
`journal_mode` 切换为 WAL，使归档清单内的数据库文件摘要不再适用。永久保留的备份包不应
被应用直接作为运行库打开。

写入顺序保证已提交 DB 引用之前 blob 已存在；暂停 worker 后复制不会与写入竞争。
备份包只包含快照数据库引用的 blob，无引用的残留对象不在备份中。不能用缺失 blob 的数据库
启动文档发布。备份包验证不代替定期的离机副本和实际恢复演练。

[Mac 副本备份演练](evidence/p21n-evidence-backup-bundle.json) 从隔离的本地旧库副本升级后生成备份包，
复制到另一目录并核验数据库摘要，34,088 条文章和 9 份旧日报数量保持不变。旧库没有新 CAS
引用，因此缺失、篡改和含证据的备份路径由专门测试覆盖；该演练不代表 Windows 生产恢复已通过。

[隔离恢复演练](evidence/p21o-isolated-bundle-restore.json) 将更新后的 Mac 数据库副本打包并恢复到
全新目录，复核数据库摘要、34,122 条文章和 9 份旧日报。此时本地旧库仍无 CAS 引用；含原文、
日报提示词与响应的恢复后读取另由固定测试覆盖。Windows 生产库仍未迁移或恢复。

[恢复后门户验收](evidence/p21p-restored-portal-smoke.json) 在更新后的 Mac 副本上覆盖首页、
主题、搜索、日报、故事和进程存活页面，全部返回 HTTP 200，原备份摘要未变。
该演练使用 34,130 条文章和 9 份旧日报，未证明 Windows worker 或外部访问就绪。

## 兼容与回滚

迁移 4 只增加四张表，不删除或改写 `items`。回滚到 P05 代码时旧进程会忽略这些表和 blob；P06a
读取开关尚未切换，门户仍读 legacy 投影。回滚不能删除新增证据，因为后续版本会继续复用它。
