# 稳定事件候选与旧 Story 映射

P09a 建立事件层的 shadow 写入模型。现有门户继续读取 `stories` / `story_items`；这一阶段不切换
页面、不替换现有聚类器，也不把标题相似解释成事实相同。

## 对象边界

- `events` 是长期稳定身份。`candidate` 表示需要后续语义判断，不能解释为事实已证实。
- `event_versions` 是追加式语义快照，固定标题、事件类型、实体、主题、事实和知识状态。
- `event_evidence` 固定引用 `document_version_id` 和属于该版本的 CAS `raw_record_id`。
- `document_event_links` 是多对多关系；结构允许一篇文档关联多个事件，也允许一个事件拥有多篇报道。
- `match_decisions` 保存输入版本、候选事件版本、特征、分数、决定理由和复核状态。
- `legacy_story_events` 保留每个旧 story ID 到稳定事件的映射，包括旧 redirect ID。

`latest_report_at` 表示最新证据可见时间，`last_fact_change_at` 只表示结构化事实最后变化时间。
旧投影没有结构化事实，所以该字段保持 NULL；新增转载或报道可以只增加 evidence/link 并推进
前者，不伪造事实变化。

## 旧数据投影

维护命令：

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py legacy-event-project
```

运行前必须已经完成：

1. P07 文档历史回填，每个 `story_item` 都能解析为稳定 document/version/raw evidence；
2. P08 身份和主题目录同步；
3. 数据库严格验证与一致性备份。

投影规则：

- 当前 redirect 链的所有旧 story ID 映射到同一 candidate event；循环或缺失目标会让事务失败。
- 旧标题聚类导入为 `candidate_link`，复核状态为 `pending`。
- 旧 match score 只是旧聚类器分数，不是事实概率，也不是置信度校准结果。
- 旧 story 不提供结构化事实，因此 `facts_json=[]`、事件时间精度为 `unknown`、知识状态为
  `unknown`。
- 旧公司的 slug 只在已有稳定实体映射时进入事件实体列表；人物、产品或宽泛关键词不会被提升
  为公司实体。
- 旧 story 标题、类型、实体或主题变化时追加 event version；旧版本和旧 link 不覆盖，新 link
  通过 `supersedes_link_id` 指向上一版本。

投影重复执行是幂等的。如果旧 redirect 拓扑在完成映射后改变，命令会失败而不是静默改写稳定
ID；后续 P10 使用正式 merge/split 关系处理这类变化。

## 明确不在本阶段完成的能力

- P09a 不运行新的语义匹配器，不自动合并或拆分事件。
- 财报期、产品版本、否认与宣布等硬负例属于 P09b。
- merge、split、withdraw 和事件演进关系属于 P10。
- 自动 `corroborated` 或 `confirmed_by_primary` 必须等待 P12 评估门槛和后续审核逻辑。
- 门户和 API 仍读旧模型，生产不会因为空 shadow 表改变页面结果。

## 失败和恢复

整个旧 story 投影在一个显式维护事务中执行。缺文档证据、redirect 循环、稳定映射冲突、版本链
错误或外键错误都会整体回滚。迁移 10 本身只创建空表与约束，不自动投影历史数据，也不修改旧
`stories`、`story_items` 或门户数据。
