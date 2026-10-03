# 事件影响输出契约（P16a）

P16a 只建立与当前分析账本一致的、可审计的 `impact/1.0` 输出契约。它不调用模型、不生成历史影响结果、
不输出价格预测或交易建议、不开放 `valid`，也不改变 Windows 生产环境。

## 语义与对象

impact 表示：

`不可变事件版本 × target × aspect × horizon`

它判断在给定证据和明确假设下，事件可能怎样影响某个公司、证券、行业、地区或宏观对象。它不等同于文章
speaker 的态度（tone），也不等同于证券未来涨跌。run 的 subject 必须是 event version；target 必须在实体
目录存在且类型一致。

首版 aspect 固定为 revenue、operating_cost、margin、capex、funding、supply、demand、
regulatory_constraint、employment、other。horizon 只能是：

- immediate：0–7 天；
- quarter：8–90 天；
- long_term：91–730 天；
- unspecified：min/max 均为 null。

模型不能改变这些边界，也不能把 unknown 填成 0。direction 为 positive、negative、neutral、mixed 或
unknown；known direction 必须有 0..1 intensity，unknown 必须使用 null intensity 并说明 uncertainty。

## 证据与置信度

每条 assessment 必须有 target、aspect、horizon、direction、mechanism、assumptions 和至少一个支持证据。
支持与冲突证据不能重叠。所有 ID 必须在 analysis run 的冻结输入内；发布前还会检查它们是否以 `supports`
或 `contradicts` 角色挂在 subject event version 上。`generated_metadata` 或 truncated raw payload 不能作为直接
impact 证据。

raw confidence 是未校准模型自报值，可为 null。当前 calibrated confidence 和 calibration version 必须为
null。结构正确只允许 `needs_review`；证据不足和 provider 拒绝分别使用只含 reason_code 的
`insufficient_evidence` / `refused` 数据。

## 发布边界

输出使用通用 analysis envelope：schema version、event subject、status、总 evidence IDs 和 data，不允许额外
字段。`needs_review` 的总 evidence IDs 必须不重不漏地等于所有 assessment 的支持与冲突证据并集。验证后的
结果、task validation report、publication version、change log 和 job completion 继续由同一事务提交。实体在
事务前和事务内都检查，避免目录变化竞态。

P16b 已建立 impact 专用标注契约、困难切片和 16 条中英合成 fixture，详见
[IMPACT_ANNOTATION_GUIDE.md](IMPACT_ANNOTATION_GUIDE.md)。后续 P16 单元继续实现双人复核、裁定、私有
证据复核、指标、校准、release admission、shadow rollout 和 production activation。未完成这些条件前，
任何合成 fixture 都不能被称为真实 impact 质量结论。
