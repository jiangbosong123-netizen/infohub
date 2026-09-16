# 不可变采集证据与 CAS

P06a 建立采集运行、来源配置快照、不可变载荷和重复观察的基础层。它先解决“取得过什么、何时又
看见一次、载荷是否还在”这三个问题；文档版本、来源时间解析和全文/原始响应规则分别由 P06b、
P06c 接续，不能把本层记录直接当作已经标准化的文章。

## 当前写入链路

1. worker 为一次来源执行创建 `ingest_runs`，记录当时的 dataset epoch，并引用不可变的
   `source_config_versions`。配置内容经过 allowlist 和秘密参数脱敏；配置 hash 不变时复用同一版本。
2. 现有 fetcher 产生的候选对象先按稳定 JSON 编码。P06a 将它标为
   `payload_kind=generated_metadata`、`retention_class=private-metadata`，明确表示它可能已经被旧解析器
   截断或生成，不能冒充发布方全文。
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
对账按公司调用数记录；SEC/HKEX 的逐请求和分页水位要在 P06b 的来源适配器中细化。

## 完整性与秘密边界

- CAS 引用只允许留在配置的 blob 根目录内；路径逃逸、缺失、大小不符或 hash 不符都失败。
- URL 中常见 token/signature/key 参数和配置/extra 中的凭据字段在持久化前替换为
  `[redacted]`。请求 Authorization、Cookie 等头不会进入表或 blob。
- `RAW_PAYLOAD_MAX_BYTES` 默认 2 MiB。超限先拒绝，不能静默截断后声称完整。
- `python cli.py raw-verify` 全量核对每个 DB 引用、文件大小和 SHA-256；任一缺失或损坏返回非零。
- CAS 写成功而数据库事务失败时可能留下无引用 blob。它不影响读取；带至少 7 天保护期的 GC 在
  后续运维 PR 实现，当前不得人工按“看起来没引用”立即删除。

## 备份与恢复

SQLite 备份从 P06a 起不再代表完整数据集，必须和 blob 一起保全：

1. 暂停唯一 worker，保留 web 读取；
2. 执行一致性 `db-backup`；
3. 对运行库执行 `raw-verify`；
4. 复制 `data/blobs/sha256` 与数据库备份到同一备份批次并记录清单；
5. 恢复演练同时还原 DB 与 blob，再执行 `db-verify` 和 `raw-verify`。

写入顺序保证已提交 DB 引用之前 blob 已存在；暂停 worker 后复制不会产生新的引用。额外的无引用
blob 可以保留，绝不能用缺失 blob 的数据库启动文档发布。

## 兼容与回滚

迁移 4 只增加四张表，不删除或改写 `items`。回滚到 P05 代码时旧进程会忽略这些表和 blob；P06a
读取开关尚未切换，门户仍读 legacy 投影。回滚不能删除新增证据，因为后续版本会继续复用它。
