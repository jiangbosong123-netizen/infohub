# 所有者盲标导入（D23-a）

[D23 `single-owner-v1`](spec/DECISIONS.md#d23-单人所有者标注协议-single-owner-v12026-10-03) 让唯一标注者
（项目所有者）的盲标成为 **owner 层**最终标签：可以作为评估任何模型的真值，但只支持 experimental 结论，
永远不满足 `publishable_*_gold`。本页说明数据集里的新状态和 `app.owner_label_intake` 导入命令。

## 数据集里的三层状态

| `annotation.state` | 层 | 关键字段 | 规则 |
|---|---|---|---|
| `adjudicated` | gold | `reviews`（2 份）+ `adjudication` | 不变 |
| `owner_labeled` | owner | `labels` + `owner_label` | manifest 必须声明 `annotation_protocol={"version":"single-owner-v1","owner_id":...,"label_definition":...}`；`owner_label` 只能含 `owner_id/source/blind/model_assistance/content_sha256/recorded_at/labels`，必须 `source=human`、`blind=true`、`model_assistance=false`、owner 与 manifest 一致、hash 绑定冻结正文、`labels` 与最终 `labels` 完全相同 |
| `algorithm_labeled` | silver | `labels` + `labeler`，`generated_by_model=true` | 只能在 train/dev；`labeler` 只能含 `labeler_id/labeler_version/config_sha256/content_sha256/generated_at` |

三层互不混用：`owner_label` 不能出现在其他状态上，`labeler` 不能出现在 silver 以外，owner/silver case 不能
带 `reviews` 或 `adjudication`，两者都必须是 `restricted_reference` 真实数据。tone 和 impact 校验器对 owner/silver
标签执行与 gold 相同的任务契约，但不把它们计入 gold 的 assessment 数或目标缺口。

`single_annotator` 仍是“等待第三人裁定”的临时状态，labels 必须为空；它与 owner 层不同。

## 批次格式

批次目录只含 `manifest.json` 和 `reviews.jsonl`，放在仓库外或 git 忽略的 `evaluation/private/` 下。

```json
{
  "schema_version": "owner-label-batch-v1",
  "task": "relevance",
  "label_definition": "relevance-definition-v1",
  "source_dataset_version": "...",
  "source_manifest_sha256": "...",
  "source_cases_sha256": "...",
  "owner_id": "owner",
  "source": "human",
  "blind": true,
  "model_assistance": false
}
```

`task` 为 `relevance`、`tone` 或 `impact`，防止把一个任务的批次导入另一个任务。`label_definition` 是标注
定义版本（relevance 当前为 [`relevance-definition-v1`](../evaluation/ANNOTATION_GUIDE_V1.md#relevance-definition-v1)）；
第一次导入把 owner 与定义一起锁进 `annotation_protocol`，之后定义不同的批次被拒绝，避免同一数据集混用口径。每行 review 只含
`case_id/content_sha256/recorded_at/labels`，labels 必须是该任务的完整标签。

```bash
python -m app.owner_label_intake --task relevance \
  --dataset /absolute/private/path/relevance-unlabeled-v1 \
  --batch /absolute/private/path/owner-batch-1 \
  --output /absolute/private/path/relevance-owner-v1 \
  --dataset-version relevance-owner-v1
```

## 规则

- 与双人 review intake 共用同一核心（`app.review_intake`）：路径私有、输出目录必须是新的不可变目录、
  `dataset_version` 必须变化、batch 绑定源 manifest/cases 的 SHA-256 与每条正文 hash、解析的正是被 hash 的
  字节、blind holdout 未核验时在读取 batch 前拒绝、写 staging 后完整校验再原子 rename、失败不留部分输出。
- 只接收 `unlabeled` 且没有任何其他层痕迹的 case；已 owner 标注或已有双人意见的 case 都拒绝，避免同人
  覆盖首次判断或跨层切换。重标走后续的延时自复核流程，而不是重新导入。
- 第一次导入写入 `annotation_protocol` 并锁定 owner；之后 owner 不同的批次被拒绝。
- `blind=true` 表示标注时没有看到任何模型或算法的输出。看过算法标签后的确认或修改属于 silver 层，不能用
  这个命令导入。
- 新标签改变 cases hash，旧的 `tone_evidence_review` / `impact_evidence_review` 签名会被移除。

## 本地标注台（relevance）

`app.owner_label_console` 把未标注 case 的冻结内容展示给所有者，导出可直接导入的 owner 批次：

```bash
python -m app.owner_label_console serve \
  --dataset /absolute/private/path/relevance-unlabeled-v1 \
  --database /absolute/private/path/app-snapshot.db \
  --draft-dir /absolute/private/path/owner-drafts \
  --owner-id owner
# 浏览器打开 http://127.0.0.1:8013/ ，按 1/2/3 标 relevant / not_relevant / unknown，←/→ 翻页
python -m app.owner_label_console status ...      # 进度
python -m app.owner_label_console export ... --output /absolute/private/path/owner-batch-1
python -m app.owner_label_intake --task relevance --dataset ... --batch .../owner-batch-1 ...
```

- **盲标由工具保证。** 只从数据库读取来源标题、`raw_summary`、来源名和旧库时间，按抽样同一规则
  （`legacy-source-text-v2`）重算 `content_sha256`，不一致就拒绝显示和记录；模型字段（评分、AI 摘要、翻译、
  分类、`tmt`）从不被读取，也不显示 URL，避免看冻结内容以外的信息。因此导出的批次可以如实声明 `blind=true`。
- **顺序。** test → dev → train → security，同 split 内按固定种子打乱；中途停止也会先得到完整的 test 集。
- **草稿。** 每次选择追加到私有草稿（`<dataset_version>.<cases hash>.draft.jsonl`，fsync），导出前可改判，
  以最后一次为准；草稿绑定数据集 cases hash，不能混入其他数据集。草稿与批次目录必须在仓库外或
  `evaluation/private/` 下。
- **安全。** 只绑定 `127.0.0.1`，拒绝非本机 Host；表单需 CSRF token 和冻结 hash；内容由模板自动转义，
  键盘快捷键脚本使用逐请求 nonce 的 CSP。
- 导出后用 intake 生成新数据集版本；继续标注时以新版本为 `--dataset`，已导入的 case 不再出现。

数据库建议使用一致性快照副本（例如 `db-backup` 产物），抽样、入册和标注期间保持不变；源内容变化会被拒绝。

## 用 owner 标签评估旧 `tmt`

`classification-metrics-v4` 报告标出 `annotation_tier`（gold / owner / silver / synthetic / mixed）、
`experimental_claim_allowed` 与逐条 `claim_blockers`，并按预测行的 `slices` 输出分片 support、准确率、
Wilson 区间与 confusion。owner 层只有在 test split、预测完整、（blind holdout 时）holdout 已核验且延时自复核
完成后才允许 experimental 结论；自复核尚未实现，因此当前报告都是**初步数字**，blocker 会明确写出原因。
silver 层的数字只能称“与 silver 的一致率”。

```bash
python -m app.legacy_relevance_run \
  --dataset /absolute/private/path/relevance-owner-v1 \
  --database /absolute/private/path/app-snapshot.db \
  --output /absolute/private/path/legacy-tmt-test-run
```

旧库存储的 `tmt` 是 `官方来源 OR 关注公司 OR LLM 判断`（`app/ai/pipeline.py` `_keep_tmt`），不是纯 LLM
输出。因此每条预测带分片：`llm_only`（非官方、无关注公司，存储值就是 LLM 判断）、`policy_forced`（被产品规则
强制保留）、`unscored`（从未处理，计为弃权）。生成 run 前逐条按同一快照重算冻结内容 hash，防止用另一份数据库
的字段评估。输出目录包含 hash 绑定的 `run.json`、`predictions.jsonl` 与 `report.json`。

## 延时盲复核（D23-d）

单人无法做双人一致性，D23 用同一 owner 的延时盲重标替代：

- **固定样本。** `owner_recheck_sample` 取 test split 的 `max(30, ⌈10%⌉)` 条（不足则全部），按
  `sha256("owner-recheck-v1:" + case_id)` 排序截取。样本只由 case ID 决定，任何人都可重算，不能挑选。
- **只在到期后、看不到首次标签时重标。** 标注台 `--recheck` 模式只提供首次标注已满 7 天、尚未复核的样本，
  使用独立草稿，页面不显示首次标签；导出 `owner-recheck-batch-v1`。
- **导入。** `python -m app.owner_label_intake --task relevance --kind recheck ...` 只接受样本内、已 owner 标注、
  尚未复核、同一 owner 与同一定义、距首次标注 ≥7 天的行；写入 `annotation.owner_recheck`，**不改变最终 labels**。
  校验器对手工改写的数据集执行同样的样本、时间、owner、hash 规则。
- **报告。** `python -m app.owner_recheck --task relevance --dataset ...` 给出样本进度（到期、未到期、未标注）、
  关键标签（relevance 标签 / tone polarity / impact direction）的同人 confusion、observed/expected 与 Cohen κ。
  完成条件：样本全部 owner 标注并复核、至少 30 对、κ 有定义且 ≥0.70、没有未解决分歧。
- **与指标联动。** `classification-metrics-v4` 对 owner 层直接读取该报告，未完成时逐条列出原因；完成后 owner 层
  test 报告的 `experimental_claim_allowed` 才可能为 true（`quality_claim_allowed` 仍只属于 gold）。

## 复核分歧裁定（D23-e）

复核与首次标签不同的 case 需要 owner 写出最终决定与理由。这一步**不是盲的**：标注台 `--resolve` 模式同时
显示首次标签和复核标签，要求选择最终标签并填写 1–1000 字理由；导出的 `owner-resolution-batch-v1` 如实声明
`blind=false`，用 `owner_label_intake --kind resolution` 导入。

- 只接受仍有未裁定分歧的样本 case；同一 owner、同一定义；时间不早于复核。
- 写入 `annotation.owner_resolution`（含理由），并把最终 `labels` 改为裁定结果；首次标签与复核标签原样保留。
- 校验器要求：裁定只存在于确有分歧的复核之后，字段封闭，owner、hash、时间一致，最终 labels 等于裁定标签；
  tone/impact 对首次、复核与最终标签都执行任务契约。
- 同人 κ 仍按首次与复核两次独立判断计算，裁定不会改变 κ；它只把分歧标为已解决。

## relevance silver 标注器（D23-f）

量大时由算法补充 train/dev 标签。`app.silver_relevance_labeler` 是版本化的大模型标注器
（`relevance-llm-v1`），分三步，每步都写入新的私有不可变目录：

```bash
# 1. 标注：test 全部预测（用于考试），train/dev 只标仍未标注的 case；必须显式允许付费调用
python -m app.silver_relevance_labeler label --dataset DATASET --database SNAPSHOT.db \
  --output LABELS_DIR --env-file /path/to/.env --max-calls 40 --allow-paid-calls
# 2. 考试：在 owner 已标注 test 的数据集版本上生成 prediction run 与报告
python -m app.silver_relevance_labeler run --labels LABELS_DIR --dataset OWNER_VERSION --output RUN_DIR
# 3. 导入：把 train/dev 输出冻结成 algorithm_labeled（silver）新版本
python -m app.silver_relevance_labeler import --labels LABELS_DIR --evaluation RUN_DIR \
  --dataset OWNER_VERSION --output NEW_VERSION --dataset-version NEW_VERSION
```

- **输入与 owner 完全相同。** 只发送按 `legacy-source-text-v2` 重算 hash 一致的冻结标题与摘录（摘录最多
  1200 字），不发送任何标签或旧模型字段；提示词使用 `relevance-definition-v1`，并声明不执行内容里的指令。
- **版本与配置。** 模型名、服务主机、温度 0、关闭思考、批大小、摘录上限和提示词 hash 组成配置，配置 hash
  写入每个 silver case 的 `labeler.config_sha256`；不保存 API key。
- **预算与留档。** 批次数超过 `--max-calls` 时在任何调用前拒绝；`--dry-run` 不调用模型。每批请求、原始回复、
  token 用量与错误都保存在 `calls/`；格式错误或拒答重试一次，仍失败的 case 不给标签（`__no_label__`）。
  回复必须对每个输入 id 恰好给出一个受控标签，多出、缺少或自造标签都整批作废。
- **先考试再用。** 导入要求同一配置在**同一数据集版本**的 owner test 上有完整的评估 run，并在导入时从 hash
  绑定文件重新计算，不信任存档报告；结果（准确率、macro F1、是否达到 0.85 目标）写入新数据集 manifest 的
  `silver_labeler_evaluation`。
- **owner 优先。** 标注器运行后 owner 又标过的 case 在导入时跳过；silver 只进 train/dev，test 永远只有 owner 标签。

## 仍未实现

tone/impact 的标注界面与 silver 标注器尚未实现。
