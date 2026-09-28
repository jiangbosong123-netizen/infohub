# 文本语调输出契约（P15a）

P15a 只建立可审计的输出结构，不调用模型、不生成历史情绪、不启用生产任务，也不改变 Windows
生产环境。当前任务仍处于实验期：结构合法的结果只能写成 `needs_review`；`valid` 会被验证器拒绝。
逐字引用核验和固定评估集契约已经分别由 P15b/P15c 建立；真实私有 gold、模型基线和质量放行仍将在后续小 PR 完成。

## 1. 语义边界

tone 表示**文本中某个 speaker 对某个 target 的表达倾向**，粒度为：

`观点/陈述 × speaker × target × aspect × document_version`

它不表示事件的客观经营影响、证券价格方向、市场共识或交易建议。上述能力分别属于 impact、市场数据
或大系统。`neutral` 是有证据支持的中性表达；没有足够证据必须返回
`insufficient_evidence`，不能以 `neutral` 填空。

## 2. 版本与信封

- output schema：P15a 的结构版为 `infohub.tone/1.0`；P15b 当前写入版为
  `infohub.tone/1.1`，增加可验证 JSON Pointer locator。新 run 不再创建 1.0，旧 1.0 结果仍可读取。
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
      "locator": {
        "type": "json_pointer",
        "json_pointer": "/source_record/summary",
        "start_offset": 10,
        "end_offset": 26,
        "offset_unit": "unicode_code_point"
      }
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
可以为 null，但仍只能处于待审状态。系统会预检实体，并在 publication 写事务内再次查询实体目录：声明的
target/speaker entity_id 必须存在，target 及可确定类型的 speaker 还必须与目录类型一致；形似 ID 的自由
文本不能进入 publication。

aspect v1 为 product_capability、business_outlook、policy_stance、valuation_view、market_position、
management_quality、social_impact、other。词表变化必须提升版本，不能在同一版本中静默改义。

polarity 为 positive、negative、neutral、mixed、unknown。非 unknown 必须有 0..1 intensity；unknown
必须使用 null intensity 并说明 uncertainty_reason。intensity 只表示表达强度，不是概率、重要性或价格幅度。

每条 assessment 至少有一个不重复证据 span。1.1 locator 使用 RFC 6901 JSON Pointer 定位冻结 raw
payload 的字符串叶子，offset 使用 Unicode code point、半开区间 `[start,end)`。结构验证核对非负、顺序、
`end-start == len(quote)` 与 run 输入白名单；进入发布写事务前还会重新验证 CAS 路径、SHA-256、文件大小、UTF-8
JSON、pointer 和逐字切片。验证报告保存 payload/quote hash 和 locator。1.0 没有可判定字段坐标系，因此只保留
兼容读取，不标 quote verification passed。即使 1.1 引用完全匹配，当前仍禁止 `valid`，因为引用正确不等于
情绪分类质量已经通过固定评估集。

raw_confidence 是未校准模型自报值，可为 null。P15a 拒绝任何非空 calibrated_confidence；至少 200 条
可裁定 held-out、ECE 门槛和 calibration version 通过 admission 后，才能在新版本契约中启用校准值。

## 4. 后续放行顺序

1. P15b（已实现）：从冻结 JSON evidence payload 逐字核验 quote/span；HTML/PDF 的规范文本抽取仍需独立版本。
2. P15c（已实现契约）：固定中英合成 fixture、tone 专用标签结构、否定/转述/反讽等困难切片及正式
   私有 gold 的放行条件。当前没有把合成样本或通用 relevance 人工工具冒充 tone gold；详见
   `TONE_ANNOTATION_GUIDE.md`。
3. P15d：运行基线与候选评估，报告 macro F1、混淆矩阵、unknown recall、coverage 和支持数。
4. P15e：达到 SPEC 门槛后，以新 admission 记录开放 `valid`；shadow 切换，不覆盖旧结果。

任何阶段失败都只关闭新的 tone publication；不可变输入、attempt、旧结果和人工修正继续保留。
