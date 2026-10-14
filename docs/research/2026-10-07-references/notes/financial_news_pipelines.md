# 金融/公司新闻管线：可供 infohub 借鉴的开源项目、数据集与论文

> 研究范围：实体链接（中英）、公告/文件接入（SEC EDGAR / 港交所披露易 / 巨潮资讯）、金融事件抽取与情感、开源金融新闻情报系统架构、实体级（target-dependent）情感与证据引文。
> 检索日期：2026-10-07。所有“事实”均附来源；“Inferences”是对 infohub 的借鉴建议，不是来源事实。凡是来源为第三方聚合站（Apify、emergentmind、CSDN 等）的，已在条目中标注可靠性较低。

---

## 1. 新闻中公司提及的实体链接（EN + ZH）：工具、数据集、品牌 vs 公司消歧

### Takeaway
没有一个现成的“中英文新闻 → 股票代码”开源链接器可以直接拿来用；成熟做法是“人工维护的小型身份表（别名/简称 + CIK/ticker/交易所）+ 外部标识符交叉核对（Wikidata、OpenFIGI、GLEIF）+ 对歧义名称做过滤/消歧（关键词过滤或 LLM 选择器）”。对 23 家公司的规模，规则 + 正负过滤词 + 少量 LLM 复核，比部署通用实体链接模型更合适。

### Cited Findings
**外部标识符来源（用于身份层交叉核对）**
- Wikidata 用 P414（stock exchange，交易所）记录上市地点，并要求以 P249（ticker symbol）作为限定词、最好再加 P580（开始时间）；P249 格式为 1–16 位大写字母数字，可含“.”；示例：Microsoft – Nasdaq – MSFT — [Wikidata P249](https://www.wikidata.org/wiki/Property:P249)；[WikiProject Companies/Properties](https://www.wikidata.org/wiki/Wikidata:WikiProject_Companies/Properties)
- Wikidata P5531 = SEC Central Index Key（CIK） — [Wikidata P5531](https://www.wikidata.org/wiki/Property:P5531)
- OpenFIGI API：`POST /v3/mapping`（第三方 ID → FIGI）、`/v3/search`、`/v3/filter`；支持 `TICKER`、`BASE_TICKER`、`ID_ISIN`、`ID_CUSIP`、`ID_SEDOL`、`ID_BB_GLOBAL` 等 25+ 种 idType，并可按 `exchCode`/`micCode` 过滤；文档中未出现 `ID_CIK`。限流：未认证 mapping 25 次/分钟（每请求 10 个 job），有 API key 时 25 次/6 秒（每请求 100 个 job）；search 未认证 5 次/分钟、有 key 20 次/分钟；超限返回 HTTP 429。官方称服务“free and open to the public” — [OpenFIGI API 文档](https://www.openfigi.com/api/documentation)
- GLEIF API：LEI 数据的全文/单字段搜索、名称与地址模糊匹配、BIC→LEI、ISIN 映射、母子公司关系查询；基于 GLEIF Golden Copy — [GLEIF API](https://www.gleif.org/en/lei-data/gleif-api)

**通用实体链接工具**
- ReFinED（Amazon Science）：一次前向完成提及检测、细粒度实体类型与消歧，链接到 Wikipedia/Wikidata（3000 万+实体），支持零样本，号称比同类模型快 60 倍 — [ReFinED GitHub](https://github.com/amazon-science/ReFinED)；[Amazon Science 博客](https://www.amazon.science/blog/improving-entity-linking-between-texts-and-knowledge-bases)
- mReFinED：ReFinED 的多语言版本 — [mReFinED GitHub](https://github.com/mrpeerat/mReFinED)
- OpenTapioca：面向 Wikidata、随编辑实时更新的实体链接系统 — [OpenTapioca GitHub](https://github.com/opentapioca/opentapioca)
- COLING 2025《Contextual Augmentation for Entity Linking using LLMs》：用 LLM 扩充提及上下文、零样本提示做消歧，在多数域外评测集上达到 SOTA — [ACL Anthology](https://aclanthology.org/2025.coling-main.570.pdf)
- 另有 2025/2026 预印本《Select, Don't Train: … Modular Entity Disambiguation with LLM-Based Selection》，主张“候选生成 + LLM 选择”的模块化消歧（仅见标题/摘要级信息） — [arXiv 2608.27470](https://arxiv.org/pdf/2608.27470)

**金融领域的“提及 → ticker”**
- BloombergGPT 论文把命名实体消歧（NED）定义为把文本中的公司提及链接到 ticker，并作为金融评测任务之一 — [BloombergGPT](https://arxiv.org/pdf/2303.17564)
- 《Extracting Structured Insights from Financial News》（2024）：用 LLM + 定制的字符串相似度校验框架，从原始新闻抽取 ticker、做公司级情感和摘要；在 5,530 篇新闻上，90% 文章与现有数据商相比“无遗漏 ticker”，22% 还找到了数据商遗漏的相关 ticker；评测集已公开 — [arXiv 2407.15788](https://arxiv.org/abs/2407.15788)
- SEntFiN 1.0 附带一个“1,000+ 金融实体、5,000+ 种媒体表述短语”的实体别名库 — [arXiv 2305.12257](https://arxiv.org/abs/2305.12257)
- FinEntity（EMNLP 2023）在金融新闻中标注实体 span（BILOU）及其情感，可兼作英文金融实体识别评测 — [ACL Anthology](https://aclanthology.org/2023.emnlp-main.956/)

**中文：简称/别名与“负面主体判定”**
- 中文金融 NER 研究指出公司常以简称出现，例如“腾讯”是“腾讯科技(深圳)有限公司”的简称；金融实体包括公司全称、简称、产品名、项目名等 — [ScienceDirect: Chinese NER for finance](https://www.sciencedirect.com/science/article/abs/pii/S0020025522015444)
- CCF BDCI 2019“金融信息负面及主体判定”：给定金融网络文本（标题+正文）和实体列表，判断是否负面，以及实体列表中哪些是负面主体；训练集 1 万条、测试集 1 万条 — [DataFountain BDCI 2019](https://www.datafountain.cn/special/BDCI2019/competition)；[Heywhale 数据集页](https://www.heywhale.com/mw/dataset/5e09a9eb2823a10036b126c0)；[Top1 方案分享（CSDN）](https://blog.csdn.net/roger_royer/article/details/104554685)
- BBT-CFLEB 中的 FinNSP 任务即“金融新闻负面消息及其主体识别”，同一基准还含 FinRE（新闻关系抽取）、FinFE（情感）等 6 个数据集 — [BBT-FinCUGE-Applications GitHub](https://github.com/ssymmetry/BBT-FinCUGE-Applications)；[BBT-Fin 论文](https://www.researchgate.net/publication/368664720_BBT-Fin_Comprehensive_Construction_of_Chinese_Financial_Domain_Pre-trained_Language_Model_Corpus_and_Benchmark)
- 中文金融嵌套实体数据集 CFNE 被称为该领域首个公开标注数据集（仅见搜索摘要） — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0020025522015444)

**品牌 vs 公司（“Apple”“小米”“理想”）消歧的经典做法**
- RepLab 2013（CLEF）“实体过滤”任务：判断推文是否真的指向某公司（related/unrelated）；语料为 2012 年 6–12 月用 61 个实体名检索的英/西推文，覆盖汽车、银行、大学、音乐四个领域 — [RepLab 2013 数据集](http://nlp.uned.es/replab2013/)；[RepLab 2013 Overview](https://www.researchgate.net/publication/258110327_Overview_of_RepLab_2013_Evaluating_Online_Reputation_Monitoring_Systems)
- UNED 团队发现“过滤关键词”（positive/negative filter keyword：出现即高概率相关/无关的表达）是公司名消歧中很有竞争力的方法 — [UNED at RepLab 2013（CEUR）](https://ceur-ws.org/Vol-1179/CLEF2013wn-RepLab-SpinaEt2013.pdf)
- 《Entity-specific text classification with LLMs》（CAFR，2025/2026）评估现成 LLM 在“对单个资产的情感”和“产品危机中的品牌牵连识别”两类实体特定分类任务上的表现 — [Emerald/CAFR](https://doi.org/10.1108/CAFR-12-2025-0269)

### Inferences
- infohub 只跟踪 23 家公司，**最可借鉴的是 SEntFiN 式“实体-别名短语库”+ RepLab 式“正/负过滤词”**：每家公司维护 `aliases_zh/aliases_en`、`positive_context`（如“理想汽车/Li Auto/李想/MEGA/L6”、“小米集团/雷军/1810”）、`negative_context`（如“理想状态/理想的/小米粥/小米椒”、“apple pie/苹果价格”）。这比现有纯正则别名多一层可审计的消歧信号。
- 歧义只在少数别名（Apple/苹果、小米、理想、Meta、Arm、Oracle 等）上出现，可采用“规则先判，歧义别名才调用 LLM 选择器”的模块化消歧（与 Select-Don't-Train、COLING 2025 思路一致），LLM 输出须给出触发判断的原文片段。
- 身份层可用 Wikidata（P414+P249、P5531）和 OpenFIGI（按 exchCode 映射 US/HK ticker）做**一次性离线核对**而非在线依赖：23 家公司的 CIK、港股代码、A 股代码（如中芯国际）人工确认后固化入库，外部 ID 只做 CI 校验。OpenFIGI 免费额度对此完全足够。
- 2407.15788 的“LLM 抽 ticker + 字符串相似度校验”可直接对应 infohub：LLM 建议的公司必须能在原文中找到别名命中，否则降级为待审。
- BDCI 2019 / FinNSP 的“给定实体列表、判定哪个是负面主体”任务形式与 infohub 未来的“实体级 tone”几乎一致，可作为中文回归测试集（需先确认许可）。

### Gaps
- ReFinED / mReFinED / OpenTapioca 的具体许可证、对中文公司简称的效果，未在本次检索中核实。
- 未找到“中文新闻提及 → 港股/A 股代码”的公开实体链接数据集；CFNE、BDCI 2019 的许可条款未核实（竞赛数据通常仅限研究用途，需要再查）。
- Wikidata 是否稳定记录港交所代码（P414=HKEX + P249）以及 LEI（属性号）未核实；GLEIF LEI 数据许可（是否 CC0）与 API 限流本次未查到明文。
- OpenFIGI 服务条款中对 FIGI 数据再分发的具体限制未读取。
- 没有找到专门针对“品牌 vs 公司”中文消歧（如“小米”“理想”）的数据集或论文。

---

## 2. 文件/公告接入最佳实践：SEC EDGAR、港交所披露易、巨潮资讯

### Takeaway
SEC 有官方、免认证的 JSON API（submissions 亚秒级更新），硬性规则只有两条：≤10 次/秒、必须声明含联系邮箱的 User-Agent；infohub 现在“每 10 分钟拉 submissions JSON”完全合规，可再加 getcurrent Atom 作为低延迟触发、daily-index 做对账。港交所和巨潮都**没有找到官方公开文档化的 API**：港交所只有网页 Title Search（有明确的时间窗口限制），巨潮常用的是社区逆向出的 `hisAnnouncement/query` 接口，均需谨慎限速并自建容错。

### Cited Findings
**SEC EDGAR 官方 API**
- 端点：`data.sec.gov/submissions/CIK##########.json`（申报历史与元数据）；`/api/xbrl/companyconcept/...`；`/api/xbrl/companyfacts/CIK##########.json`；`/api/xbrl/frames/...`；批量 `submissions.zip` 与 `companyfacts.zip` 每晚约 3:00 ET 生成 — [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- 时效：submissions API 更新延迟“less than a second”，XBRL API “under a minute”，高峰期可能延迟；无需认证/API key；data.sec.gov 不支持 CORS — [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- 公平访问：当前最大请求速率 10 次/秒；须在请求头声明 User-Agent，格式示例 `Sample Company Name AdminContact@<sample company domain>.com`；SEC 保留限速权利，不允许 botnet/自动化工具爬站，建议“只下载需要的内容”；`/Archives/edgar/daily-index` 提供当年每日索引文件 — [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- 全文检索（EFTS）：覆盖 2001 年以来所有电子申报，含附件（exhibits）；支持 AND/OR/NOT、NEAR()、后缀通配符、引号短语；可按表格类型与日期过滤；结果按相关度排序；FAQ 中未给出 JSON 端点与更新延迟 — [EDGAR Full-Text Search FAQ](https://www.sec.gov/edgar/search/efts-faq.html)
- 最新申报 Atom feed：`https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom`，可按表格类型过滤（如 8-K、4），近实时更新（来源为第三方页面，具体刷新间隔未证实） — [Apify: SEC EDGAR Current Filings](https://apify.com/darthdimus/sec-edgar-current-filings)

**SEC 开源库**
- edgartools（dgunning）：MIT；把 EDGAR 申报解析为带类型的 Python 对象；支持 XBRL 标准化财报、8-K 事件 items 解析、Form 4 内部人交易转 DataFrame、13F；可配置限流与缓存、企业/学术镜像；`set_identity()` 一次设置 User-Agent 邮箱；内置 MCP server 与 Claude Code/Desktop skills；1000+ 测试；抓取页显示约 2.8k stars — [edgartools GitHub](https://github.com/dgunning/edgartools)
- sec-edgar-downloader：MIT，最新 5.0.3（2025-02） — [libraries.io](https://libraries.io/pypi/sec-edgar-downloader)；[GitHub](https://github.com/jadchaar/sec-edgar-downloader)
- secedgar：Apache-2.0，基础爬虫式下载器，最新发布 2025-05（0.6.0） — [libraries.io](https://libraries.io/pypi/secedgar)

**港交所披露易（HKEXnews）**
- Title Search 支持按股票代码/名称、标题类别（Headline Category）或文件类型、新闻标题关键词、日期组合检索；2007-06-25 之后用 Headline Category（三级结构，第一级 16 类，如 “Announcements and Notices → Notifiable Transactions → Very Substantial Acquisition”），之前用 Document Type — [HKEXnews Title Search Guide (PDF)](https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/Listed-Company-Publications/Search-Guide/TitleSearchGuide_e.pdf)
- 时间窗口限制：不指定股票代码且不指定类别时，检索期不得超过 1 个月；指定类别但不指定股票时不得超过 12 个月；指定股票代码时默认 1999-04-01 至今；预测匹配最多显示 1,000 条 — [HKEXnews Title Search Guide (PDF)](https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/Listed-Company-Publications/Search-Guide/TitleSearchGuide_e.pdf)
- 第三方（Apify）HKEX 公告抓取器输出每条披露 11 个字段（公司名、披露日期、披露 ID、时间、文件类型、文件 URL、标题、标题类别等），并说明 HKEXnews 公开无需登录（第三方商业爬虫，仅作字段设计参考） — [Apify: HKEX News Announcements](https://apify.com/nexgendata/hkex-news-announcements)；[Apify: HKEX Disclosure Scraper](https://apify.com/crawlerbros/hkex-disclosure-announcements-scraper)

**巨潮资讯（cninfo）/ A 股公告**
- 社区普遍使用的历史公告查询端点为 `http://www.cninfo.com.cn/new/hisAnnouncement/query`（非官方文档，来自爬虫教程） — [CSDN 教程](https://blog.csdn.net/weixin_44566452/article/details/121039032)
- 巨潮另有 `webapi.cninfo.com.cn` 数据服务，需注册认证、企业用户可能需营业执照（来源为腾讯 ima 知识库聚合摘要，可靠性较低） — [ima 知识库](https://ima.qq.com/create/knowledge-base-seomedia?extraInfo=%7B%22launchChannel%22%3A%2210000179%22%2C%22opSlotId%22%3A%222165665%22%2C%22seoType%22%3A%221%22%7D&knowledgeBaseId=7416670364645874&launchChannel=10000179&scene=5)
- 开源实现：`rollysys/use_cninfo`（按需抓取 A 股公告 PDF → PyMuPDF 转 Markdown，集成 announcement_filter 类目分类，并提供 Claude Code skill 入口） — [use_cninfo GitHub](https://github.com/rollysys/use_cninfo)；Go 封装 `gocninfo` — [pkg.go.dev](https://pkg.go.dev/github.com/opensecx/gocninfo)；`CnInfoReports` 公告下载 — [GitHub](https://github.com/tr1s7an/CnInfoReports)
- FinNLP 把 SEC 与 Juchao/CNINFO 一并列为“公司公告”数据源 — [FinNLP GitHub](https://github.com/AI4Finance-Foundation/FinNLP)

### Inferences
- **SEC**：23 家公司 × 每 10 分钟 1 次 = 约 0.04 次/秒，远低于 10 次/秒上限；可借鉴 edgartools 的 `set_identity()` 设计把 UA 配置集中化。建议三层：① getcurrent Atom（按 8-K/4 过滤）做秒级到分钟级触发；② 现有 submissions JSON 作为权威记录；③ 每日 `daily-index` 对账补漏。EFTS 全文检索可用于“新闻中提到的事项是否已有正式申报”的交叉验证。
- 8-K 的 item 编号本身就是监管方给出的**免费事件类型标签**，可直接映射到 infohub 的事件类型（earnings、M&A、高管变动等），作为 LLM/规则事件分类的弱监督与校验。edgartools（MIT）可复用其 8-K item 解析逻辑，或仅参考其实现。
- **港交所**：HKEX 的三级 Headline Category 同样是官方事件标签（如须予公布交易、回购、盈警），建议按股票代码轮询（可回溯至 1999 年、无 1 个月窗口限制），并把 Headline Category 原样入库作为事件类型来源之一。Apify 的 11 字段 schema 可作为 infohub 港股公告表的字段清单参考。
- **巨潮**：在观察名单中主要与 A 股上市的中芯国际（A+H）相关；`hisAnnouncement/query` 属非官方接口，应低频、按代码查询并保存原始响应以防接口变动。use_cninfo 的“PDF → Markdown + 类目过滤”流水线可借鉴。

### Gaps
- SEC 官方文档中未找到超限后的封禁时长；getcurrent Atom 的确切刷新间隔、每页条数未经官方来源证实；submissions JSON 中是否带 8-K `items` 字段未在本次来源中确认（需实测）。
- 未找到港交所官方 RSS/JSON 推送或 API 文档，也未找到 HKEXnews 网站使用条款中关于自动化访问的规定。
- 巨潮官方 API 的注册流程、费用、限流、使用条款均未通过官方页面核实；`hisAnnouncement/query` 是否覆盖港股（H 股）未核实。

---

## 3. 金融事件抽取与情感：数据集、基准、2024–2026 表现较好的方法

### Takeaway
英文侧可用 EDT（新闻级公司事件检测）、FinBen/PIXIU（35+ 任务综合基准）、FinGPT（情感模型与基准）；中文侧事件抽取以 DuEE-Fin、ChFinAnn 为经典，2025 年的 DocFEE（CC BY 4.0、人机协同标注）最值得借鉴。多项研究一致表明：在窄任务（事件论元、实体级情感）上，**微调的小模型仍优于零样本 LLM**，但 LLM 用于“预标注 + 共识 + 人工复核”可把标注成本降到极低。带证据片段的 LLM 抽取已有成熟开源实现（Google LangExtract，Apache-2.0，字符级定位）。

### Cited Findings
**英文事件 / 新闻数据集**
- EDT（ACL 2021 Findings，Trade the Event）：9,721 篇带 token 级事件标签的新闻 + 303,893 篇带分钟级时间戳与股价标签的新闻；方法为“低层 token 事件检测 + 高层文章级事件检测”；仓库含 Reuters（2017 年起）新闻抓取工具和交互式事件检测/情感分析 — [TradeTheEvent GitHub](https://github.com/Zhihan1996/TradeTheEvent)；[ACL Anthology](https://aclanthology.org/2021.findings-acl.186/)
- Financial PhraseBank：4,840 句英文金融新闻句级情感，16 名具备金融背景的标注者；许可 **CC BY-NC-SA 3.0（非商业）** — [Hugging Face takala/financial_phrasebank](https://huggingface.co/datasets/takala/financial_phrasebank)
- FiNER-139：110 万句、139 种 XBRL 标签的数值实体识别，来源约 1 万份 2016–2020 年 10-K/10-Q；许可 CC BY-SA 4.0 — [Hugging Face nlpaueb/finer-139](https://huggingface.co/datasets/nlpaueb/finer-139)

**综合基准 / 模型**
- PIXIU / FinBen：35+ 评测数据集，覆盖情感（FPB、FiQA-SA、TSA、FOMC）、分类（Headlines、M&A、MultiFin 等）、知识抽取（NER、FiNER-ORD、FinRED、FinCausal）、QA、摘要（ECTSUM、EDTSUM）、风控与股价预测；FIT 指令数据 136K；FinMA 模型（LLaMA 系）；代码 MIT，数据集许可各异（CC BY-SA 3.0、MIT、CC BY 4.0 等）；有中文、西班牙语版本与 HF 排行榜 — [PIXIU GitHub](https://github.com/The-FinAI/PIXIU)
- FinGPT：MIT；五层架构（数据源 → 数据工程 → LLM（LoRA 微调）→ 任务 → 应用）；v3.3 情感模型加权 F1：FPB 0.882、FiQA-SA 0.903、TFNS 0.643、NWGI 0.643；FinGPT-Benchmark 另含关系抽取（27.6k）、标题分类（82.2k）、NER（511）；主要更新集中在 2023 年 — [FinGPT GitHub](https://github.com/AI4Finance-Foundation/FinGPT)

**中文事件抽取数据集**
- DuEE-Fin（百度，NLPCC 2022）：约 11,900 篇真实中文金融新闻/公告，15,000+ 事件、13 类事件、81,000+ 论元、92 种论元角色；允许一文多事件、一论元多角色 — [OpenReview](https://openreview.net/forum?id=t2lg704kK2)；[ResearchGate](https://www.researchgate.net/publication/363807209_DuEE-Fin_A_Large-Scale_Dataset_for_Document-Level_Event_Extraction)
- ChFinAnn（Doc2EDAG）：5 类事件（股权冻结、回购、减持、增持、质押）、35 种论元角色 — [ScienceDirect 2026 综述性引用](https://www.sciencedirect.com/science/article/pii/S0306457326003778)；[Doc2EDAG arXiv](https://arxiv.org/pdf/1904.07535)
- DocFEE（Scientific Data 2025）：19,044 篇东方财富 A 股公告（2020-01 至 2024-04），平均 2,277 字；9 类事件（破产清算、重大安全事故、股东减持、股权质押、股东增持、股权冻结、高管死亡、重大资产损失、重大对外赔付）、38 种论元；许可 CC BY 4.0，托管于 Figshare（10.6084/m9.figshare.28632464）与 OpenDataLab — [PMC 全文](https://pmc.ncbi.nlm.nih.gov/articles/PMC12065892/)
- DocFEE 的 HAC-Ann 人机协同标注：规则过滤（100% 召回）→ Qwen1.5-32B 预筛 → 多模型生成式标注 + 共识机制 → 人工迭代修正；标注准确率 84.04%，成本约为纯人工的 0.4%；基线：BERT 事件分类 F1 90.45%，Qwen1.5-7B 微调论元 F1 72.50%；微调模型明显优于 GPT-3.5-turbo 零样本 — [PMC 全文](https://pmc.ncbi.nlm.nih.gov/articles/PMC12065892/)
- 另有 CFinDEE（中文细粒度文档级金融事件抽取，WWW 2024 Companion）与《A Chinese Fine-grained Financial Event Extraction Dataset》（WWW 2023 Companion） — [ACM CFinDEE](https://dl.acm.org/doi/abs/10.1145/3589335.3651921)；[ACM 2023](https://dl.acm.org/doi/10.1145/3543873.3587578)

**中文金融 LLM 基准**
- CFinBench（NAACL 2025）：99,100 道题，43 个二级类别，单选/多选/判断；数据与评测代码开源于 cfinbench.github.io — [arXiv 2407.02301](https://arxiv.org/abs/2407.02301)；[ACL Anthology](https://aclanthology.org/2025.naacl-long.40/)
- FinEval：4,661 道中文金融/经济/会计/证书题（聚合站摘要） — [emergentmind](https://www.emergentmind.com/papers/2308.09975)
- CFBenchmark：评测中文金融助手能力 — [arXiv 2311.05812](https://arxiv.org/abs/2311.05812)
- BBT-CFLEB：6 个数据集 FinNA（新闻摘要）、FinQA（事件问答）、FinNL（新闻分类）、FinRE（关系抽取）、FinFE（情感）、FinNSP（负面消息及主体） — [BBT-FinCUGE-Applications](https://github.com/ssymmetry/BBT-FinCUGE-Applications)

**带证据片段的 LLM 抽取**
- Google LangExtract：Apache-2.0；用提示 + 少量示例定义抽取任务，每个抽取结果映射到原文精确字符区间（`char_interval`），可生成自包含 HTML 高亮可视化；长文通过分块、并行和多轮抽取提升召回；支持 Gemini、OpenAI、Ollama 本地模型（抓取页显示 v1.6.0、约 3.9 万 stars，数字以抓取时为准） — [LangExtract GitHub](https://github.com/google/langextract)
- 《Learning to Aggregate Zero-Shot LLM Agents for Corporate Disclosure Classification》（2026 预印本）研究聚合多个零样本 LLM 对公司披露进行分类（仅见标题） — [arXiv 2603.20965](https://arxiv.org/pdf/2603.20965)

### Inferences
- **最值得照搬的是 DocFEE 的 HAC-Ann 流程**：规则高召回 → LLM 预筛 → 多模型共识 → 人工复核。这与 infohub 已定的“算法大规模出标签、用户是唯一标注者”的分工完全吻合，只需把“共识不一致/低置信”样本送给用户。
- 事件类型体系可对齐：DuEE-Fin 13 类 / DocFEE 9 类 / ChFinAnn 5 类偏 A 股股权事件（增减持、质押、冻结），对 infohub 关注的科技巨头不一定高频；更贴合的是 8-K items 与 HKEX Headline Category，再补充新闻侧的产品发布、监管、诉讼等。
- 证据片段：采用 LangExtract 式“抽取结果必须带原文字符区间”，入库时校验 `text[start:end] == quote`，不匹配即拒收；这同时满足 infohub “版本化 + 人工审核”的要求。若不想引入依赖，也可只借鉴其输出 schema 与 HTML 审核视图。
- 许可风险：FPB 为非商业许可，只能做内部评测，不应混入可分发的训练产物；FiNER-139 为 CC BY-SA（相同方式共享）；DocFEE CC BY 4.0 最宽松。

### Gaps
- DuEE-Fin、EDT、ChFinAnn、CFinBench、BBT-CFLEB 的具体许可证未在本次检索中核实。
- 没有找到 2024–2026 年在 DuEE-Fin/ChFinAnn 上 GPT-4 级 / Qwen2.5+ / DeepSeek 等最新 LLM 与微调模型的系统对比数字（DocFEE 只对比了 GPT-3.5-turbo 与 Qwen1.5）。
- FinBen 中文版具体包含哪些中文数据集未展开核实。
- 未找到“带证据片段的金融事件抽取”专用评测基准（证据定位准确率等指标）。

---

## 4. 开源金融新闻情报系统：可复制的架构思路

### Takeaway
值得借鉴的不是交易模型，而是“数据源插件化 + 统一 schema + 分层处理”的骨架：FinGPT 的五层架构、FinNLP 的中美多源采集、newsnow 的“每个来源一个文件”的 source 目录 + MCP 暴露、edgartools 的类型化文件对象与 MCP server。OpenBB v5 的变化提醒：依赖第三方新闻数据商的聚合层很脆弱。

### Cited Findings
- FinGPT：数据源 → 数据工程（实时 NLP 处理、应对时间敏感性）→ LLM（LoRA）→ 任务评测 → 应用 的五层框架；MIT — [FinGPT GitHub](https://github.com/AI4Finance-Foundation/FinGPT)
- FinNLP（AI4Finance）：新闻源含 Yahoo Finance、Reuters、Seeking Alpha、新浪财经、东方财富、第一财经；社交源 Twitter、StockTwits、Reddit、微博；公告源 SEC、巨潮/CNINFO；支持“日期区间”与“流式”两种采集模式；MIT，但声明为学术用途“MIT education license”；约 1.5k stars — [FinNLP GitHub](https://github.com/AI4Finance-Foundation/FinNLP)
- newsnow（ourongxing）：MIT；聚合华尔街见闻、财联社、澎湃、微博、知乎等中文热榜/快讯；每个数据源在 `server/sources` 下独立实现，CONTRIBUTING 规定新增来源的方式；官方 newsnow-mcp-server 暴露 40+ 来源 — [newsnow GitHub](https://github.com/ourongxing/newsnow)；[newsnow-mcp-server](https://github.com/ourongxing/newsnow-mcp-server)
- TrendRadar 基于 newsnow 数据源做关键词频率分析、报告生成与飞书/企业微信等推送，社区维护了自定义资讯平台清单 — [TrendRadar Issue #95](https://github.com/sansan0/TrendRadar/issues/95)
- OpenBB v5.0.0：仓库许可从 AGPL-3.0-only 改为 Apache-2.0；移除 openbb-benzinga、openbb-fmp、openbb-intrinio、openbb-tiingo 扩展且未迁移功能；`obb.news.world` 不再可用，`obb.news.company` 只保留 tmx 与 nasdaq 两个 provider — [OpenBB v5.0.0 Release](https://github.com/OpenBB-finance/OpenBB/releases/tag/openbb-v5.0.0)；与旧文档（列出 benzinga、fmp、intrinio、polygon、tiingo 等 provider）存在冲突，应以 v5 发布说明为准 — [OpenBB CompanyNews 文档](https://docs.openbb.co/platform/data_models/CompanyNews)
- edgartools 提供面向 RAG 的“AI-native text output”和内置 MCP server — [edgartools GitHub](https://github.com/dgunning/edgartools)
- TradeTheEvent 仓库包含新闻抓取 + 在线事件检测/情感分析的交互式工具（架构参考，非交易用途） — [TradeTheEvent GitHub](https://github.com/Zhihan1996/TradeTheEvent)
- use_cninfo 把 A 股公告抓取包装成 Claude Code skill — [use_cninfo GitHub](https://github.com/rollysys/use_cninfo)

### Inferences
- **来源插件化**：仿 newsnow `server/sources/<source>.ts` 的约定，infohub 每个来源（SEC、HKEX、cninfo、RSS、中文快讯）一个模块、统一输出 schema（来源、原始 ID、发布时间、抓取时间、标题、URL、语言、原始载荷哈希），新增来源只需加一个文件和一条测试。
- **分层**：参考 FinGPT 五层，把 infohub 拆成 采集 → 规范化/去重 → 实体链接 → 事件/tone 抽取（版本化）→ 审核/展示；每层产物带版本号，方便 infohub 现有的“版本化 + 人工审核”机制。
- **MCP 暴露**：edgartools、newsnow 都提供 MCP server；infohub 若要让 Claude 协助审核，可以只读 MCP 暴露“待审事件 + 证据片段”。
- OpenBB v5 删 provider 的教训：新闻聚合层尽量直连一手源（交易所/监管/公司 IR/RSS），不要把核心链路押在单一数据商 SDK 上。
- 许可：FinNLP/FinGPT 虽为 MIT 但声明学术用途，抓取的第三方网站内容本身另有版权，借鉴架构即可，不要直接复用其抓取的数据。

### Gaps
- newsnow 中“财联社电报”等来源的抓取方式与稳定性、各中文源的条款未核实。
- FinNLP 最近一次提交时间、是否仍在维护未查到。
- OpenBB v5 发布的准确日期在抓取结果中不可靠（抓取摘要给出的日期与版本时间线不符），未采用。

---

## 5. 实体级（target-dependent）情感与证据引文：数据集与评测方式

### Takeaway
实体级金融情感的主流公开集是 FinEntity（EN，ODC-BY）、SEntFiN 1.0（EN 标题，多实体冲突情感）、SILC-EFSA（COLING 2025，EN+ZH 最大）以及中文 BDCI 2019 / FinNSP（负面主体判定）。评测普遍用“实体-情感对”的准确率/F1（FinEntity 用序列标注的 span+情感）；2024–2026 的结论是：微调小模型（BERT-CRF、DeBERTa、RoBERTa）与前沿 LLM 零样本接近或更好，推理型 LLM（DeepSeek-R1、o1）在较新的研究中表现最好。**没有找到评测“证据引文是否正确”的标准基准**，这部分需要 infohub 自定指标。

### Cited Findings
- FinEntity（EMNLP 2023）：979 句、2,131 个实体，标注实体 span 与正/中/负情感，按 BILOU 序列标注建模；许可 ODC-BY 1.0；HF：`yixuantt/FinEntity`；结论：微调预训练模型（BERT-CRF 最好）优于 ChatGPT-3.5；含加密市场监控案例 — [FinEntity GitHub](https://github.com/yixuantt/FinEntity)；[arXiv 2310.12406](https://arxiv.org/pdf/2310.12406)
- SEntFiN 1.0：10,753 条人工标注新闻标题，其中 2,847 条含多个实体且情感常相互冲突；RoBERTa 与 FinBERT 最高平均准确率 94.29%、F1 93.27%；Kaggle 公开 — [arXiv 2305.12257](https://arxiv.org/abs/2305.12257)；[Kaggle](https://www.kaggle.com/datasets/ankurzing/aspect-based-sentiment-analysis-for-financial-news)
- SILC-EFSA（COLING 2025）：构建“迄今最大的英文和中文金融实体级情感数据集”；两阶段方法：微调 LLM 生成伪标签 → 用基于 GNN 的示例检索器训练纠错模型（in-context learning correction）；新数据集上 SOTA；代码与数据在 GitHub — [arXiv 2412.19140](https://arxiv.org/abs/2412.19140)；[COLING 2025 PDF](https://aclanthology.org/2025.coling-main.333.pdf)
- 《On Assessing the Performance of LLMs for Target-Level Sentiment Analysis in Financial News Headlines》（Algorithms, 2025）：微调 DeBERTa-v3-base 表现好，ChatGPT-4/4o、Gemini 1.5 Pro 无需微调即可达到相近水平（来自搜索摘要，未读全文） — [DOI 10.3390/a18010046](https://doi.org/10.3390/a18010046)
- 《Benchmarking LLMs for target-based financial sentiment and stock return》（Springer, 2026）：DeepSeek-R1 与 ChatGPT（尤其 o1）在所有指标上优于词典法与 transformer 模型；数据为 2023-09 至 2024-01 的 Bloomberg 终端新闻（Alphabet、Amazon、Netflix、Nvidia）（来自搜索摘要） — [Springer](https://link.springer.com/article/10.1007/s41060-026-01201-x)；会议版 [CLiC-it 2025](https://aclanthology.org/2025.clicit-1.74.pdf)
- 中文：BDCI 2019 负面主体判定（给定实体列表判断负面主体，1 万训练/1 万测试） — [DataFountain](https://www.datafountain.cn/special/BDCI2019/competition)；FinNSP（BBT-CFLEB） — [BBT-FinCUGE-Applications](https://github.com/ssymmetry/BBT-FinCUGE-Applications)
- 带证据的抽取机制：LangExtract 为每个抽取结果给出原文字符区间并支持可视化核对 — [LangExtract GitHub](https://github.com/google/langextract)；2407.15788 用字符串相似度对 LLM 抽取结果做校验 — [arXiv 2407.15788](https://arxiv.org/abs/2407.15788)
- 另有通用领域 ABSA 新进展（推理型 LLM 大规模 ABSA、可解释多模态 ABSA，2026 预印本），可作方法参考 — [arXiv 2601.03940](https://arxiv.org/pdf/2601.03940)；[arXiv 2601.06848](https://arxiv.org/pdf/2601.06848)

### Inferences
- infohub 的 tone 模块建议采用“(article_id, entity_id, aspect, polarity, quote, char_start, char_end, model_version)”记录粒度；SEntFiN 的“多实体冲突情感”恰好对应科技股新闻常见情形（如“英伟达受益、AMD 承压”），应作为重点测试用例。
- 评测可分两层：① 极性：按实体-情感对算 macro-F1（与 FinEntity/SEntFiN 一致，可直接用其英文数据做回归测试，FinEntity 的 ODC-BY 许可较友好）；② 证据：自定指标——quote 是否为原文精确子串（硬约束）、quote 是否提到目标实体或其别名、人工判定 quote 是否“支持”该极性（抽样审核）。
- 中文侧可用 BDCI 2019/FinNSP 的“负面主体”格式做冷启动测试；正式数据由用户审核积累，并借鉴 SILC-EFSA 的“LLM 伪标签 + 纠错/检索示例”思路降低人工量。
- 模型选择：既有证据表明前沿/推理型 LLM 零样本已与微调模型相当或更好，infohub 早期可直接用 LLM + 强制引文，积累审核数据后再评估是否需要微调小模型做低成本批处理。

### Gaps
- SILC-EFSA 中英文数据集的具体规模与许可证未读到（摘要未给出）。
- Algorithms 2025 与 Springer 2026 两篇的具体数据集、指标数值未能读取全文（MDPI 返回 403），仅依据搜索摘要。
- 未找到任何以“证据引文正确率/支持度”为指标的金融实体情感基准；也未找到中文“实体 + 方面 + 引文”三元标注的公开数据集。
- BDCI 2019 数据的再使用许可未核实。
