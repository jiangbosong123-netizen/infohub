# Impact 标注与固定评估集规范（P16b）

P16b 建立 impact 的人工标注单位、事件证据角色、困难切片和数据集放行规则。它不调用模型、不产生真实
准确率、不开放 `valid` publication，也不改变 Windows 生产环境。仓库中的
`evaluation/datasets/impact-contract-v1` 是 16 条中英合成契约样本，只证明校验器能拒绝错误结构、错误
时间范围、错误证据角色和把证据不足写成中性；它不是 gold 数据。

## 1. 标注单位

一条 case 只标一个：

`event_version × target × aspect × horizon`

同一事件影响两个 target、两个 aspect 或两个 horizon 时必须拆成不同 case，但继续引用同一个不可变
event version。同一 target/aspect/horizon 不能在一个 case 中重复。标注判断的是“固定证据是否支持这条条件
影响”，不是事件发生后证券价格是否上涨，也不是文章作者态度。

case 的最终标签位于 `annotation.labels.impact`。为与通用评估数据集保持兼容，它是恰好含一个 assessment
的数组。assessment 字段固定为：

- `event_version_ref`：必须等于 case 冻结的事件版本引用。
- `expected_status`：`needs_review` 或 `insufficient_evidence`。P16b 不能制造 `valid`。
- `target`：稳定 entity_id 和受控实体类型；标注阶段不创建临时自由文本实体。
- `aspect`：使用 `impact-vocabulary-v1`，与 `impact/1.0` 的受控词表一致。
- `horizon`：`immediate=0..7`、`quarter=8..90`、`long_term=91..730` 或
  `unspecified=null..null`；不能自行改变边界。
- `direction`：positive / negative / neutral / mixed / unknown，表示对所定义 aspect 和 target 的方向。
- `intensity_band`：zero / weak / moderate / strong / unknown。它是序数档，不是概率或收益幅度。
- `evidence_judgment`：supported / conflicting / insufficient。
- `evidence_ids` 与 `contradicting_evidence_ids`：只能引用 case 的冻结 event evidence，并匹配
  `supports` / `contradicts` 角色。
- `mechanism` 与 `assumptions`：条件因果链和显式假设；证据不足时不得补写推测机制。
- `phenomena`：受控困难切片，去重并按字典序保存。
- `uncertainty_reason`：unknown 或 insufficient 时必填；已知方向必须为 null。

人工真值不包含模型 confidence。模型 raw confidence、后续校准值和 release admission 属于独立阶段。

## 2. 方向、证据与拒判

`positive` / `negative` 描述 target 在该 aspect 下的有利或不利影响。比如成本下降对公司
`operating_cost` 负担可标 positive；它不是“成本数值向上”。如果业务含义容易混淆，mechanism 必须说明
方向。`neutral` 只用于直接证据支持影响为零或保持不变，intensity 必须为 zero。

`unknown` 是合格答案。事件事实存在但不能推出方向时，使用 unknown、unknown intensity 和明确原因。
没有直接影响证据时使用 `insufficient_evidence`，不得用 neutral 填空，也不得从事后行情、搜索结果、常识或
模型记忆补证据。

`needs_review` 必须至少有一条 role=`supports` 的直接事件证据和非空 mechanism。若材料存在实质冲突，
`evidence_judgment=conflicting`，同时引用 role=`contradicts` 的证据并进入 `conflicting_evidence` 切片。
支持与冲突 ID 不能重叠。公告元数据只证明“文件被提交”，不能直接证明文件中的收入或成本变化。

## 3. 困难切片

正式数据集必须在 manifest 中冻结每个切片的最低支持数：

- `conditional_plan`：结果依赖审批、执行或其他条件。
- `conflicting_evidence`：同一事件版本中有方向冲突的直接材料。
- `cross_entity`：政策、灾害或供应链事件影响另一实体。
- `direct_effect`：证据直接陈述已发生或明确生效的影响。
- `insufficient_evidence`：事件存在，但影响结论缺少直接支持。
- `numeric_revision`：actual / prior / revised 或同口径数字修订。
- `prompt_injection`：原文试图改变规则或强制标签，只能留在 security split。
- `unknown_direction`：target/aspect/horizon 已定义，但方向仍不可可靠判断。

结果按语言、来源、方向、状态、证据判断和 phenomena 报告分子/分母。支持数小于 30 的切片只能标低支持，
不能用点估计作质量声明。

## 4. 冻结证据与受限正文

每个 case 都有 `event_evidence` 目录，记录 evidence_id、事件角色、payload kind、截断状态、逐字 quote、
Unicode code point 半开区间和 quote SHA-256。generated metadata 或截断载荷不能成为直接 impact 证据。
合成 fixture 内嵌正文，校验器逐字核对。真实正文使用 `restricted_reference`，公共仓库只
保存 object_ref、内容 hash、quote hash 和 offsets，quote 必须为 null。

数值事实放在 `event_facts`，每项明确 `actual / prior / revised`、metric、十进制字符串 value、unit、period
和直接 evidence_id。`numeric_revision` case 必须有同 metric/unit/period 的 prior 与 revised 成对事实；浮点
自动改写、跨期间对比或只写“下降”文本都不能通过契约。

正式放行要求独立人工签署 `impact-evidence-review.json`，将 exact `cases.jsonl` hash、事件证据角色复核和
规范文本 artifact 复核绑定进 manifest。修改任一 case 或复核记录都会使证明失效。HTML/PDF 必须先转换为
版本化规范文本，offset 不能指向浏览器位置或不稳定的原始标记。

## 5. 人工流程

1. 入册 case 为 `unlabeled` 且 `labels={}`。
2. 第一名和第二名标注者独立提交完整 impact label；单人阶段为 `single_annotator`，最终 labels 仍为空。
3. 一致性必须分别比较 expected status、target、aspect、horizon、direction、intensity、证据、机制和困难切片。
4. 不同于两名 reviewer 的第三人裁定后，状态才可成为 `adjudicated`。
5. 第四人或独立复核职责核对私有 event evidence 与规范文本。
6. AI 可以生成候选预测，但 `generated_by_model=true` 不能成为人工 gold。

P16b 只实现标注契约、合成 fixture 和放行失败状态。P16c 已增加 hash 绑定、无模型辅助的双人
review intake；具体边界见 [`docs/IMPACT_REVIEW_INTAKE.md`](IMPACT_REVIEW_INTAKE.md)。它仍不会填写最终
labels，也不会把意见变成 gold。双人一致性、裁定、私有证据复核、指标、校准和 release admission
仍拆成后续小 PR；现有 tone 工作流不能改字段名后冒充 impact 工作流。

## 6. 正式数据集放行

`infohub.impact-evaluation/1.0` 至少要求：

- 300 个冻结 impact case、至少 150 个事件组和 300 个最终 assessment；中英文各至少 100；
- 至少 100 条 insufficient 或 conflicting case，另有 50 条 security case；
- train/dev/test/security 均非空，同事件、出处、内容、转载、翻译和修订不跨 split；
- 全部正式 case 使用受限正文引用，完成双人独立标注、第三人裁定和私有证据复核；
- 至少 3 个非 synthetic source kind，每个困难切片达到 manifest 冻结支持数；
- 来源数据库入册核验与时间/来源盲测人工复核完成。

任一条件不满足，`publishable_impact_gold=false`。单人数据只能称 experimental。方向 macro F1、证据支持率、
insufficient recall、人工一致性、校准和安全门槛属于后续评估与发布阶段，不能由 16 条 fixture 推导。

本地验证：

```bash
python -c "from app.impact_evaluation import validate_impact_evaluation_dataset as v; print(v('evaluation/datasets/impact-contract-v1').to_dict())"
```
