# Tone 私有引用与规范文本复核（P15c-5）

`app.tone_private_evidence_review` 在第三人裁定后，把正式 tone 标签中的引用坐标重新对照私有、不可变的
规范文本 artifact，并将第四人的人工复核记录冻结进一个新的数据集版本。该过程不调用模型、不修改源数据集、
不复制新闻正文到输出目录，也不触碰生产数据库或 Windows。

## 输入边界

源数据集中的每个 case 都必须是 `restricted_reference` 且已完成两人独立 review 和第三人 adjudication。
工具拒绝只完成部分 case 的“全量通过”声明。artifact bundle 与人工 review 文件必须位于
`evaluation/private` 或代码仓库之外；输出也只能写到该私有区域，且目录必须尚不存在。

artifact bundle 包含 `manifest.json`、`artifacts.jsonl` 和 `text_ref` 指向的 UTF-8 文本文件。manifest 使用
`tone-private-artifacts-v1`，绑定源 dataset version、manifest hash、cases hash、artifact ledger hash 和 case 数量。
每个 ledger row 只允许：

```text
case_id · object_ref · content_sha256 · normalizer_version · text_ref · text_sha256 · size_bytes
```

`text_ref` 必须是 bundle 内的普通相对路径；绝对路径、`..`、反斜杠、符号链接、缺失文件和非普通文件均被拒绝。
`normalizer_version` 定义 offset 所属的规范文本坐标系。HTML/PDF 只有在上游先生成带版本和源 hash 的稳定文本
artifact 后才能进入该流程；本工具不会猜测浏览器文字、HTML 字节或 PDF 渲染坐标。

## 确定性核验

工具先核对 artifact ledger 与 exact source dataset，再对每个 case 的两份 reviewer labels 和最终 adjudicated label
逐条检查 evidence。offset 采用 Unicode code point 半开区间 `[start,end)`；切片 UTF-8 SHA-256 必须等于
`quote_sha256`。受限数据中的 `quote` 仍为 null。任一引用不匹配时整批失败，不产生输出。

人工记录使用 `tone-evidence-review-v1`，绑定源 manifest/cases、artifact manifest/ledger 和最终 cases hash，声明：

```text
source=human
model_assistance=false
all_cases_verified=true
quote_hash_and_offsets_verified=true
normalized_artifacts_verified=true
```

还必须提供 `verifier_id`、带时区的 `recorded_at` 与非空 `inspection_notes`。verifier 必须不同于全部 reviewer 和
adjudicator。程序能验证身份字符串与哈希链，不能证明现实身份或某人确实独立阅读，因此真实运行仍需流程负责人
核验人员与私有材料访问记录。

## 冻结命令

```bash
python -m app.tone_private_evidence_review \
  --dataset /absolute/private/path/tone-adjudicated-v1 \
  --artifacts /absolute/private/path/tone-artifacts-v1 \
  --review /absolute/private/path/tone-evidence-review.json \
  --output /absolute/private/path/tone-evidence-verified-v1 \
  --dataset-version tone-evidence-verified-v1
```

成功输出只复制 exact `cases.jsonl`、必要的 holdout review 和人工 `tone-evidence-review.json`，并在 manifest 中
记录 artifact hashes、normalizer versions、已核验 label/span 数量。私有正文 bundle 不被复制。所有文件先写临时
目录，通过完整 tone dataset 校验后原子 rename。

这一步只关闭引用溯源缺口。样本量、来源与困难切片覆盖、盲测、一致性、模型指标或校准未满足时，
`publishable_tone_gold` 仍为 false，生产 `valid` tone publication 仍保持关闭。
