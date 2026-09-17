# 事件拆分与撤回（P10b）

P10b 为稳定事件增加两种需要人工复核的终态操作：`split` 和 `retracted`。它们与 P10a 的
`merged` 一起构成互斥终态。当前门户仍读取旧 `stories`；这些表和服务先在 shadow 数据模型中运行，
不会改变现有页面、URL 或生产内容。

## 拆分

当一个事件候选错误地混合了两个或更多独立事件时，`publish_event_split` 把原事件标记为 `split`，
并指向至少两个已经存在的 canonical replacement events。它不会删除原事件、改写原版本，或复制证据。

每次拆分必须满足：

- 锁定原事件和每个 replacement 的 `current_version_id`；
- 把原事件当前版本的每一条 `(document_version_id, raw_record_id)` 证据分配给 replacement；
- 每个 replacement 至少收到一条证据；
- 原事件和 replacement 都没有被合并或进入其他终态；
- 保存人工可读理由、原状态、发布序号和对应 `change_log`。

原事件 ID 仍可查询，状态变为 `split`。读取方可用 `event_split_replacements` 展示替代事件，而不是把
历史链接静默重定向到其中任意一个。

## 撤回

当可靠新证据表明整个事件结论不应继续生效时，`publish_event_retraction` 追加一个新的
`event_versions` 版本，将 `knowledge_status` 设为 `retracted`，再把事件状态设为 `retracted`。
旧事件版本不可变并保留原结论，因此能够回答“当时系统知道什么”。

撤回证据必须固定到 `(document_version_id, raw_record_id)`，角色只能是 `contradicts` 或 `context`。
撤回记录保存原状态、理由、新版本、证据 ID、发布时间和变化序号。事实字段的普通修订不使用撤回；
它属于后续独立的 P10c 版本修正规则。

## 原子性与历史读取

拆分和撤回都通过 durable job、lease 与 `publish_job_result` 在单个 `BEGIN IMMEDIATE` 事务中发布。
状态、版本、证据、终态记录、变化账本和任务完成必须全部成功，否则全部回滚。完全相同的重试返回原
结果；复用幂等键但改变理由、证据或目标会被拒绝。

`event_status_at_sequence` 根据 `publication_seq` 返回终态发生前后的状态。迁移 12 为新 merge 保存
`previous_status`，因此新产生的 merge、split 和 retraction 都能按变化高水位复现。迁移前已经存在且
没有原状态的 legacy merge 会明确报“历史状态未知”，不会猜测。

`db-verify` 检查终态互斥、状态一致、变化记录、版本归属、证据存在和拆分证据覆盖。管理 API 与页面
仍未开放；在 P17 身份认证和人工复核队列完成前，这些写操作只允许受控 worker 调用。
