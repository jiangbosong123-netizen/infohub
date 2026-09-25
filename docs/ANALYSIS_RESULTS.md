# 分析结果验证与发布（P11c）

P11c 将 provider attempt 与可供门户/API使用的分析结果分开。只有 succeeded/refused attempt 可以进入验证；
输出必须匹配 run 的 schema 与主对象版本，声明证据列表，且所有引用都来自 P11a 输入白名单。confidence、
raw/calibrated confidence 和 intensity 必须位于 0..1。NaN、坏 JSON、伪 ID、错误对象或无证据的正常结论
不会发布。

`analysis_results` 分别保存原始响应引用/哈希、验证后的 JSON 和验证报告。`analysis_publication_versions`
追加每次发布或复核版本；`analysis_publications` 只是可重建的当前指针。结果、发布版本、指针、change_log
与 durable job 完成由一个事务提交。同一 run 不覆盖旧结果；模型比较应创建不同 run。

本阶段验证通用信封与证据安全。translation、tone、impact 等任务的专用字段约束随对应能力 PR 增加；
未通过固定评估集前，出现结构合法结果也不表示模型质量已达标。
