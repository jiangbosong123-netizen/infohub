# Tone 运行成本与可靠性评估（P15d-5）

`app.tone_operational_evaluation` 对一份冻结 test prediction run 的**全部调用尝试**计算结构成功率、重试、延迟、
token 和费用。失败与修复尝试都进入分母和成本，供应商未返回 usage 时记为 unknown，不能按 0 费用处理。

`tone-operational-run-v1` 绑定 exact dataset、prediction run、predictions 和 `attempts.jsonl` 哈希，并记录经审批的
`policy_id` 及三项上限：每 1000 input token 费用、每 100 case 费用和 p95 延迟。policy 身份必须在最终 release
review 中核实；候选模型不能靠提交一份宽松 policy 自行批准自己。

每个 case 必须从 attempt 1 的 `primary` 开始，编号连续，最多三次；后续只能是 retry/repair，结构修复最多一次。
provider 调用失败后只能 retry，调用成功但 schema 不合格后只能 repair；尝试不能重叠，得到有效结果后不能继续调用。
每条 attempt 保存带时区的毫秒时间、状态、schema_valid、usage 状态、input/output token 和微美元费用。unknown
usage 的三个数值必须全部为 null，并阻断放行。p50/p95 使用包括失败调用在内的 attempt 延迟 nearest-rank。

```bash
python -m app.tone_operational_evaluation \
  --dataset /absolute/private/path/tone-evidence-verified-v1 \
  --run /absolute/private/path/candidate-test/run.json \
  --operations /absolute/private/path/candidate-test/operations.json \
  --output /absolute/private/path/candidate-test/operations-report.json
```

`admission_ready` 要求：正式私有 blind-test gold、完整预测、每个 case 有尝试、最终结构合格率 100%、首次结构
合格率至少 99%、usage 全部已知、input token 非零，且两项成本和 p95 延迟均在 policy 内。通过仍不代表 tone
可以发布；安全 split、人工一致性、分类指标、相对基线、校准和人工 release decision 需要在 P15e 一起核验。
