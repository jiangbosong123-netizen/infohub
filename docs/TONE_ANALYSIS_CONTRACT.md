# 文本语调输出契约（P15a）

P15a 只建立可审计的输出结构，不调用模型、不生成历史情绪、不启用生产任务，也不改变 Windows
生产环境。当前任务仍处于实验期：结构合法的结果只能写成 `needs_review`；`valid` 会被验证器拒绝。
逐字引用核验、固定评估集和质量放行将在后续小 PR 完成。

## 1. 语义边界

tone 表示**文本中某个 speaker 对某个 target 的表达倾向**，粒度为：

`观点/陈述 × speaker × target × aspect × document_version`

它不表示事件的客观经营影响、证券价格方向、市场共识或交易建议。上述能力分别属于 impact、市场数据
或大系统。`neutral` 是有证据支持的中性表达；没有足够证据必须返回
`insufficient_evidence`，不能以 `neutral` 填空。

## 2. 版本与信封

- output schema：`infohub.tone/1.0`
- vocabulary：`tone-vocabulary-v1`
- subject：tone 只接受不可变 document version；event version 留给 impact 等事件级任务。
- 顶层仍使用 `analysis_results` 的通用信封：schema_version、subject、status、evidence_ids、data。
- `insufficient_evidence` / `refused` 的 data 只能包含非空 `reason_code`。
- 当前 `valid` 被关闭；实验结果只能使用 `needs_review`，并进入现有追加式结果与 publication 账本。

## 3. `data` 结构

`needs_review` 的 data 必须包含一个版本化词表和至少一条 assessment：

```json
{
  "vocabulary_version": "tone-vocabulary-v1",
  "assessments": [{
    "speaker": {
      "kind": "quoted_person",
      "entity_id": "person:analyst",
      "label": "Analyst"
    },
    "target": {"entity_id": "organization:example", "type": "organization"},
    "aspect": "business_outlook",
    "polarity": "positive",
    "intensity": 0.7,
    "evidence": [{
      "evidence_id": "raw-record-id",
      "quote": "Demand improved.",
      "start_offset": 10,
      "end_offset": 26
    }],
    "confidence": {
      "raw_confidence": 0.8,
      "calibrated_confidence": null,
      "calibration_version": null,
      "uncertainty_reason": null
    }
  }]
}
```

speaker kind 为 author、interviewee、quoted_person、quoted_organization 或 unknown。已识别 speaker
至少要有 entity_id 或 label；unknown 不得伪造 entity_id。target 类型复用实体目录的受控类型，无法解析时
可以为 null，但仍只能处于待审状态。发布事务会查询实体目录：声明的 target/speaker entity_id 必须存在，
target 及可确定类型的 speaker 还必须与目录类型一致；形似 ID 的自由文本不能进入 publication。

aspect v1 为 product_capability、business_outlook、policy_stance、valuation_view、market_position、
management_quality、social_impact、other。词表变化必须提升版本，不能在同一版本中静默改义。

polarity 为 positive、negative、neutral、mixed、unknown。非 unknown 必须有 0..1 intensity；unknown
必须使用 null intensity 并说明 uncertainty_reason。intensity 只表示表达强度，不是概率、重要性或价格幅度。

每条 assessment 至少有一个不重复证据 span。offset 使用 Unicode code point、半开区间 `[start,end)`，
当前结构验证会核对非负、顺序和 `end-start == len(quote)`，并核对 evidence_id 属于该 run 的冻结输入。
它尚未从 CAS 重新读取原文逐字比较，所以本阶段禁止 `valid`。这一限制是显式发布门，而不是模型质量结论。

raw_confidence 是未校准模型自报值，可为 null。P15a 拒绝任何非空 calibrated_confidence；至少 200 条
可裁定 held-out、ECE 门槛和 calibration version 通过 admission 后，才能在新版本契约中启用校准值。

## 4. 后续放行顺序

1. P15b：从冻结 evidence payload 解析规范文本并逐字核验 quote/span；明确标题、摘要、全文字段坐标系。
2. P15c：固定多语种/多来源/引用类型 gold 数据、否定/转述/反讽切片与人工裁定流程。
3. P15d：运行基线与候选评估，报告 macro F1、混淆矩阵、unknown recall、coverage 和支持数。
4. P15e：达到 SPEC 门槛后，以新 admission 记录开放 `valid`；shadow 切换，不覆盖旧结果。

任何阶段失败都只关闭新的 tone publication；不可变输入、attempt、旧结果和人工修正继续保留。
