# 分析结果验证与发布（P11c）

P11c 将 provider attempt 与可供门户/API使用的分析结果分开。只有 succeeded/refused attempt 可以进入验证；
输出必须匹配 run 的 schema 与主对象版本，声明证据列表，且所有引用都来自 P11a 输入白名单。confidence、
raw/calibrated confidence 和 intensity 必须位于 0..1。NaN、坏 JSON、伪 ID、错误对象或无证据的正常结论
不会发布。

`analysis_results` 分别保存原始响应引用/哈希、验证后的 JSON 和验证报告。`analysis_publication_versions`
追加每次发布或复核版本；`analysis_publications` 只是可重建的当前指针。结果、发布版本、指针、change_log
与 durable job 完成由一个事务提交。同一 run 不覆盖旧结果；模型比较应创建不同 run。

本阶段验证通用信封与证据安全。translation、relevance、summarization、importance 已有专用
策展契约；tone 从 P15b 起新 run 使用 `infohub.tone/1.1` 严格契约并逐字核验 raw CAS 引用，1.0
仅保留兼容读取；具体边界见 [TONE_ANALYSIS_CONTRACT.md](TONE_ANALYSIS_CONTRACT.md) 和
[TONE_EVIDENCE_VERIFICATION.md](TONE_EVIDENCE_VERIFICATION.md)。impact 等其余任务的专用字段约束随对应
能力 PR 增加。未通过固定评估集前，出现结构合法结果也不表示模型质量已达标。

P15e-4c-2 对新的 tone `valid` 结果增加独立发布门禁。结果必须绑定唯一 active tone activation，run 的
provider/model/prompt/pipeline/parameters/output schema 必须与该 activation 的冻结 runtime 完全一致，
每条 assessment 必须使用其 calibration version，并且 publication 的 evidence status 必须为 `supported`。
门禁在 publication 写事务内再次查询，防止检查后发生 rollback 的竞态。`analysis_results`、验证报告和
change payload 都保存 `tone_activation_id`；API 也返回该字段。非 tone 或非 `valid` 结果必须为 null。
