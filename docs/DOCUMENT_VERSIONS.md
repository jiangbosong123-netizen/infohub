# 稳定文档与不可变版本（P06c）

P06c 把新采集内容投影为稳定 `document` 和追加式 `document_version`。门户在读取切换前仍使用
`items`，但 `items` 只是一份兼容投影，不再是新内容的事实来源。迁移不会自动把历史 items
包装成伪造的文档版本；历史回填、质量分级和时间核验由 P07 完成。

## 写入与身份规则

一次新抓取按以下顺序处理：

1. `raw_record` 和 `raw_observation` 已经提交，CAS 文件重新核对大小与 SHA-256；
2. 用 `(source_id, external_id)` 查找 `document_locator`；同一 legacy URL 已存在但来自另一来源时，
   可通过 `legacy_item_id` 关联到原文档并增加 locator；
3. 首次看到 locator 时创建稳定 UUID 文档；`first_seen_at` 固定取 raw record 的观察时间，后来的
   更早发布时间或更正都不能回写首次看到时间；
4. 标准化标题、文本、可信 published 时间和内容质量，计算 `version_sha256`；
5. 与当前版本完全相同则不增加版本，只补充尚未存在的 raw input；内容或元数据改变时，追加连续
   版本并用 `previous_version_id` 形成更正链；
6. 新版本、raw input、current pointer 和 `items` 兼容投影在同一个 SQLite 事务中提交。

相同正文出现在不同 URL 时，P06c 保留为不同 document，不能仅凭内容 hash 合并语义身份。转载、
别名 URL 和跨来源合并需要后续带证据的决策。重复抓到完全相同的来源 record 会复用 raw record，
因此数据库只增加 observation，不增加 document version 或 input。

## 时间与内容质量

`document_versions.published_at` 可空，只从同一 raw record 上 `role=published`、`status=valid` 的
来源时间投影。updated、SEC accepted、filing date、report period、Google News 聚合时间以及
legacy fallback 都不能提升为正式发布时间。版本通过 `published_time_value_id` 保留到具体解析
证据的引用。

内容质量字段必须说明系统实际取得了什么：

- RSS/Google entry：`feed_excerpt`，有摘要为 `excerpt`，否则为 `title_only`；
- 财联社/华尔街见闻逐条 API 正文：`publisher_text`；
- 新浪超过当前 400 字兼容投影限制时明确 `truncated=1` 和 `partial`；
- SEC、HKEX 和 HTML 列表当前是 `generated_metadata`，不能标成发布方全文。

每个版本的 `source_id` 指采集入口。`publisher_id` 预留为可空稳定身份，P08 建立 publisher 目录前
保持 NULL；不能把 Google News、SEC/HKEX 传输入口或一个 feed 自动当成实际刊登者。版本同时冻结
`normalizer_version`、`normalized_at`、`time_rule_version`、`tzdb_version`、时间精度和解析状态。

当前 normalizer 为 `document-normalizer-v1`。任何会改变标准化结果的代码升级必须提升版本标识，
不能用同一个规则名产生不同结果。

## 不可变与发布边界

数据库触发器禁止修改或删除 document versions、version inputs 和 locators 的身份字段，也禁止
删除稳定 document。只有 document 的 current pointer/status，以及 locator 的最后观察时间等受控
字段可以更新。current pointer 必须引用本 document，版本号必须连续且 previous 指向紧邻版本。

新表目前为 shadow model：没有公共 API，也不会写消费者 change log。原因是现有 crawl handler
尚未将整个抓取批次纳入 durable job 的完成事务；提前写 change 会制造消费者已见到、任务却重试
的分裂状态。P18 切换外部读取时，必须通过 `publish_job_result` 原子写版本可见性、change 和 job
完成。表内 `available_at` 只表示本地规范化事务提交时间，不代表对外发布游标。

## 迁移、回滚与验收

迁移 6 只新增 `documents`、`document_versions`、`document_version_inputs`、`document_locators` 和
约束，不改写历史 items/raw evidence。部署前照常生成一致性 SQLite 备份并保留 blobs。回滚代码时
旧版本继续读取 items；新增 document 表和证据必须保留，不能删除。关闭新写入后，当前门户仍完整
可用。

验收至少覆盖：同 locator 更正追加版本、相同 record 重抓只增加 observation、正式发布时间不由
updated/accepted 伪造、不同 URL 不因相同正文误合并、CAS 缺失时 legacy 与 normalized 写入一同
回滚、版本和 input 无法被更新或删除。
