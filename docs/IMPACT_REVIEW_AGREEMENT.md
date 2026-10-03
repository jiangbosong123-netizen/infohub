# Impact 双人标注一致性（P16d）

`app.impact_review_agreement` 对指定的两名独立 reviewer 生成只读一致性报告。它不修改数据集、不裁定分歧，
也不把 adjudicated final label 当作任何 reviewer 的原始意见；只读取 P16c intake 写入的
`annotation.reviews`。

报告固定一个 reviewer pair 和一个 split（或 all），避免把不同人员组合的边际分布混在一起。reviewer 顺序
决定 confusion 的行列方向，但不改变 κ。它输出：

- `direction`、`expected_status`、`evidence_judgment` 三个受控分类字段各自的 confusion、observed
  agreement、expected agreement 与 Cohen κ；
- expected status、target、aspect、horizon、direction、intensity_band、evidence_judgment、evidence、
  mechanism 是否存在、phenomena、结构化 label 和完整 label 的逐项 exact agreement；
- 配对样本按 split、language 和 source_kind 的支持数；
- 数据集覆盖不足、pair 覆盖不足、小样本和单一类别 κ 未定义告警。

evidence agreement 比较排序后的 `evidence_ids` 与 `contradicting_evidence_ids` 两个集合。ID 由校验器绑定到
case 冻结的 event evidence 角色、quote hash 和 offsets，因此同一 ID 就是同一证据片段；把冲突证据改判成
支持、或漏掉 contradiction，都会计为不一致。

mechanism、assumptions 和 uncertainty_reason 是自由文本，逐字比较只能测量措辞而不是判断，因此
`structured_label` 只比较它们是否存在，其余受控字段全部逐项相等才算一致。措辞差异留给第三人裁定。
`full_label` 仍要求整条 assessment 字节级相同，用来发现“完全相同提交”，不作为质量指标。

```bash
python -m app.impact_review_agreement \
  --dataset /absolute/private/path/impact-review-two-v1 \
  --reviewer-a reviewer-a \
  --reviewer-b reviewer-b \
  --split test
```

SPEC 对关键标签的人工一致性初始目标为 κ≥0.70。impact 的门禁字段是 `direction`，与 tone 的 polarity
对应；只有至少 30 个配对样本时 `direction_kappa_passed` 才可能为 true。`expected_status` 是二分类且通常
高度不平衡，`evidence_judgment` 也是如此，它们的 κ 只作诊断，不单独放行。单一类别的表面 100% 一致会使
expected agreement=1、κ 未定义，不能伪装成完美一致。

`quality_gate_eligible` 还要求正式私有 impact gold、test split 和完整 evidence/holdout admission；因此
合成 fixture 或 provisional 数据集上的报告始终为 false。κ 达标只证明标注者对方向定义较一致，不能替代
target、horizon、证据角色、机制审查或模型准确率检查。第三人 adjudication、私有证据复核、模型指标和
release admission 仍是后续独立单元。
