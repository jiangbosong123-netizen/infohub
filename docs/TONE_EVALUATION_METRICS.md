# Tone 分类评估指标（P15d-1）

`app.tone_evaluation_metrics` 对冻结的 tone polarity 预测运行做只读、确定性评估。它不调用模型、不产生预测、
不修改 gold、数据库或生产环境。规则基线与候选模型必须使用同一 prediction run 契约，失败、缺失和拒答都进入
固定分母，不能只统计成功请求。

## Prediction run

`tone-prediction-run-v1` 的 `run.json` 固定 dataset version、exact manifest/cases hashes、任务/输出/vocabulary
契约、dev/test/security split、方法与配置 hash、生成时间以及 exact predictions hash。`predictions_file` 只能是同目录
普通文件名。每行仅允许：

```text
case_id · content_sha256 · predicted_polarity · raw_confidence
```

polarity 只能是 `positive/negative/neutral/mixed/unknown` 或显式 `__abstain__`。raw confidence 可为 null，
非空时必须是 0..1 的有限数；本阶段不会把它冒充 calibrated confidence。缺失 case 自动计为 abstain；重复 ID、
跨 split case、旧 content hash、额外字段、非法类别和坏 hash 全部拒绝。

## 报告口径

报告使用 `tone-classification-metrics-v1`，包含：

- 五个固定真值类别的 support、predicted、precision、recall、F1；
- 完整混淆矩阵，`__abstain__` 是预测结果而非真值类别；
- 五类固定宏平均 F1，因此缺失类别不能从分母消失；
- unknown recall、coverage、abstained 和 missing predictions；
- accuracy 及 Wilson 95% 区间；
- language、source kind 与每个 phenomenon 的相同切片指标和支持数；
- 小于 30 条的切片明确标记 `low_support=true`。

security split 与自然分布准确率分开报告。只有 test split、正式 `publishable_tone_gold`、完整预测和至少一个非拒答
结果同时成立时，`quality_claim_allowed` 才可能为 true；这仍不是发布批准。P15d 后续 release gate 还要检查
macro F1 ≥0.80、unknown recall ≥0.90、各关键切片、人工一致性和相对基线退化。校准与 ECE 另行评估。

```bash
python -m app.tone_evaluation_metrics \
  --dataset /absolute/private/path/tone-evidence-verified-v1 \
  --run /absolute/private/path/tone-baseline-test-v1/run.json \
  --output /absolute/private/path/tone-baseline-test-v1/metrics.json
```

仓库中的测试只使用 16 条合成契约 fixture 验证计算和失败边界，不代表真实模型准确率。
