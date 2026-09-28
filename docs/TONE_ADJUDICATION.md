# Tone 第三人裁定（P15c-4）

`app.tone_adjudication` 将已有两份独立人工意见的 case 交给第三人裁定，并把决定冻结到一个新的私有
数据集版本。它不修改原数据集，不允许任一原 reviewer 担任 adjudicator，也不自动选择“多数意见”。

批次目录含 `manifest.json` 与 `decisions.jsonl`。manifest 使用
`human-tone-adjudication-batch-v1`，绑定源 dataset version、manifest hash、cases hash、adjudicator_id，
并声明 `source=human`、`model_assistance=false`。每行 decision 只能含：

```text
case_id · content_sha256 · recorded_at · labels · reason
```

labels 必须是完整 `infohub.tone-annotation/1.0`；reason 是不超过 2000 字符的人工裁定理由。
decision 时间不得早于两份 review，正文 hash 必须一致，受限正文仍不得嵌入 quote。

```bash
python -m app.tone_adjudication \
  --dataset /absolute/private/path/tone-review-two-v1 \
  --batch /absolute/private/path/tone-adjudicator-batch \
  --output /absolute/private/path/tone-adjudicated-v1 \
  --dataset-version tone-adjudicated-v1
```

成功后保留两份原 review，写入最终 `annotation.labels`、`state=adjudicated` 与 adjudication provenance。
输出目录必须不存在，所有内容先写 staging，经完整 tone dataset 校验后原子 rename。stale batch、重复 case、
不完整标签、原 reviewer 越权裁定、AI 辅助声明、过早时间或任一非法行都会使整批失败且不留下输出。

裁定会改变 cases hash，所以任何旧 `tone_evidence_review` 都被删除。最终数据集必须重新对私有冻结正文、quote
hash、offset 和规范文本 artifact 做 evidence review。达到样本数量、盲测、来源、困难切片、agreement、evidence
和模型指标之前，`publishable_tone_gold` 仍为 false，生产 `valid` tone publication 仍关闭。
