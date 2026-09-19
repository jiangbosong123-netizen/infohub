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

本阶段不批量导入生产数据，也不切换门户读取路径。P13c 再建立经过发布指针控制的
旧门户兼容投影。
