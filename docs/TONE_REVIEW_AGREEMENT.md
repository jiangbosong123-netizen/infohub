# Tone 双人标注一致性（P15c-3）

`app.tone_review_agreement` 对指定的两名独立 reviewer 生成只读一致性报告。它不修改数据集、不裁定分歧，
也不把 adjudicated final label 当作任何 reviewer 的原始意见。

报告固定一个 reviewer pair 和一个 split（或 all），避免把不同人员组合的边际分布混在一起。它输出：

- polarity 5×5 confusion、observed agreement、expected agreement 与 Cohen κ；
- speaker、target、aspect、polarity、intensity_band、evidence、phenomena 和完整 label 的逐项 exact agreement；
- 配对样本按 split、language 和 source_kind 的支持数；
- 数据集覆盖不足、pair 覆盖不足和小样本告警。

evidence agreement 比较 quote SHA-256、Unicode `[start,end)` 和 offset unit 的集合，不受列表顺序影响。
完整 label agreement 则要求整条 speaker × target × aspect 判断完全相同。

```bash
python -m app.tone_review_agreement \
  --dataset /absolute/private/path/tone-review-two-v1 \
  --reviewer-a reviewer-a \
  --reviewer-b reviewer-b \
  --split test
```

SPEC 的 polarity κ 初始目标为 0.70。报告只有在至少 30 个配对样本时才可能将
`polarity_kappa_passed` 置为 true；单一类别的表面 100% 一致会使 expected agreement=1、κ 未定义，
不能伪装成完美一致。低支持切片继续显示计数，但不能用于质量门禁。

`quality_gate_eligible` 还要求正式私有 tone gold、test split 和完整 evidence/holdout admission；因此合成
fixture 或 provisional 数据集上的报告始终为 false。κ 达标只证明标注者对 polarity 的定义较一致，不能替代
target、aspect、引用范围或模型准确率检查。
