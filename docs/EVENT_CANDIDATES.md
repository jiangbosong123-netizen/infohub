# 稳定事件候选、旧 Story 映射与保守匹配

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

## P09b：结构化候选匹配

`app/event_matching.py` 接收一条固定 `document_version_id` 上的结构化事件提议，并逐个比较固定的
`event_version_id`。调用者必须传入明确的事件类型、主体实体、事件时间和事实；一个长文包含多个事件时，
应拆成多个提议，也允许同一文档版本最终产生多条 event link。

匹配顺序是“硬排除优先，支持证据其次，标题相似最后”：

1. 主体实体明确且不相交，直接 `no_match`；
2. 两边事件类型明确且冲突，直接 `no_match`；
3. 财报期明确且不同，直接 `no_match`；
4. 产品或模型版本明确且不同，直接 `no_match`；
5. 一边是否认、另一边是宣布/计划/断言，直接 `no_match`，后续由 P10 建立事件关系；
6. 去除 `fact_id`、证据 ID 和置信度后仍相同的结构化事实，可建立 `candidate_link`；
7. 否则只有主体兼容且标题达到保守阈值，或主体/事件类型/财报期均相同，才建立候选链接；
8. 没有硬冲突但证据不足时记录 `needs_review`，不建链接；所有召回候选都被硬排除时再记录
   `new_candidate` 决定，实际新事件创建仍由独立发布事务负责。

每次比较保存输入文档版本、候选事件版本、规则版本、完整特征、决定和理由。分数的字段语义固定为
`ranking_not_probability`，不能解释为事实为真的概率。自动建立的关系始终使用 `candidate` role、
`pending` review status；P12 固定标注集和质量门槛完成前，匹配器不把事件改为 active、reported、
corroborated 或 confirmed。重复输入使用稳定 decision key，因此重跑不会增加决定、链接或证据。

候选链接会引用该文档版本已有的 CAS raw input，并只更新 `latest_report_at`；它不会改变
`last_fact_change_at`。这保证“又有一篇报道”和“事件事实发生变化”是两种不同时间。

当前规则能识别显式结构化 period/version/modality，以及中英文标题中有限的财报季度、点分版本和
否认/宣布词。金额变化、日期变化究竟是更正还是新事件仍需要事实比较和 P10 关系模型；不能通过降低
标题阈值解决。当前没有 embedding，也没有声称召回率已达标，阈值需在 P12 标注集上评估后才能调整。
- 自动 `corroborated` 或 `confirmed_by_primary` 必须等待 P12 评估门槛和后续审核逻辑。
- 门户和 API 仍读旧模型，生产不会因为空 shadow 表改变页面结果。

## 失败和恢复

整个旧 story 投影在一个显式维护事务中执行。缺文档证据、redirect 循环、稳定映射冲突、版本链
错误或外键错误都会整体回滚。迁移 10 本身只创建空表与约束，不自动投影历史数据，也不修改旧
`stories`、`story_items` 或门户数据。
