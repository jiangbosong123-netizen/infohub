# InfoHub NLP 标注指南 v1

## 原则

标注者只使用样本 manifest 中的固定证据，不使用搜索、记忆或事后行情。`unknown` 是合格答案；证据不足
时不能为了覆盖率猜测。模型可生成预标注，但正式 gold 必须由人独立判断。关键 event/impact 标签要求两名
标注者，分歧经第三步裁定；只有裁定完成才能标 `adjudicated`。

## 分组与切分

同一事件、同一原始出处、转载、翻译、公告修订必须留在同一个 split。`security` 用于提示注入、极长输入、
畸形结构和伪证据，不混进自然分布准确率。最后一段时间及至少一个来源应留作盲测。

## 主要标签

- relevance：`relevant / not_relevant / unknown`。拒判不是不相关。
- event relation：`same_event / related / distinct / uncertain`。财报期、产品版本、发行批次不同通常是 distinct；
  否认与宣布是 related，不能自动合并。
- tone：按 speaker × target × aspect 标 `positive / negative / neutral / mixed / unknown`，引用他人观点不等于作者观点。
- impact：按 event version × target × aspect × horizon 标方向、机制、假设与证据。证据不足为
  `insufficient_evidence`，不是 neutral；实际股价涨跌不作为新闻语义真值。

数字、主体、否定、时态和证据引用属于关键字段。一个关键错误可阻断该能力发布。标注修改追加新数据集版本，
不能覆盖已用于报告的旧 gold。
