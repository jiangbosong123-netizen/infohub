# 事件事实修订（P10c）

P10c 处理“身份仍是同一个事件，但我们掌握的事实发生变化”的情况。它不会替代 P10a 的关系/合并，
也不会替代 P10b 的拆分/撤回。当前门户继续读取旧 `stories`；本能力暂时只服务 shadow 事件模型。

## 修订类型

- `fact_update`：同一事件出现新增事实或进展，必须改变标题、类型、时间、实体或 `facts` 中至少一项；
- `correction`：可靠证据纠正先前记录，可以改变任意事件语义字段；
- `knowledge_update`：事实内容不变，只把知识状态更新为 `reported`、`corroborated`、`disputed`、
  `confirmed_by_primary` 或 `unknown`。

撤回不能伪装成普通修订。`knowledge_status=retracted` 只能走 P10b 的撤回服务。已经 merged、split 或
retracted 的终态事件也不能再修订。

## 版本与证据

`publish_event_revision` 要求调用者锁定当前 `event_version_id`，提交一份完整的新事件快照和该版本的
完整证据快照。每条证据固定到 `(document_version_id, raw_record_id)`；如绑定具体事实，`fact_id` 必须
存在于新版本的 `facts` 中。每个事实必须有稳定、唯一的 `fact_id`，并至少有一条直接支持或反驳证据。
新版本只引用显式提交的证据，不会暗中继承可能已经失效的旧证据。

发布后：

1. 旧 `event_versions` 和旧 `event_evidence` 保持不可变；
2. 新版本通过 `previous_version_id` 连接旧版本；
3. `event_revisions` 保存修订类型、实际变化字段、证据、理由和发布序号；
4. `events.current_version_id` 原子移动到新版本；
5. 事实字段变化才更新 `last_fact_change_at`，单纯知识状态变化不会伪造事实变化时间。

完全相同的任务重试返回原发布结果；复用幂等键但改变内容会失败。版本过期、证据配对错误、无实际
变化或事务中的并发终态变化都会整体回滚。`db-verify` 会重新计算两个版本之间的变化字段，并校验
版本链、证据集合和 `change_log`，防止审计描述与真实数据漂移。
