# 分析调用、费用与重试审计（P11b）

P11b 在 provider 调用前创建不可变授权。授权绑定 P11a run、当前 durable-job lease、attempt 序号、类型、
provider、预算策略和保守费用上限。系统在事务中累计 UTC 当日已允许额度；超过单次或每日上限时写入
`blocked` 决策，且禁止记录为真实 provider 调用。

调用完成后，`analysis_attempts` 追加 resolved model、provider request ID、起止时间、状态、token、usage
来源、微美元费用、计价版本、原响应引用/哈希和错误。`refused`、`invalid_output`、`failed` 与成功分开；
provider 未返回用量时必须为 unknown，不能填零。所有表禁止修改和删除。

首个调用必须是 primary；普通自动尝试总数最多三次；结构修复最多一次，并且只允许紧跟
`invalid_output`。授权后未记录结果前不能领取下一次调用，避免并发重复收费。本阶段使用模拟调用验证，
不接真实 provider；输出 schema 与证据验证及正式结果发布属于 P11c。
