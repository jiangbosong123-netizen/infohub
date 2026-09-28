# Tone 授权人工发布决定（P15e-2）

`app.tone_release_decision` 将 P15e-1 的 exact evidence bundle、受控 approver registry 和人工 approve/reject
决定绑定为不可变候选记录。它不会因为 JSON 中写了“管理员”就信任该人：审批人必须在 registry 中 active、决定时间位于
授权窗口内并拥有 `tone:release:approve` scope，而且不能是 bundle 中的两名 annotation reviewer。

```bash
python -m app.tone_release_decision \
  --bundle /private/release/tone-evidence-bundle.json \
  --registry /protected/ops/tone-release-approvers.json \
  --decision /private/release/human-decision.json \
  --output /private/release/validated-decision-record.json
```

决定必须由人提交，`model_assistance=false`，并绑定 bundle 与 registry 的 SHA-256、版本和 ID。approve 还必须满足：

- evidence bundle 的全部门槛已经通过；
- 明确确认 blind holdout 没有进入训练、提示词选择或阈值调优；
- 确认 bundle 中的预算 policy ID；
- 确认已有回滚计划。

reject 可以记录不完整 bundle，便于保留失败原因，但永远不会成为 approval candidate。有效 approve 输出
`approval_candidate_for_controlled_import=true`；它仍不是数据库中的正式 admission。P15e-3 导入时必须再次校验
bundle/registry/record、权限与 append-only ledger 冲突，再决定是否允许 shadow publication。registry 属于受保护的
运维材料，不应把私人身份或权限清单提交公共 Git。
