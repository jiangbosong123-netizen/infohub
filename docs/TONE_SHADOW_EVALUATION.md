# Tone 影子观测与质量门槛（P15e-4b）

P15e-4b 把一次 `running` rollout 的完整候选总体冻结为不可变 batch，再按 rollout ID、document version ID 和
`sample_bps` 做确定性抽样。数据库同时保存总体中的每个成员、selection hash 和 selected 标志，因此可以独立复核
总体清单、抽样结果和缺失记录，不能只提交表现好的样本。

每个被抽中的 document version 必须且只能写一条 observation：

- `matched`：候选与冻结参考一致；
- `disagreed`：两者都有结果但不一致；
- `error`：候选无法形成可比较结果，并记录非空错误码。

数据库只保存结果指纹和分类结果，不复制受限原文。观察必须发生在配置的窗口内；rollout 暂停或终止后不能继续写。
只有全部 selected members 均有 observation，才可生成唯一 evaluation。evaluation 从不可变行重算错误率和分歧率，
并对照 rollout 中的 minimum observations、maximum error bps 和 maximum disagreement bps。

迁移 42 还收紧了 rollout 状态机：`running -> completed` 必须绑定同一 rollout 的 `passed` evaluation；failed 或
缺失 evaluation 均会由数据库触发器拒绝。完整数据库校验会重算总体 manifest、确定性抽样、metrics hash、decision
和完成状态绑定。

本阶段仍不调用模型、不写正式 analysis publication、不开放 `valid`、不改变门户/API。P15e-4c-1 已增加受控人工
激活与即时回滚账本，详见 `TONE_RELEASE_ACTIVATION.md`；P15e-4c-2 才把正式 publication 事务接入 active profile。
真实 shadow worker 与真实观测数据也必须在后续显式启用。
