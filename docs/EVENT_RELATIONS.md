# 事件关系与无环合并（P10a）

P10a 在稳定候选事件之上增加可追踪的事件关系和合并身份链。当前门户仍读取旧 `stories`，因此本阶段
只建立 shadow 领域能力，不改变现有页面 URL、排序或生产内容。

## 事件关系

`event_relations` 保存 `follows`、`implements`、`corrects`、`denies` 和 `related_to`。每条关系固定：

- 两个稳定 event ID；
- 明确的关系类型和人工可读理由；
- 至少一个存在的 CAS `raw_record` 证据 ID；
- `available_at`、发布序号和对应 `change_log`；
- 可选的 `supersedes_relation_id`。

关系不可修改或删除。修正关系要追加新行并指向同端点的旧关系；旧行仍可用于历史查询。一个否认事件
和被否认的宣布事件保持两个身份，通过 `denies` 关联，不能为了页面简洁合并成同一事件。

## 合并与旧 ID

`event_merges` 保存 absorbed event → survivor event。合并只把 absorbed event 的当前状态更新为
`merged`，不会删除或覆盖它的 `event_versions`、文档链接、证据和旧 ID。`resolve_event` 沿不可变链
解析最终 canonical ID，例如 A→B、B→C 后，A 和 B 都解析到 C，并保留完整路径。

应用层和 SQLite trigger 都拒绝自环和循环。survivor 如果后来又被合并，可以继续形成链；已经
merged/split/retracted 的对象不能再次作为新的合并源或直接目标。调用者应保存用户请求的旧 ID，
读取时返回 canonical ID 和 chain，而不是物理改写历史引用。

## 原子发布

`publish_event_relation` 和 `publish_event_merge` 是唯一写入口。它们要求：

1. 已领取的 durable job 和仍有效的 lease；
2. 决策所依据的两个 `current_version_id`，作为乐观锁；
3. 原始证据 ID、理由和业务幂等键；
4. 通过 `publish_job_result` 在一个 `BEGIN IMMEDIATE` 事务中追加 change、关系/合并记录、状态变化并
   完成任务。

任一检查失败会整体回滚。同一成功任务和 lease 重试会返回原结果，不重复写入；相同幂等键对应不同
内容会被发布账本拒绝。`db-verify` 进一步检查证据、发布序号、change 资源、merged 状态和整张合并图。

## 边界与后续

- P10a 只实现关系和 merge，不实现 split、withdraw/retract 或事件事实版本修正；这些属于 P10b。
- 合并不会把被吸收事件的文档链接复制到 survivor；未来查询服务按 canonical lineage 聚合，避免复制
  造成证据计数膨胀。
- 关系和合并已有变化序号，但 v1 API、snapshot 与消费者游标仍分别属于 P18/P19。
- 当前没有管理页面。P17 鉴权和 P14 人工复核完成前，不开放匿名写入口。
