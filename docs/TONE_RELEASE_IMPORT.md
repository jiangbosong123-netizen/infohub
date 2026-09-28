# Tone 受控准入账本（P15e-3）

`app.tone_release_import` 是 P15e-2 人工决定进入 InfoHub 数据库的唯一受控入口。它会在写入前重新读取并校验
exact evidence bundle、受保护 approver registry、原始人工决定和候选 decision record；候选 record 必须与重算结果
逐字段一致。导入不会调用模型、不会写 `analysis_results`、不会移动 `analysis_publications`，也不会开放 `valid`。

数据库迁移 40 新增 `tone_release_admissions`。每行保存证据包与决定记录的规范 JSON、内容哈希、模型/校准/运行
身份和导入操作者。`decision_id` 与 `bundle_id` 都唯一，因此同一个人工决定不能重复使用，同一个冻结证据包也不能先
reject 后再改成 approve；需要重新评审时必须生成新的证据包。数据库触发器禁止更新和删除历史行，完整数据库校验会
重算 record SHA-256 并核对快照字段。

离线导入必须明确指定已完成迁移并通过校验的数据库：

```bash
python -m app.tone_release_import \
  --database /private/infohub/app.db \
  --bundle /private/release/tone-evidence-bundle.json \
  --registry /protected/ops/tone-release-approvers.json \
  --decision /private/release/human-decision.json \
  --record /private/release/validated-decision-record.json \
  --imported-by release-operator
```

approve 行只表示 `approval_candidate=1` 已进入不可变账本。P15e-4 仍需建立新的 tone 输出版本、shadow publication
和回滚规则；任何读取 API 在那之前都不能把本表当成可公开结果。reject 行只保留审计事实，永远不是发布候选。
