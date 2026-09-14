# NLP 分析、证据与评估规范

状态：目标规范。当前系统仅有翻译/摘要/TMT/编辑重要性/类别，没有经过评估的情绪或宏观模型。本轮不调用付费模型，也不宣称达到了下述质量门槛。

## 1. 将“AI 策展”拆成可验证任务

| 任务 | 输入 | 输出 | 不允许 |
|---|---|---|---|
| language/translation | 固定文档版本、原题/正文范围 | 语言、译题、译文、原证据对齐 | 丢数字、改变否定或时态；润色冒充翻译 |
| relevance/classification | 原文与标签定义版本 | 多标签、相关程度、证据、未知 | 用拒判代替非相关；非 TMT 删除原始材料 |
| entity_linking | mention span、候选实体及版本 | resolved/unresolved/ambiguous、证据 | 直接把马斯克视为特斯拉经营事件 |
| summarization | 足够原始材料、证据 ID | 事实句与逐句引用、范围限制 | 只凭标题补财务数字和因果 |
| importance | 已知事实、关注主题/对象、策略版本 | 0..100 编辑分、理由、组成项 | 把编辑分称为准确概率/收益预测 |
| event_extraction | 固定文档版本与实体提及 | 主体/动作/对象/时间/事实/证据 | 凭标题相似直接确认事实 |
| event_linking | 新事实与候选事件版本 | same_event/related/distinct/uncertain | 将相关事件全部合并，或用情绪一致性归并 |
| tone | 原始文本中的陈述/观点 | speaker、target、aspect、polarity、quote | 把媒体立场当事件实际影响 |
| impact | 固定事件事实、证据、有限背景快照 | target、aspect、horizon、direction、机制、冲突、置信状态 | 宣称保证股价涨跌、从缺失数据猜数 |
| macro_mapping | 事件事实/实体及主题目录 | factor、region、direction/unknown、机制 | 从报道数推出实际 CPI/经济增速 |
| report | 固定输入 manifest、as-of、覆盖 | 带引用文本和版本 | 用生成时的最新事件补写过去日报而不标记 |

所有模型任务经过统一 runner；规则方法也是 versioned analysis，不能只有 LLM 才有审计。各任务可独立重试、比较模型和发布结果。首期允许一个 provider 执行多任务，但不把全部任务塞进一个不可解释的七字段请求。

## 2. 输入与输出协议

### 2.1 输入快照

每次调用保存：task_type、schema_version、dataset、主对象及版本、证据列表、相关实体/主题版本、prompt template/hash、渲染后的 messages（脱敏、受限保存）、工具/检索配置、模型参数、实际截断策略、输入 token 估计。

每条证据提供 `evidence_id,document_version_id,kind,quote,language,published_at,observed_at,content_quality`。模型只能引用输入中的证据 ID。若使用外部检索结果，先经正式采集通道持久化，再进入分析；不把模型内置知识说成项目证据。

首期原文不足时不默认联网补全文；用明确任务与来源策略获取。全文可用时按段落切块，每块有稳定边界与 hash，先抽事实再合成，记录遗漏范围。

### 2.2 模型调用与审计

每次 attempt 记录 provider endpoint 的服务标识（不含 key/query）、requested/resolved model、pipeline commit、prompt hash、schema hash、input hash、temperature、max_output_tokens、seed（若支持）、开始/结束、状态、token、费用估计、request ID、错误类型。

模型别名可能变化；即使参数相同也不承诺逐字确定性重现。规范保证“同一输入与输出可被审计、旧结果可重读”，不保证供应商以后重新调用得到完全相同答案。

输出先解析，再做 Pydantic/JSON schema 验证，再做 ID/证据/时间/数字/冲突规则验证。未知 ID、重复 item、越界分数、字符串布尔、未输入证据、非法 URL、单位冲突不能进入已发布投影。原模型输出与清洗后输出分别留存。

提供 OpenAI 兼容协议不等于每个 provider 支持 `thinking` 参数；provider adapter 声明 capabilities，禁用不支持的选项。禁止在 API handler 中调用模型。

### 2.3 推荐结果载荷

```json
{
  "schema_version":"impact/1.0",
  "subject":{"event_id":"event-demo","event_version_id":"ev-demo-003"},
  "status":"needs_review",
  "assessments":[
    {
      "target":{"entity_id":"entity-demo-company","type":"organization"},
      "aspect":"operating_cost",
      "horizon":{"bucket":"quarter","min_days":8,"max_days":90},
      "direction":"positive",
      "intensity":0.4,
      "evidence_ids":["evidence-demo-001"],
      "contradicting_evidence_ids":[],
      "mechanism":"若削减费用按公告计划实施，运营成本可能下降。",
      "assumptions":["计划能按期执行；其他成本不抵消节省"],
      "raw_confidence":0.72,
      "calibrated_confidence":null,
      "calibration_version":null,
      "uncertainty_reason":"仅有公司计划，尚无实际费用结果"
    }
  ]
}
```

合成示例只展示字段，不代表评估结果。raw_confidence 是模型自报值，未校准不能用作“72% 会发生”的陈述。投资/经营影响判断保留条件式机制，不输出执行交易指令。

## 3. Tone 与 impact 的定义

### 3.1 文本情绪 tone

粒度：一条观点/陈述 × speaker × target × aspect × 文档版本。

- polarity：positive/negative/neutral/mixed/unknown。
- speaker：作者、受访者、引用机构或 unknown；同文不同 speaker 可意见相反。
- aspect：如产品能力、经营前景、政策态度、估值观点；存在于版本化词表。
- intensity：0..1，表达强度，不是事件重要性。
- evidence：原文 quote + offset；否定、反讽、引用和转述必须在标注指南中定义。

“分析师说裁员是利好”只证明该分析师表达了这一观点，不能自动生成客观 impact=positive。

### 3.2 事件影响 impact

粒度：事件版本 × target（公司/行业/地区/宏观因子）× aspect × horizon。

- aspect 独立于 direction：revenue/cost/margin/capex/funding/supply/demand/regulatory_constraint/employment 等。
- horizon 首期 immediate=0..7 天、quarter=8..90 天、long_term=91..730 天、unspecified。若模型无法确定，选 unspecified，不自行填 90 天。示例及序列中使用同一边界定义。
- direction 是对所定义 aspect 的有利/不利/中性/混合/未知。成本下降对公司成本负担是有利，需要 mechanism 清楚写明。宏观变量“通胀上升”是数值方向 up，不能直接叫 positive。
- intensity 是推测影响幅度的序数强度，不当成收益幅度。未经标注集校准，前端优先展示弱/中/强及条件，而非小数精度。
- 每条判断含支持与冲突证据、主要假设、适用边界、评估状态。缺少证据返回 insufficient_evidence，不默认 neutral。

价格/资产收益方向不是首期输出必填项。若大系统后来要求 market_price aspect，应新增专门标注与回测协议，并把新闻语义标签与事后行情评估分开。

## 4. 宏观信息与结构化数值

第一批 factor：ai_capex、compute_supply、chip_export_policy、enterprise_ai_adoption、technology_employment、venture_funding、robotics_commercialization；可关联 rates/inflation/fiscal/trade 背景。factor 使用稳定 ID 与规则版本，地区与币种必须明确。

若原文有数值，抽取字段：metric_name、value、unit、currency、period_start/end、seasonal_adjustment、basis（yoy/qoq/level）、actual/forecast/prior/revised、release_time、source_evidence。未知 NULL，不能从标题猜精确数值。

“低于预期”只有实际值与来源所述预期在同口径/同时间可比时才能成立；市场共识数据未接入时标 `expectation_basis=source_claim` 或 unknown，不凭模型常识补预期。数值型宏观主库仍由大系统负责，InfoHub 输出带证据的抽取结果与引用。

## 5. 来源与证据质量

采用多维状态而非一个“真相分”：

| 维度 | 值/解释 |
|---|---|
| completeness | full/excerpt/title_only/generated_metadata/missing |
| attribution | identified/uncertain/unknown |
| origin independence | verified/distinct_claimed/shared_origin/unknown |
| claim support | directly_supported/inferred/disputed/unsupported |
| time certainty | parsed/approximate/unknown |
| review | unreviewed/accepted/rejected/corrected |

首期有效 impact 必须至少有一条直接相关的非生成原文证据、目标实体可解析、主体动作和时间范围明确。标题型记录可用于候选发现与分类，但不发布高强度因果影响。公告元数据只能支持“提交/发布某文件”事实，不能支持文件中未抓取的收入数字。

多个不同 publisher 并不自动增加结论可信度。同来源转载先归 origin group；独立性没有证据时保持 unknown。官方材料证明该主体的声明，可与其他材料冲突。

## 6. 重试、预算与降级

- 初期同一 worker 最多同时 1 个 LLM 请求；可配置但必须压测 provider 限制。网络抓取与 LLM 执行容量分开。
- 短暂网络/429/5xx：最多 3 次自动 attempt，退避候选 1、5、20 分钟并加抖动；Retry-After 优先。认证/权限/无额度立即 blocked，防止反复调用。
- 结构输出失败：最多 1 次带错误说明的修复请求，仍失败进 review/dead_letter；不把畸形内容写入 items。
- 内容拒判：记录 refusal；可按 provider 政策换低风险抽取任务或人工处理，不能绕过安全拒绝，也不能把它判为非 TMT。
- 最大请求 token、最大文档长度、每小时任务量、每日日/月费用上限为配置。总费用包含失败和重试，供应商不返回 usage 时标 estimated/unknown；不能当 0。
- 每日预算未由所有者明确填入前，新增 paid NLP 功能保持关闭。80% 提示，100% 停新 paid jobs，已有网页与原文读取继续服务；不自动切换到另一个收费模型。
- 免费/离线规则降级保持 labels/method=rule_fallback；日报 mode=structured_fallback；无模型不能改写已有评分阈值让所有内容悄悄变“精选”。
- 任务优先级：重要新原文、人工指定复核、必要重分析、一般新条目、历史回填；保留最低历史配额，同时避免失败老任务无限占队列。

## 7. 输入安全与结果发布

新闻、网页与 PDF 是不可信输入。prompt 明确将正文视为资料；模型没有执行 shell、发送消息或修改数据库的工具权限。出现“忽略前面指令”“发送密钥”等内容只作为可分析文本，不执行。

验证层检查：JSON schema、输入 ID 白名单、引文片段一致、数字/单位/否定、时间边界、外链域名和状态。HTML/Markdown 渲染继续清洗，模型新生成引用只能指向已有来源。

人工修正优先于自动投影，同一对象重跑不能覆盖有效人工决定；遇新事实可提出复核。需要检查动作 actor/reason/evidence/expected_version，详细审计不在公众页面暴露个人信息。

## 8. 评估数据集设计

### 8.1 版本 1 样本计划（未创建，未标注）

- 600 份去重文档，中文/英文各至少 200；第三语言或繁体材料单列切片。
- 从中建立至少 150 个事件组，含至少 40 组同公司不同事件、不同财报期/版本/金额的困难负例；确保转载同源、中英跨文和否认/修正样例。
- 至少 300 条 target/aspect/horizon 标注，其中 ≥100 条 unknown/mixed/证据不足或冲突；不能只抽清楚的利好/利空样例。
- 单独 50 条输入安全/提示注入/畸形响应/极长文本案例，不混入自然分布的 accuracy 分母。
- 对 30 个真实日报样本（日期累计达到后）进行引用与覆盖审计，未收集到时只报告已有 n，不虚称完整。

抽样按 source、language、kind、topic、时间、quality 分层；困难样本与自然样本分别报分。不从精选页直接抽全部测试集，否则模型只在自己挑选的数据上评估。

### 8.2 分割与标注

按事件组和原始出处分组后 60/20/20 训练/开发/盲测；同事件、转载、翻译或同公告的不同版本不可跨分割。最后一段时间留作时序盲测；至少一个来源保留作跨来源检查。

数据集 manifest：dataset_version、来源允许用途、原文 hash、抽样 SQL/时间、stratum、document/event group、split、annotation_schema、annotators、adjudication、label timestamps。受限制正文只保本地对象引用，不推公开仓库。

两名独立标注者评审关键事件/impact 真值；意见分歧经裁定。若当前只能由一名所有者标注，标记 single_annotator，影响分析仅为 experimental，不能假装完成双人验收。AI 可提出预标注，不能自己给自己出正式真值。

impact 的标注真值是“在给定证据下，判断是否有依据且符合定义”，不是以后是否涨价。加入 unknown 作为合格答案，避免逼模型猜方向。

### 8.3 指标与首期发布门槛

以下为工程验收目标，允许先基线测量后在 SPEC 中审议调整；不得通过删困难样本凑合格。

| 能力 | 指标 | 候选门槛 |
|---|---|---|
| 输出结构 | JSON/schema 合格率（所有 attempts 和首次 attempts 分别报） | 最终发布结果 100%；首次 ≥99% |
| 引用有效性 | 引用 ID 存在且 quote/span 精确匹配 | 发布结果 100% |
| 摘要事实 | 人工判定无依据句子 / 可验证事实句 | ≤2%，关键数字/主体/否定错误为发布阻断 |
| 相关性 | 宏观/科技保留 recall；宏平均 F1 | 保留 recall ≥0.95；macro F1 ≥0.85，逐类同时报告 |
| 实体关联 | 已解析 entity 的 precision / recall | precision ≥0.97、recall ≥0.85；歧义 abstain 比率单列 |
| 候选检索 | gold 同事件至少一个候选的 recall@50 | ≥0.95 |
| 事件归并 | pairwise precision/recall、B-cubed F1、过并/漏并率 | precision ≥0.98、recall ≥0.80；关键不同期/版本负例 0 误并 |
| tone | macro F1、类别混淆矩阵 | ≥0.80，各语种/来源/引用类型报告 |
| impact | 方向 macro F1 + 对象/方面/期限准确 + evidence support | F1 ≥0.75；已发布结论 evidence support ≥0.95；关键安全切片 0 无证据强判断 |
| 未知判断 | insufficient-evidence recall、拒答与覆盖率 | recall ≥0.90；同时报 coverage，避免全拒答得高分 |
| 人工一致性 | Cohen kappa 或 Krippendorff alpha | 关键标签 ≥0.70，否则先修标注定义 |
| 置信校准 | Brier score、ECE、reliability plot | 至少 200 条可裁定 held-out；ECE ≤0.10 后才能展示 calibrated confidence |
| 成本/耗时 | 每 1000 输入/每 100 有效事件 token/费用、p50/p95 | 不超过配置预算，且不明显劣于同等质量基线 |

所有比例给分子/分母、95% 区间和切片支持数；聚类指标按事件组 bootstrap，不能把成千 pair 当独立样本虚增精度。小样本切片 <30 时标低支持；即使点估计过门槛也只进入受监控试运行，不宣称统计保证。

基线至少：现有 titles-v3、规则分类、不调用 LLM 的结构化摘要；候选模型在同样 input/token 预算下比较。失败、缺失和拒判进入分母，不只统计成功调用。

### 8.4 变更门禁

- prompt/model/provider/normalizer/matcher/schema/taxonomy 任一变化触发受影响评估。
- PR CI 执行 deterministic fixture 和 mock；付费评估独立运行，固定预算与版本，不在每次 push 自动收费。
- 关键主体/数值/否定、跨版本误合并、历史信息泄漏出现一次即阻断发布；一般指标相对基线退化 >2 个百分点需要说明并审批新基线。
- 候选以 shadow 模式处理固定样本，记录差异；通过后分阶段切当前 publication，历史结果不覆盖。回滚切回旧发布指针/模型配置。
- 首周每天抽查 30 条或当日全部（取小），按低证据/新来源/模型分歧加权；稳定后每周 50 条。严重错误先暂停影响结果发布，再保留采集和阅读。
- 监测 NULL、拒判、类别分布、摘要长度、来源组合、费用、延迟变化；分布变化是调查线索，不能自动判定新闻不真实。

## 9. 情绪/宏观聚合 v1 候选口径

第一版称“新闻叙事倾向/事件影响判断分布”，不称实际市场情绪总指数。

聚合键：target_id × aspect × horizon × UTC 窗口 × as_of × formula_version × analysis_version_group。

贡献单位：在 `[window_start,window_end)` 内发生可见事实变化的稳定事件，每个 event_id 对此键最多一次；选 `available_at<=as_of` 且与选定事件版本匹配的最新合格分析。纯转载不改变 last_fact_change_at；新事实更新同一事件时替换该窗口贡献并产生新 signal version。

候选公式：

- 合格 positive：x=+intensity；negative：x=-intensity；neutral：x=0。
- mixed、unknown、insufficient_evidence 单独计数，首期不放入均值，不能当 0。
- `score = 100 × sum(x) / n_valid`，每个事件等权；不按报道数/模型自报置信度/热度加权。
- `n_valid=0` 或 `n_valid<10`：score=NULL，status=insufficient_sample，可显示分布与原事件。
- 同时返回 n_events（窗口内去重候选事件总数）、n_eligible（符合目标/方面/期限与权限的事件数）、n_valid/n_unknown/n_mixed；valid_fraction=n_valid/n_eligible（分母0为NULL），coverage对象记录覆盖范围、来源分布、缺失源时长、期内数据缺口。未完成/被拒判/证据不足统一纳入n_unknown，使n_eligible=n_valid+n_unknown+n_mixed；n_valid含neutral。缺上游数据不能把n_eligible=0解读为中性。
- 单一 publisher/origin 超过 50% 或关键源存在缺口时 status=partial；阈值是初始显式规则，须通过源结构评估再调整。
- 只比较同 formula/model group、相近来源覆盖的序列；更换模型/主题分类产生新版本序列与必要桥接评估，不能直接拼成连续趋势。

这种指标仍有新闻选择、媒体频次、来源语言和事件归并偏差；输出这些质量元数据供大系统选择使用。未获准的历史缺失材料不用于构造看似完整的长期序列。

## 10. Definition of Done

一项 NLP 能力只有同时具备：明确任务 schema、版本化输入、不可变结果、完整 attempt 审计、证据验证、未知/拒判语义、人工评估报告、预算与降级、回滚方法，才算可集成。一个页面上出现“利好/利空”字样不满足完成标准。
