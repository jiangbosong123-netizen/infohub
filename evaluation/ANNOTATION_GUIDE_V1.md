# InfoHub NLP 标注指南 v1

## 原则

标注者只使用样本 manifest 中的固定证据，不使用搜索、记忆或事后行情。`unknown` 是合格答案；证据不足
时不能为了覆盖率猜测。模型可生成预标注，但正式 gold 必须由人独立判断。关键 event/impact 标签要求两名
标注者，分歧经第三步裁定；只有裁定完成才能标 `adjudicated`。
开始标注前，在私有库中按 `object_ref` 取回冻结内容并重算 `content_sha256`；内容不一致时
停止标注，重新制作候选集。每份 review 记录标注者身份、独立性、标签、时间和这个 hash；
裁定者不得与两名标注者相同。不得把旧 InfoHub AI 字段或模型建议复制为人工标签。

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

### relevance-definition-v1

问题只有一个：**这条内容的主体是否属于 InfoHub 的科技研究范围（TMT）？** 只看冻结的标题与原文摘录，
不打开链接、不搜索、不凭记忆补充，也不参考任何 AI 字段（评分、翻译、摘要、分类）。

- `relevant`：主体是 AI（模型、产品、研究、算力）、机器人、半导体与芯片、智能硬件与消费电子、互联网平台、
  软件与云、智能汽车技术、电信与网络；或者是科技公司自身的动态与公司事件（财报、申报、回购、并购、评级、
  内部人交易、人事等，关注清单 23 家公司都属于科技公司）；或者是**直接针对**科技行业或具体科技公司的政策、
  监管、资本开支与融资环境（如芯片出口管制、针对某科技公司的反垄断审查、AI 资本开支）。
- `not_relevant`：主体在科技范围之外：大盘与指数行情、货币政策（如加息预期）、不以科技行业为焦点的地缘政治、
  医疗医药、体育娱乐、能源大宗农产品、非科技公司的一般财经新闻。科技公司只是顺带提及时仍为 `not_relevant`。
- `unknown`：冻结内容不足以判断，例如只有含义不明的标题、乱码或截断、公司名有歧义。它表示“信息不够”，
  不是“拿不准就选它”；能判断时必须给出 relevant / not_relevant。

这一定义与旧 LLM 策展提示词中的 `tmt` 判断范围一致（`app/ai/pipeline.py`），因此可以直接用人工标签评估旧
`tmt` 字段。产品规则对官方来源或关注公司条目的强制保留（`policy_override`）不是语义判断，标注时不考虑。
定义如需修改，必须发布新版本号；同一个 owner 数据集只能使用一个定义版本。

Impact 的固定字段、事件证据角色和困难切片以
[`docs/IMPACT_ANNOTATION_GUIDE.md`](../docs/IMPACT_ANNOTATION_GUIDE.md) 为准；通用指南不能替代专用契约。

数字、主体、否定、时态和证据引用属于关键字段。一个关键错误可阻断该能力发布。标注修改追加新数据集版本，
不能覆盖已用于报告的旧 gold。
