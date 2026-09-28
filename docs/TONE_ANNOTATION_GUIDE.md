# Tone 标注与固定评估集规范（P15c）

P15c 建立 tone 的人工标注单位、困难切片和数据集放行规则。它不调用模型，不产生真实准确率，
不开放 `valid` publication，也不改变 Windows 生产环境。仓库中的
`evaluation/datasets/tone-contract-v1` 是 16 条中英合成契约样本，只用于证明校验器会拒绝错误结构、
错误引用和偷换标签；它不是 gold 数据。

## 1. 标注单位

一条 case 只标一个：

`观点/陈述 × speaker × target × aspect × document_version`

同一文档中有两个 speaker、两个 target 或两个独立观点时，必须拆成不同 case，并继续引用同一个冻结
document version。媒体转述某人的看法时，speaker 是被转述者；只有作者自己表达判断时才标 author。
引用某人“看好公司”不等于媒体、InfoHub 或该事件客观上看好公司。

case 的 gold 位于 `annotation.labels.tone`，字段固定为：

- `speaker`：kind、稳定 entity_id（如有）和可读 label；无法判断时用 unknown，不能猜实体。
- `target`：稳定 entity_id 与实体类型；对象不明时为 null，同时 polarity 必须为 unknown。
- `aspect`：使用 `tone-vocabulary-v1`，不得用自由文本悄悄增加类别。
- `polarity`：positive / negative / neutral / mixed / unknown。
- `intensity_band`：zero / weak / moderate / strong / unknown。它标表达强度，不标概率、新闻重要性或股价幅度。
- `evidence`：逐字 quote、Unicode code point 半开区间和 quote SHA-256。
- `phenomena`：受控困难切片，多标签、去重并按字典序保存。
- `uncertainty_reason`：只在 unknown 时必填；已知标签必须为 null。

标注不包含模型 confidence。confidence 属于待评模型的输出，不能写进人工真值。

## 2. polarity 与强度

| polarity | 使用条件 | intensity_band |
|---|---|---|
| positive | speaker 对 target/aspect 有明确正向评价 | weak / moderate / strong |
| negative | speaker 对 target/aspect 有明确负向评价 | weak / moderate / strong |
| neutral | 有逐字证据支持的中性立场或评价 | zero |
| mixed | 同一标注单位内同时存在不可合理拆开的正负表达 | weak / moderate / strong |
| unknown | 证据不足、target 不明或无法可靠判别 | unknown |

没有证据不是 neutral。事实句也不自动是 neutral：如果句子只报告“公司提交了文件”，而没有对某个 target/aspect
表达倾向，应不进入 tone case，或在抽样要求必须保留时标 unknown 并写明原因。

强度使用离散档位，避免人工制造无意义的小数精度。模型的 0..1 intensity 在 P15d 比较时按冻结边界投影到档位；
边界必须写入 prediction run 配置 hash，不能评估后调整。

## 3. 困难切片

每条 case 至少有一个 phenomena：

- `direct`：直接表达，未依赖转述结构。
- `negation`：否定、双重否定或否定范围会改变方向。
- `quotation`：引号中的逐字引语。
- `reported_speech`：间接引语或“某人称/认为”。
- `sarcasm`：字面与实际方向相反；只有语境足够时才判方向，否则 unknown。
- `mixed`：同一单位包含正负两面，且不能合理拆分。
- `ambiguous_target`：表达存在但目标无法可靠解析；target=null、polarity=unknown。
- `prompt_injection`：原文包含要求模型改规则、泄密或强制标签的指令；只作为不可信文本处理。

正式数据集必须在 manifest 中冻结每个切片的最低支持数。结果按语言、来源、speaker kind、phenomena 和
polarity 报告分子/分母；支持数小于 30 的切片标低支持，不能凭点估计作质量声明。

## 4. 引用与受限正文

合成 fixture 内嵌正文和 quote，校验器逐 Unicode code point 核对 `[start,end)` 与 SHA-256。真实新闻正文不得
推入公开仓库：case 使用 `restricted_reference`，只保留本地 object_ref、冻结正文 hash、quote hash 和 offsets，
`quote` 必须为 null。私有入册工具必须在源数据库上重算正文和 quote hash；仅靠公开 manifest 不能声称引用已核验。
正式放行还要求人工签署的 `tone-evidence-review.json`，并把 exact `cases.jsonl` hash、review record hash、
quote/offset 检查和规范文本 artifact 检查绑定进 manifest。修改任一 case 或复核记录都会使该证明失效。

HTML/PDF 必须先产生版本化规范文本 artifact，offset 指向该 artifact；不能把浏览器渲染位置或不稳定 HTML 字节位置
当作文字坐标。此规范文本生产链仍未实现，因此这些来源暂不具备 tone gold 入册条件。

## 5. 人工流程

1. 候选入册后为 `unlabeled`，没有 labels。
2. 每名标注者独立提交完整 tone label。单人阶段为 `single_annotator`，最终 labels 仍为空，不能当 gold。
3. 两名标注者都完成后计算 polarity、target、aspect 和证据 span 的一致性；不只比较 polarity。
4. 第三名、且不同于两名标注者的裁定者处理分歧，最终状态才是 `adjudicated`。
5. AI 可以生成独立的候选预测，但 `generated_by_model=true` 的内容不能成为人工 gold。

当前通用 review intake / adjudication 命令只支持 relevance，不能复用来伪造 tone 工作流。P15c-2 已增加
`app.tone_review_intake`，对第一、第二名人工 reviewer 使用完整 tone 契约并始终保留 provisional 状态；详见
`TONE_REVIEW_INTAKE.md`。tone 专用一致性与第三人裁定工具仍留给后续独立 PR。
`app.tone_evaluation` 对 provisional review 和最终 labels 使用同一结构校验。

## 6. 正式数据集放行

`infohub.tone-evaluation/1.0` 在通用 hash、分割和来源校验之上要求：

- 至少 600 个冻结文档/单元和 600 条最终 assessment；中英文各至少 200；
- 至少 50 条 security case；train/dev/test/security 均非空；
- 全部正式 case 为受限正文引用并完成双人独立标注和第三人裁定；
- 至少 3 个非 synthetic source kind，每个必需困难切片达到 manifest 冻结支持数；
- event、origin、相同内容、公告修订和翻译组不跨 split；
- 来源数据库入册核验和时间/来源盲测人工复核均完成。
- 私有 tone evidence review 与 exact cases hash 绑定，所有 quote/offset 和规范文本 artifact 均已复核。

以上任一条件不满足，`publishable_tone_gold=false`。合成 fixture 永远不能转成正式 gold。至少 200 条可裁定 held-out
以及 ECE ≤0.10 的校准放行属于 P15d/P15e，不由本阶段提前宣布。

本地验证：

```bash
python -c "from app.tone_evaluation import validate_tone_evaluation_dataset as v; print(v('evaluation/datasets/tone-contract-v1').to_dict())"
```

输出中的缺口是有意保留的真实状态：目前只有契约 fixture，没有私有 gold、基线结果或模型质量结论。
