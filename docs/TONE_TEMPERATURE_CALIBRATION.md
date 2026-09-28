# Tone 温度校准映射（P15d-4）

`app.tone_temperature_calibration` 对同一冻结模型的完整五分类概率做 multiclass temperature scaling。
拟合只读取 `dev` gold；独立 `test` gold 只在映射冻结后用于一次复评，不能反向选择 temperature。

输入是 P15d-3 已校验的两组 run 和 calibration input：

```bash
python -m app.tone_temperature_calibration \
  --dataset /absolute/private/path/tone-evidence-verified-v1 \
  --fit-run /absolute/private/path/candidate-dev/run.json \
  --fit-input /absolute/private/path/candidate-dev/calibration.json \
  --test-run /absolute/private/path/candidate-test/run.json \
  --test-input /absolute/private/path/candidate-test/calibration.json \
  --output /absolute/private/path/candidate-calibration/report.json
```

两份 run 必须属于同一 dataset、`method_id`、`method_version` 和 `method_config_sha256`，run ID 必须不同，
并分别固定为 `dev` 和 `test`。两边都不允许缺失或拒答概率。算法在固定 temperature 区间 `[0.05,20]`
内最小化 dev multiclass negative log likelihood；输出的 `calibration_version` 由算法、模型身份、dev run/input
哈希和拟合值确定，不包含 test input，因此换 test 数据不会悄悄产生另一份拟合映射。

报告比较 test 上校准前后的 multiclass Brier、ECE 和 10 个固定 reliability bins。只有同时满足以下条件，
`admission_ready` 才为 true：

- 私有 gold 已正式放行；
- dev 和 test 各至少 200 条、概率覆盖完整；
- test classification run 具备正式质量声明资格；
- dev NLL、test Brier 和 test ECE 均不退化；
- test ECE ≤0.10。

test 参与的是独立验收，不参与拟合。通过本报告仍不会自动写入 `calibrated_confidence`、修改 analysis publication
或部署生产；P15e 需要再把模型质量、人工一致性、安全切片、成本/延迟和该 calibration version 汇总到一次明确的
release admission。公开仓库的 16 条合成 fixture 只有 4 条 dev 和 6 条 test，永远不能形成真实校准结论。
