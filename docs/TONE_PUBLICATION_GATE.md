# Tone 有效结果发布门禁（P15e-4c-2）

本单元把 P15e-4c-1 的生产激活账本接到分析结果发布事务。迁移 44 给 `analysis_results` 增加可空的
`tone_activation_id`，并通过数据库 trigger 和完整性验证器共同执行 fail-closed 约束。

## 新发布的准入条件

新的 `tone` + `valid` 结果必须同时满足：

1. 数据库中恰好有一个 active tone activation；
2. analysis run 在 activation 之后创建；
3. run 的 provider、requested model、prompt template/hash、pipeline、parameters 和 output schema 与
   activation 冻结的 runtime config 完全一致，canonical runtime hash 也一致；
4. output 使用 `infohub.tone/1.1`，quote locator 通过 raw CAS 逐字核验；
5. 每条 assessment 都有非空 calibrated confidence，calibration version 精确等于 release 版本；
6. publication 的 evidence status 为 `supported`，并继续通过实体引用和通用分析信封验证。

这些检查先用于形成确定性的 request hash，然后在 `result + publication version + current pointer +
change_log + job completion` 的同一 `BEGIN IMMEDIATE` 事务内重新执行 active/runtime 检查。若检查与写入之间
发生 rollback，事务内检查会拒绝发布。

## 溯源和回滚语义

成功写入后，activation ID 同时存在于：

- `analysis_results.tone_activation_id`；
- `validation_report_json.tone_release`，包含 validator、runtime hash 和 calibration version；
- 对应的 `change_log.payload_json`；
- `/v1/analyses/{id}` 返回的 `tone_activation_id`。

非 tone 结果及非 `valid` tone 结果不得携带 activation ID。回滚后不允许任何新的 `valid` tone 发布；已经
原子提交的结果不会被删除或改写。相同 idempotency key、相同 request hash 的重试返回原 publication，即使
其 activation 后来已回滚；不同请求不能借此绕过门禁。

## 迁移与生产边界

schema 43 到 44 只添加可空列、索引和 insert trigger。旧 schema 中 `valid` tone 一直被应用验证器关闭，
因此现有结果无需回填 activation。迁移前备份、迁移后 integrity/foreign-key/schema verification 仍使用现有
数据库安全流程。

本单元只建立并测试门禁能力。它没有导入真实私有 gold、调用模型、批准预算、创建真实 production
activation、生成生产 `valid` tone 结果、切换门户读取或部署 Windows。
