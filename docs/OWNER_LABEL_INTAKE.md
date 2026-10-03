# 所有者盲标导入（D23-a）

[D23 `single-owner-v1`](spec/DECISIONS.md#d23-单人所有者标注协议-single-owner-v12026-10-03) 让唯一标注者
（项目所有者）的盲标成为 **owner 层**最终标签：可以作为评估任何模型的真值，但只支持 experimental 结论，
永远不满足 `publishable_*_gold`。本页说明数据集里的新状态和 `app.owner_label_intake` 导入命令。

## 数据集里的三层状态

| `annotation.state` | 层 | 关键字段 | 规则 |
|---|---|---|---|
| `adjudicated` | gold | `reviews`（2 份）+ `adjudication` | 不变 |
| `owner_labeled` | owner | `labels` + `owner_label` | manifest 必须声明 `annotation_protocol={"version":"single-owner-v1","owner_id":...}`；`owner_label` 只能含 `owner_id/source/blind/model_assistance/content_sha256/recorded_at/labels`，必须 `source=human`、`blind=true`、`model_assistance=false`、owner 与 manifest 一致、hash 绑定冻结正文、`labels` 与最终 `labels` 完全相同 |
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
  "source_dataset_version": "...",
  "source_manifest_sha256": "...",
  "source_cases_sha256": "...",
  "owner_id": "owner",
  "source": "human",
  "blind": true,
  "model_assistance": false
}
```

`task` 为 `relevance`、`tone` 或 `impact`，防止把一个任务的批次导入另一个任务。每行 review 只含
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

## 仍未实现

延时自复核抽样与同人一致性、experimental 指标结论、把受限 case 渲染给所有者阅读的本地标注工具、
silver 标注器均在后续 PR。现在只能导入 owner 标签，不能据此发布任何质量结论。
