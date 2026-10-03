# Tone 人工标注批次导入（P15c-2）

`app.tone_review_intake` 将一名人工标注者的 tone 意见导入一个**新的私有数据集版本**。它不读取网络、
不调用模型、不修改源目录，也不产生最终 gold。即使同一 case 已有两份意见，`annotation.labels` 仍为空，
状态仍为 `single_annotator`，必须由后续第三人裁定。

## 输入边界

源数据集必须通过 `infohub.tone-evaluation/1.0`，被标 case 必须是 `restricted_reference`。合成 fixture
不得进入人工 intake。盲测数据集还必须先完成人工 holdout leakage review。

批次目录只含：

```text
manifest.json
reviews.jsonl
```

manifest 使用 `human-tone-review-batch-v1`，并绑定源 dataset version、manifest SHA-256、cases SHA-256、
reviewer_id，以及以下人工声明：

```json
{
  "source": "human",
  "independent": true,
  "model_assistance": false
}
```

每行 review 只能含 `case_id/content_sha256/recorded_at/labels`。labels 必须是完整
`infohub.tone-annotation/1.0`；不能只给 polarity，也不能在受限数据集中复制 quote 原文。quote 为 null，
但 quote_sha256 与 Unicode offsets 必须保留，供最后的私有 evidence review 对原文核验。

## 写入与并发规则

- batch 与输出放在私有路径；仓库内只允许 `evaluation/private/`，该目录被 gitignore。
- 输出目录必须不存在，dataset_version 必须变化；写入 staging，完整校验后原子 rename。
- review 的正文 hash 必须与冻结 case 一致；batch 必须与源 manifest/cases hash 完全匹配。
- 同一 reviewer 不能重复标同一 case，每个 case 最多两名独立 reviewer。
- provisional dataset 的最终 labels 必须保持空；AI 预标注不能作为人工意见导入。
- 新 labels 会改变 cases hash，因此旧 `tone_evidence_review` 签名必定作废且不会复制到新版本。
- 任一行错误时不创建输出目录，不留下部分数据集。
- manifest 只能包含上述 8 个字段，额外字段（如备注）会被拒绝，避免未入 hash 语义的旁路信息。

导入规则由 `app.review_intake` 统一实现，relevance、tone 与 impact 只在 batch schema、label 校验器、
manifest key 前缀和 evidence 签名字段上不同；`tests/test_review_intake.py` 对三者执行同一组规则测试。

调用方式：

```bash
python -m app.tone_review_intake \
  --dataset /absolute/private/path/tone-unlabeled-v1 \
  --batch /absolute/private/path/reviewer-a-batch \
  --output /absolute/private/path/tone-review-a-v1 \
  --dataset-version tone-review-a-v1
```

第二名 reviewer 必须以第一次输出为 source，再生成绑定新 hashes 的独立 batch。不要让第二份 batch 继续引用
最初未标注版本，否则会被当作 stale batch 拒绝。

## 当前仍未开放

本单元只解决安全导入。tone 专用 reviewer agreement、第三人 adjudication、私有 quote/artifact 复核、模型
prediction run、macro F1/unknown recall/切片报告和 release gate 仍需独立 PR。在这些条件完成前，
`publishable_tone_gold` 与生产 `valid` tone publication 均保持 false。
