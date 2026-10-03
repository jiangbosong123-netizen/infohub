# Tone 影子运行计划（P15e-4a）

P15e-4a 只建立受控 shadow rollout 的计划和状态机。它不生成 tone 结果、不调用模型、不移动
`analysis_publications`，也不让门户或 API 读取候选输出。迁移 41 新增：

- `tone_shadow_rollouts`：绑定一个 P15e-3 approved admission、候选 test run、calibration version、抽样比例和
  固定观察门槛；模式只能为 `shadow_only`。
- `tone_shadow_rollout_transitions`：追加式状态历史，使用 expected previous transition 防止两个操作者相互覆盖。

状态只能按以下路径前进：

```text
planned -> running -> completed
    |          |  \
    |          v   -> aborted
    |        paused -> running
    |          |
    +----------+-----> aborted
```

`completed` 和 `aborted` 都是终态。所有 rollout 与 transition 行不可更新、不可删除；完整数据库校验会重算配置
hash、核对 approved admission 身份并重放整个状态链。一个 admission 只能建立一个配置确定的 rollout。

本阶段中的 `running` 仅表示允许后续 shadow worker 采集对照结果，并不代表已有 worker，也不代表任何请求会看到
候选结果。P15e-4b 已定义完整总体清单、确定性抽样、不可变 observation、错误/分歧统计和完成门槛，详见
`TONE_SHADOW_EVALUATION.md`。P15e-4c 才能设计独立的人工切换与即时回滚，且不能复用当前会直接移动正式指针的
通用 publication 写入函数。
