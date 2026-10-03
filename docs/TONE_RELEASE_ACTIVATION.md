# Tone 生产激活与回滚账本（P15e-4c-1）

本单元在迁移 43 中建立生产激活控制面。它只接受已经完成且绑定 `passed` shadow evaluation 的 rollout，并将以下材料写入不可变账本：

- admission 中哈希绑定的 candidate test run manifest；
- `tone-production-profile-v1` 生产配置；
- 拥有 `tone:release:activate` scope 的人工激活请求；
- 激活时使用的 registry 版本、哈希和授权条目快照。

生产 profile 的 `runtime_config` 精确包含 provider、requested model、prompt template/hash、pipeline、parameters、output schema 和 calibration version。其 canonical SHA-256 必须等于已评估 candidate run 的 `method_config_sha256`，因此不能把通过评估的候选名称替换成另一套实际运行参数。candidate run 文件本身的字节哈希还必须等于 admission evidence bundle 中冻结的 artifact hash。

激活人必须处于 registry 有效期内、拥有 `tone:release:activate`，且不能是 release approver、shadow evaluator 或两名 annotation reviewer。激活时间不得早于 profile、candidate run、shadow evaluation 或 rollout completion。全库同一时刻最多存在一个 active tone profile。

```bash
python -m app.tone_release_activation activate \
  --database /absolute/path/app.db \
  --rollout-id TONE_ROLLOUT_ID \
  --candidate-run /protected/candidate-test/run.json \
  --profile /protected/release/production-profile.json \
  --registry /protected/ops/tone-release-approvers.json \
  --request /protected/release/activation-request.json
```

回滚是追加式 `active -> rolled_back`，需要拥有 `tone:release:rollback` scope，并通过 expected previous transition 防止并发操作者覆盖。回滚不删除 admission、rollout、evaluation 或旧分析结果；同一 activation 不能恢复为 active，要再次启用必须形成新的 evidence/admission/rollout/activation 链。

```bash
python -m app.tone_release_activation rollback \
  --database /absolute/path/app.db \
  --activation-id TONE_ACTIVATION_ID \
  --expected-previous-transition-id CURRENT_TRANSITION_ID \
  --registry /protected/ops/tone-release-approvers.json \
  --request /protected/release/rollback-request.json
```

P15e-4c-2 已通过迁移 44 将新的 tone `valid` publication 接入该账本。发布前和 publication 的同一写事务内
都会重新检查 active 状态、exact runtime、calibration 与证据状态；成功结果保存 activation ID，rollback 后
立即拒绝新发布。对已经成功提交的同一请求，幂等重试仍返回原结果，旧的不可变结果继续保留。完整规则见
[TONE_PUBLICATION_GATE.md](TONE_PUBLICATION_GATE.md)。

这些代码仍未调用真实模型、创建真实生产 activation、移动现有 production pointer 或部署 Windows。
