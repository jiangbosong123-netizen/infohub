# 相关性盲测发布门槛

`app.evaluation_release_gate` 把已冻结的分类预测与**盲测集**的双人标注一致性放在一起检查。运行只读，不调用模型或新闻源，不更改数据集。

```bash
python -m app.evaluation_release_gate \
  --dataset /path/to/private/adjudicated-dataset \
  --run /path/to/private/prediction-run/run.json \
  --reviewer-a reviewer-id-a \
  --reviewer-b reviewer-id-b
```

命令输出每一项检查及其分子/指标；未通过时退出码为 2。当前 v1 只支持 `relevance` 的 `test` split，要求：经验证的完整 gold 与盲测预测、两名指定审核者覆盖全部 test 案例、该 split 的 Cohen κ ≥0.70、`relevant` 召回率 ≥0.95、macro F1 ≥0.85，且三个真值类别各有至少 30 条。这些是正式 SPEC 的候选发布门槛；其他任务须有各自的评估与门槛，不能借此结果放行。`unknown` 是真值类别，模型拒答是另一个预测结果，二者不得混淆。

`quality_claim_allowed` 只代表分类报告满足结构性前提，**并不等于通过本门槛**。即使这里显示 `passed=true`，仍需人工核实审核者身份与独立性、盲测材料未进入训练或提示词、难例与来源切片，以及其他适用的安全和成本门槛。当前真实候选集未完成人工标注或裁定，所以没有实际发布通过记录。测试中的“通过”仅用合成报告验证判断逻辑，不代表真实模型质量。

该工具的首版要求固定同一对审核者覆盖全部 test 案例；若以后采用多审核者轮换，应另行设计多标注者一致性方法并修订 SPEC，不能将不同审核者组合的 κ 简单平均。私人报告含审核者 ID，保留在受限环境，不提交公开仓库。
