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

本阶段只建立契约和旧字段转换规则，不调用模型、不批量导入生产数据，也不切换门户读取路径。
P13b 才会建立带 lease 的迁移 worker，把转换结果写入不可变 run/attempt/result/publication 链；
P13c 再建立经过发布指针控制的旧门户兼容投影。
