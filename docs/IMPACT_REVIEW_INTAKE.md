# Impact 人工复核批次导入（P16c）

`app.impact_review_intake` 将一名人工标注者的 impact 意见导入一个**新的私有数据集版本**。它只处理
hash 已冻结的人工意见，不读取网络、不调用模型、不修改源目录，也不产生最终 gold。即使同一个 case
已经有两份意见，`annotation.labels` 仍为空，状态仍是 `single_annotator`，必须由后续独立裁定者决定。

## 输入边界

源数据集必须先通过 `infohub.impact-evaluation/1.0` 校验，被标 case 必须是
`text_storage=restricted_reference`。合成 fixture 不能进入人工 intake；blind holdout 还必须先通过
独立的 holdout leakage review。

批次目录只含两个文件：

```text
manifest.json
reviews.jsonl
```

manifest 使用 `human-impact-review-batch-v1`，并且只能包含以下字段：

```json
{
  "schema_version": "human-impact-review-batch-v1",
  "source_dataset_version": "...",
  "source_manifest_sha256": "...",
  "source_cases_sha256": "...",
  "reviewer_id": "...",
  "source": "human",
  "independent": true,
  "model_assistance": false
}
```

每行 review 只能含 `case_id/content_sha256/recorded_at/labels`。`labels` 必须是完整的
`infohub.impact-annotation/1.0` assessment；不能只提交 direction，也不能在受限数据集中复制原文或
证据 quote。受限证据的角色、hash 和 offsets 仍来自源 case，供后续私有 evidence review 使用。

## 写入与版本规则

- batch 与输出放在私有路径；仓库内只允许 `evaluation/private/`，该目录由 Git 忽略。
- 输出目录必须不存在，`dataset_version` 必须变化；程序先写 staging，完整校验后原子 rename。
- batch 必须精确绑定源 manifest/cases 的 SHA-256；每行正文 hash 也必须与冻结 case 一致。
- 源 manifest/cases 与 batch 文件都只读取一次，解析的正是被 hash 的那份字节；输出 manifest 记录的
  `parent_*_sha256` 与 `impact_review_batch_*_sha256` 因此精确描述本次导入内容。
- 同一 reviewer 不能重复标同一个 case；每个 case 最多两名不同 reviewer。
- reviewer 必须明确声明独立人工、无模型辅助；AI 预标注不能作为人工意见导入。
- 新 labels 会改变 cases hash，因此旧的 `impact_evidence_review` 签名不会复制到新版本。
- 任一行错误、重复或未知 case、空 batch、字段多余或输出目录已存在时，不创建部分输出。
- blind holdout 未通过 leakage review 时，在读取 batch 之前即拒绝。

这些规则由共享的 `app.review_intake` 实现，与 relevance、tone intake 完全相同；impact 只提供 batch
schema、`_validate_impact_label`、`impact_review_batch_*` manifest key 和 `impact_evidence_review` 签名字段。

调用方式：

```bash
python -m app.impact_review_intake \
  --dataset /absolute/private/path/impact-unlabeled-v1 \
  --batch /absolute/private/path/reviewer-a-batch \
  --output /absolute/private/path/impact-review-a-v1 \
  --dataset-version impact-review-a-v1
```

第二名 reviewer 必须以第一次输出作为 source，再提交绑定新 hash 的独立 batch。两份意见都保留在
`annotation.reviews` 中，但在第三人裁定前 `annotation.labels` 必须继续为空；这一步只表示意见已安全
入册，不表示数据已经可以训练模型或发布质量指标。

## 当前仍未开放

本单元只实现 hash 绑定、私有正文边界、双人 provisional intake 和失败原子性。两份意见的只读一致性
报告见 [`docs/IMPACT_REVIEW_AGREEMENT.md`](IMPACT_REVIEW_AGREEMENT.md)（P16d）。第三人 adjudication、
私有证据复核、模型 prediction run、指标、校准和 release admission
仍需独立 PR。在这些条件完成前，`publishable_impact_gold` 与生产 `valid` impact publication 均保持
false，Windows 生产环境不会被升级。
