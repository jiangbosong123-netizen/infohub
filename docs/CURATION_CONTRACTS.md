# 现有策展能力的版本化契约

P13a 将旧 `items` 表中的复合策展结果拆成四种独立任务：

| 任务 | schema | 旧字段 | 新语义 |
|---|---|---|---|
| translation | `infohub.translation/1.0` | `title_zh` | 翻译或中文标题精简；保留原始标题，不覆盖原文 |
| relevance | `infohub.relevance/1.0` | `tmt/reason/ai_cat` | 相关/不相关；官方或公司关联强制保留必须标记 `policy_override` |
| summarization | `infohub.summarization/1.0` | `summary/raw_summary` | 摘要与逐项证据；旧数据来源不明时明确标记 |
| importance | `infohub.importance/1.0` | `score/reason` | 0–100 编辑重要性，不是准确率或涨跌概率 |

旧结果全部以 `needs_review` 导入，不直接成为 `valid`。缺失值转成
`insufficient_evidence`；历史内容过滤占位 `title_zh='-'` 和 `score=-1` 转成
`refused`，不能继续伪装成译文或分数。旧流程把官方文件和已关联公司强制保留时，
`policy_override=true`，避免把产品规则误称为模型的 TMT 判断。

任务契约由结果发布层再次验证。注册 schema 与任务必须一致；摘要每个 claim 必须引用
本次输入中的 evidence ID；重要性必须声明 `editorial_importance_not_probability`。
其他尚未进入 P13 的分析 schema 继续由通用 envelope 验证器处理。

P13b 增加离线导入器。只有 P07 已回填、且当前文档版本的 primary 输入是
`legacy_excerpt` 的条目才可排队；导入器会校验 CAS 哈希及冻结快照与文档版本的一致性。
它不读取后来可能改变的 `items` 当前字段，因此中断重试不会把另一版内容误挂到旧版本。
每个文档版本 × 任务有独立幂等键，先写 run/zero-cost 授权/attempt，再原子发布结果并完成 job。
若已有相同对象与任务的 publication，导入器只完成 job，不覆盖人工或新模型结果。

仅在**隔离副本**上按以下顺序运行；P07 回填必须先完成。进程角色需设为 `maintenance`：

```bash
python cli.py legacy-curation-enqueue 0 100
python cli.py legacy-curation-process 100
```

`legacy-curation-enqueue` 返回 `next_after_item_id`；继续下一页时将这个数字作为第一个参数。
重放同一页只会确保相同 job，不产生第二份结果。`legacy-curation-process` 每次最多处理
500 个独立任务，失败任务进入已有的 retry/dead-letter 状态；不自动调用付费模型。
旧模型名和提示词不可证明时写 `historical_unknown`，原始模型响应无法补造，费用只记录
零成本**导入授权**而非历史模型费用。摘要的旧来源证据标记 `partial`、结果标记
`needs_review`，并不表示逐句事实经过核实。

本阶段不批量导入生产数据。

P24 在 Mac 历史库的**隔离副本**上抽取 40 份跨频道文档（其中 10 份 SEC 相关），
完成四种任务共 160 个离线导入；验收记录见
[`p24-curation-evaluation-rehearsal.json`](evidence/p24-curation-evaluation-rehearsal.json)。
导入前的冻结快照与文档版本按 P07 标准化规则比较空白字符；原始 CAS 快照和哈希仍须
逐字节校验，实质内容差异仍阻止发布。`db-curation-audit [PATH]` 是只读阶段闸门，
检查当前副本中所有旧策展导入 job、冻结输入、离线 provider、零预算授权和初始发布状态；
有未完成 job 或结构异常时退出非零。该闸门仅适用于**人工复核或新模型改版之前**的初始
离线导入。检查通过不代表旧 AI 内容准确，也不代表 36,262 份历史文档已全部导入。

P13c 增加门户兼容读取投影，使用 `INFOHUB_CURATION_READ_ENABLED=true` 单独开启，
默认关闭。首页、搜索、稍后读、主题列表共用批量读取层；事件详情沿用同一投影。
只读取 `documents.current_version_id` 对应的四种当前 publication 指针，且仅允许
`valid`/`needs_review`、`supported`/`partial`、未被拒绝的注册 schema 输出。后端
重新校验 envelope 和任务数据；错误、拒绝或证据不足的**现有指针**会清空对应旧 AI
展示字段，避免把旧值当成新版结果。无指针时保持旧字段展示。旧导入结果展示为
“AI 待复核”，不声称已有人工确认。原始标题、原始摘要和原文 URL 不经过投影。

P13d 把首页与主题页的可见性、分类筛选、精选门槛、事件去重排序和主题计数
一起切到当前 relevance/importance publication；过滤在 SQL 的分页前完成。
relevance 的 `policy_override=true` 可保留产品规则强制收录的条目，
但页面不会把它误称为模型判断。无指针仍使用旧字段；有指针但结果被拒绝、
证据不足或无法读取时，条目不从旧相关性字段恢复可见性，也不沿用旧评分。
与 P13c 共用默认关闭的开关。

P13e 将“稍后读”、事件详情（报道、来源、主题标签和可见篇数）及热点的
入榜条件切到相同的发布结果可见性规则，仍在分页前过滤。热点已有的热度分、
来源数及历史归并计数不会在这里重算；它们仍是旧派生索引数值。

搜索索引、热点派生值、日报以及旧 AI 写入仍读取
`items`，因此在新旧结果不一致时可能与新版策展文本不同。在这些查询迁移并通过
回归前不要在生产环境开启此开关；尤其不能把本投影视为完整的新版发布通道。
回滚只需关闭开关并重启 web 进程，无需回退数据库迁移。

P13f 为搜索增加 schema 17：`curation_search_documents` 是可重建的派生表，
记录条目、文档版本、译题和摘要 publication 指针及入索引的文本；FTS5 trigram
索引由表触发器同步。`curation_search_dirty` 记录旧条目、文档版本和发布指针的
变动，`curation_search_state` 标记整代索引尚未构建/构建中/可用。迁移只建空表，
**不在 Web 启动或数据库升级中遍历历史记录**；索引构建与搜索切读将各自独立验收。
因此 schema 17 完成后，搜索仍走旧路径，`INFOHUB_CURATION_READ_ENABLED` 仍不能
用于生产。Mac 现有数据库只读备份的隔离升级演练见
[`p13f-search-schema-rehearsal.json`](evidence/p13f-search-schema-rehearsal.json)。

P13g 提供仅 maintenance 可执行的分批搜索索引构建，进度、文本和 dirty 确认在
同一事务提交。详见 [`CURATION_SEARCH_BUILD.md`](CURATION_SEARCH_BUILD.md)。

P13h 为搜索页增加独立的 `INFOHUB_CURATION_SEARCH_ENABLED` 开关；必须同时开启
`INFOHUB_CURATION_READ_ENABLED`。索引仅在状态 ready、dirty 队列为空且条目总数
与记录的索引行数相同时读取。否则展示旧搜索结果并明确提示可能缺少新发布内容。
搜索词最长 120 字符、页码最多 200、每页 30 条，支持译题和摘要的已发布文本；
短于 3 字符或 FTS 无匹配时使用转义后的字面 LIKE。回滚只需关闭搜索开关。
这一步仍不等于生产验收：生产演练与其他旧派生值切换尚未完成。

P13i 让 durable worker 在搜索开关启用时每分钟执行一次有界刷新（最多十批，
每批最多 500 条），初次建索引和增量 dirty 队列都可跨任务续跑。关闭开关会在
worker 下次启动时禁用该 schedule；已排队任务执行时会直接跳过。web 进程
始终不负责索引写入，不增加模型调用。

P13j 为热点榜增加独立的可重建统计投影 schema 18。迁移只建空表和变更队列，
不重算历史故事，也不改变现有榜单；后续分批构建、读切换另行验收。旧故事仍是门户
兼容层，不会因此冒充正式 stable event。详见
[`CURATION_HOT_METRICS.md`](CURATION_HOT_METRICS.md)。

P13k 增加可续跑的热点统计构建器：每事务最多 100 个旧事件，按当前发布版
可见性、重要性分和译题计算展示统计；用已知发布方身份去重而不是数采集源。
断点、统计行和 dirty 确认同事务提交。全量 Mac 副本演练见
[`p13k-hot-metrics-build-rehearsal.json`](evidence/p13k-hot-metrics-build-rehearsal.json)。

P13l 增加独立的热点读开关；只有统计投影 ready、dirty 为空且行数完整时
才读取新版报道数、已知发布方数、代表标题和按查询时刻衰减的热度。按频道和主题
筛选会检查可见成员，不假设旧事件频道一定与所有成员一致；未就绪时展示旧口径提示。
本机副本查询演练见
[`p13l-hot-metrics-read-rehearsal.json`](evidence/p13l-hot-metrics-read-rehearsal.json)。

P13m 把热点投影刷新接入 durable worker；仅在热点读开关启用时注册每分钟任务，
每次最多十批、每批至多 100 个事件。关闭开关时禁用日程，已入队任务跳过；
初始回填可用 maintenance 命令提前完成。构建中或存在 dirty 时仍回退到旧榜并提示。
