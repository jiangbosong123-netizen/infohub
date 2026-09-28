# Tone 发布证据包（P15e-1）

`app.tone_release_admission` 不接受手工填写的“已通过”汇总。它从同一私有数据集和冻结的原始运行材料重新计算：

- test 双人独立标注一致性；
- baseline 与 candidate 的分类指标和退化；
- dev-only temperature mapping 与独立 test 校准；
- test 全部 attempts 的结构、token、成本和延迟；
- 同候选配置的独立 security split。

```bash
python -m app.tone_release_admission \
  --dataset /private/tone-gold-v1 \
  --baseline-test-run /private/baseline-test/run.json \
  --candidate-dev-run /private/candidate-dev/run.json \
  --candidate-dev-calibration-input /private/candidate-dev/calibration.json \
  --candidate-test-run /private/candidate-test/run.json \
  --candidate-test-calibration-input /private/candidate-test/calibration.json \
  --candidate-test-operations /private/candidate-test/operations.json \
  --candidate-security-run /private/candidate-security/run.json \
  --reviewer-a reviewer-id-a \
  --reviewer-b reviewer-id-b \
  --output /private/release/tone-evidence-bundle.json
```

所有报告必须属于同一 dataset version；comparison、calibration、operations 和 security 引用的 candidate test run
必须完全一致；agreement、calibration 和 operations 的 test 分母必须相同。bundle 保存 dataset、run、calibration
input 和 operations manifest 的 SHA-256，`bundle_id` 由这些哈希、全部检查和关键测量值确定。底层 probability、prediction
和 attempt 文件已经由各自 manifest 再做一次哈希绑定。

只有私有 gold、完整 reviewer pair、κ、分类与基线、校准、运行预算和 security 全部通过，才会得到
`evidence_ready_for_human_review=true`。这个值表示材料完整，**不表示已经批准发布**。P15e-2 必须由有权限且不与
模型自评混同的人对 exact bundle hash 做 approve/reject 决定；本阶段不会写 analysis publication、不会打开 `valid`
tone，也不会改变生产环境。
