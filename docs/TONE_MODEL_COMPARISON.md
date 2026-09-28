# Tone 基线与候选对比（P15d-2）

`app.tone_model_comparison` 在同一冻结数据集和 split 上比较两份已经通过 P15d-1 校验的 prediction run。
它不调用模型、不重算或修改 gold，也不会用不同样本、不同分母或不同切片覆盖来制造“提升”。

```bash
python -m app.tone_model_comparison \
  --dataset /absolute/private/path/tone-evidence-verified-v1 \
  --baseline-run /absolute/private/path/baseline/run.json \
  --candidate-run /absolute/private/path/candidate/run.json \
  --output /absolute/private/path/comparison.json
```

两次 run 必须有不同 run ID，并由各自的 manifest 提供 method ID/version。数据集版本、split、总分母、五类
support、语言/来源/phenomenon 切片集合及各切片 support 必须完全相同。两边都重新执行完整 hash、content version、
类别和置信度验证，不能提交手写汇总数。

`tone-model-comparison-v1` 报告 baseline/candidate 的 macro F1、unknown recall、coverage 及差值，同时报告每类
recall 差值和每个切片的 macro F1 差值。任一可比较指标退化超过 0.02，都会设置
`requires_regression_review=true`，要求解释并显式审议新基线，不能自动通过。

候选门还要求：

- 两边都是同一正式私有 test gold 上可作质量声明的完整运行；
- candidate macro F1 ≥0.80；
- candidate unknown recall ≥0.90；
- 没有超过 0.02 的总体、类别或切片退化。

`candidate_gate_passed` 只表示本对比契约满足这些候选条件，仍不等于生产 admission。人工身份、盲测隔离、
校准、成本、延迟、安全案例和 shadow 发布仍由后续门槛处理。当前仓库没有真实私有运行，对比测试只使用合成
fixture，因此 `comparison_eligible=false`。
