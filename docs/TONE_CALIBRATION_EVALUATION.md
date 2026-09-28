# Tone 置信度校准评估（P15d-3）

`app.tone_calibration_evaluation` 对冻结 tone prediction run 的**原始五分类概率**计算 multiclass Brier score、
10 桶 reliability 数据和 Expected Calibration Error（ECE）。它不训练校准器、不生成 `calibration_version`，
也不允许把 raw confidence 直接展示成 calibrated confidence。

校准输入使用 `tone-calibration-input-v1`，绑定 exact dataset manifest/cases、prediction run 文件、predictions
文件和 probabilities 文件的 SHA-256。每个 split case 在 `probabilities.jsonl` 中必须恰好出现一次：

```text
case_id · content_sha256 · probabilities
```

正常预测必须提供五个 polarity 的完整概率向量。每个值须为 0..1 有限数，总和在 `1e-9` 内等于 1；唯一 argmax
必须等于冻结预测，最大概率必须与原 run 的 `raw_confidence` 在 `1e-12` 内一致。拒答或缺失预测只能填写
`probabilities=null`，仍保留在 `total` 和 coverage 分母，不能通过删除失败项改善 ECE。

```bash
python -m app.tone_calibration_evaluation \
  --dataset /absolute/private/path/tone-evidence-verified-v1 \
  --run /absolute/private/path/candidate-test/run.json \
  --calibration-input /absolute/private/path/candidate-test/calibration.json \
  --output /absolute/private/path/candidate-test/calibration-report.json
```

报告使用 `tone-calibration-report-v1`。Brier score 为每个样本五类 `(p-y)^2` 之和的平均值；ECE 使用固定等宽
区间 `[0.0,0.1)` 至 `[0.9,1.0]`，按各桶样本数加权绝对 calibration gap。空桶保留 count=0，均值与准确率为
null，不能用插值伪造 reliability curve。

只有同时满足以下条件，`calibration_admission_ready` 才为 true：

- 正式可发布私有 gold 的 test run；
- 至少 200 条 held-out case；
- 每条都有完整概率，拒答或缺失为 0；
- ECE ≤0.10。

通过仍只说明这份 raw probability report 可进入后续 admission。真正的 calibration mapping 必须在独立数据上拟合、
版本化并重新评估，不能在同一 test gold 上拟合后再报告。当前合成 fixture 只有 6 条 test case，永远不能形成校准结论。

