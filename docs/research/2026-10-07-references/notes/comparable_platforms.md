# 可比新闻情报/聚合平台调研：产品与架构可借鉴点（面向 infohub）

> 调研日期：2026-10-07。范围：开源聚合器与 AI 日报、大规模新闻事件库、热度排序算法、LLM 日报的溯源与幻觉控制、覆盖率/召回测量。
> 写作约定：「Cited Findings」只放有来源的事实；「Inferences」是基于事实对 infohub 的推断与建议（明确标注为推断）；「Gaps」列出未能证实的点。
> 标注「据搜索摘要」的条目来自搜索结果摘要而非直接抓取原文，可信度低一级。
> infohub 背景（来自任务说明）：中文门户（AI、机器人、美股/港股科技频道），热点事件列表、持久事件时间线、专题页、日报、全文搜索、信源健康页；版本化数据底座（原始证据、文档/事件/分析版本、持久任务、发布台账）；带认证的只读 API（快照 + 增量变更同步）；LLM 做相关性、重要性、翻译/摘要；单人审核。信息组织参照 AIHOT。

## Q1. 开源聚合器与 AI 新闻日报：它们做什么、怎么做、成熟度与许可证

### Takeaway
开源侧最成熟的是「抓取/转 RSS/热榜聚合」层（newsnow MIT、RSSHub AGPL-3.0、TrendRadar GPL-3.0、Huginn MIT），但它们基本不做事件聚类与溯源摘要；做「故事级聚合 + LLM 摘要」的产品（Kagi News、Particle、Ground News、smol.ai AINews）大多只公开产品形态和信源清单，不公开管线细节。对 infohub 最有价值的是：Kagi 的「每日有限版面 + 固定故事结构 + 社区可审计信源清单」、AINews 的「多管线候选 + 薄人工编辑层」、Ground News 的「信源级元数据（倾向/事实性/所有权）+ 覆盖分布」，以及 newsnow/TrendRadar 作为中文热榜的现成数据面。

### Cited Findings

**newsnow（ourongxing/newsnow）**
- 定位为实时热门新闻的优雅阅读界面；MIT 许可；约 22k stars、6k forks — [GitHub](https://github.com/ourongxing/newsnow)
- 后端 Nitro，Node ≥ 20；通过 db0 支持多种数据库，推荐 Cloudflare D1；可部署到 Cloudflare Pages / Vercel / Docker — [GitHub](https://github.com/ourongxing/newsnow)
- 缓存默认 30 分钟，用户可手动刷新；抓取间隔按信源更新频率自适应，最短 2 分钟 — [GitHub](https://github.com/ourongxing/newsnow)
- 信源适配器位于 `shared/sources` 与 `server/sources`，有完整类型定义；支持 MCP server、GitHub OAuth 同步、PWA — [GitHub](https://github.com/ourongxing/newsnow)
- README 未说明排序算法（各平台热榜按原平台顺序展示） — [GitHub](https://github.com/ourongxing/newsnow)

**TrendRadar（sansan0/TrendRadar）——基于 newsnow API 的中文热点监控**
- 明确使用 newsnow 的 API 获取多平台数据，覆盖微博、抖音、B 站、知乎等 11 个主流中文平台 — [GitHub](https://github.com/sansan0/TrendRadar)
- 热度计算由「排名权重、频次权重、热度权重」三部分组成（README 未给出具体公式） — [GitHub](https://github.com/sansan0/TrendRadar)
- 关键词语法：`[分组名]` 分组、`#` 注释、`+` 必须出现、`!` 排除、`/正则/` 正则；v6.5.0 起可在 `ai_interests.txt` 用自然语言描述兴趣，由 AI 分类而非仅关键词 — [GitHub](https://github.com/sansan0/TrendRadar)
- 推送渠道 9+（企业微信/个人微信、飞书、钉钉、Telegram、邮件、ntfy、Bark、Slack、Webhook）；存储为本地 SQLite 或 S3 兼容远端；部署 GitHub Actions / Docker / uv；提供 MCP — [GitHub](https://github.com/sansan0/TrendRadar)
- 许可 GPL-3.0；62.7k stars、24.9k forks；最新 v6.10.0（2026-06-19） — [GitHub](https://github.com/sansan0/TrendRadar)

**RSSHub（DIYgod/RSSHub）**
- 把大量网站转成 RSS，自称「全球最大 RSS 网络」，超过 5,000 个全球实例；配套 RSSHub Radar 浏览器扩展用于发现可订阅源 — [GitHub](https://github.com/DIYgod/RSSHub)
- 许可 AGPL-3.0；46.4k stars、10.3k forks；17,879 次提交，仍在活跃开发 — [GitHub](https://github.com/DIYgod/RSSHub)

**Huginn**
- 「在自己服务器上的可 hack 的 IFTTT/Zapier」：Agent 产生/消费 Event，沿有向图传播；含 RSS Agent、Website Agent、Trigger Agent（可对高频事件计数并在激增时告警）、定时摘要邮件 Agent、去重能力 — [GitHub](https://github.com/huginn/huginn)
- Rails + delayed_job；MIT；50k+ stars — [GitHub](https://github.com/huginn/huginn)

**Kagi News（Kite）**
- 每天读取「数千个社区维护的 RSS 源」，用 AI 生成一份每日简报并清楚标注来源；每天约 UTC 中午只发布一次，理由是给新闻消费一个「自然终点」，把无尽习惯变成有限仪式 — [Kagi Blog](https://blog.kagi.com/kagi-news)
- 故事先用原始来源语言生成，再用 Kagi Translate 翻译；不爬网页，只用出版商主动提供的 RSS；不做个性化以避免信息茧房 — [Kagi Blog](https://blog.kagi.com/kagi-news)
- 每个故事含：所有来源列表（带链接和文内引用）、要点、关键各方观点、历史背景、事件时间线（据搜索摘要） — [NiemanLab](https://www.niemanlab.org/2025/10/kagi-news-is-an-ai-powered-app-for-keeping-up-with-the-world/)
- 公开仓库 kite-public：`kite_feeds.json` 按类别组织 RSS（含 `source_language`），一个类别至少要有 25 个 feed 才会上线；`media_data.json` 记录媒体机构信息；`contentFilters.json` 内容过滤；代码 MIT、数据 CC BY-NC 4.0 — [GitHub kite-public](https://github.com/kagisearch/kite-public)
- 仓库与博客均未公开聚类、排序、摘要管线细节；HN 讨论中 Kagi 员工也未给出技术细节，仅表示已在博文中更清楚地说明使用了 AI — [GitHub kite-public](https://github.com/kagisearch/kite-public); [Hacker News](https://news.ycombinator.com/item?id=45426490)

**smol.ai AINews**
- 工作日日报，汇总头部 AI Discord、Reddit、X — [news.smol.ai](https://news.smol.ai/)
- 2024-03 Karpathy 称其索引约 356 个 Twitter 账号、约 21 个 Discord，且「很长、适合略读」 — [X/Karpathy](https://x.com/karpathy/status/1767616494752731633)
- swyx 访谈：每天摄入 20–30 万词；在有人往 Discord 丢链接等事件触发时执行「多阶段摘要」；每天处理数十万 token，强调可调试、可复现；同时跑 4 条管线，由他作为编辑挑最好的一份；保留「一层薄的人工策展」 — [Heavybit](https://www.heavybit.com/library/article/ai-hidden-opportunities-for-software-developers-swyx)
- 平静日子会直接用「not much happened today」作标题 — [AINews issue](https://news.smol.ai/issues/26-01-09-not-much/)
- 网站代码在 smol-ai/ainews-web-2025（存档内容策略），生成管线本身未开源 — [GitHub](https://github.com/smol-ai/ainews-web-2025)

**小型开源 AI 日报项目（成熟度低，作为模式参考）**
- yeshuai508/ai-daily-news：RSS + X 输入；三层去重 URL → 标题相似 → 摘要相似，「同一故事 5 家媒体报道变成 1 条交叉验证条目」；评分 `log2(engagement) × account_weight × time_decay(half_life=24h)`，高权重账号 3×；中/英 prompt 分文件；MIT；0 star（玩具级） — [GitHub](https://github.com/yeshuai508/ai-daily-news)
- isimpsondu/ai-news-brief：据仓库描述，94 个源（RSS、HN、Reddit、X、YouTube 字幕、GitHub Trending），LLM 过滤+打分，SimHash + pgvector 去重，中英双语摘要（据搜索摘要；2026-10-07 直接访问返回 404，可能已删除或改名） — [GitHub（搜索结果）](https://github.com/isimpsondu/ai-news-brief)

**Particle / Ground News（多视角覆盖，仅有公开文档的部分）**
- Particle 以「故事」而非单篇文章为单位，读取多来源后摘要；功能有 Opposite Sides（左右倾向媒体如何报道同一事）、话题关注、AI 问答；与 AFP、Fortune、TIME（经 TollBit）等有合作但细节未披露；创始人 Sara Beykpour（前 Twitter） — [WAN-IFRA](https://wan-ifra.org/2024/11/particle-a-new-player-in-the-ai-news-game/)
- Particle 用左右光谱条展示报道分布，气泡数量/大小表示一侧是否不成比例地报道 — [MakeTechEasier](https://maketecheasier.com/how-particle-app-helps-me-read-news-critically/)
- Ground News 倾向评分综合 AllSides、Ad Fontes Media、Media Bias Fact Check 三家；事实性评分为 Ad Fontes 与 MBFC 的平均，均为「出版物级」而非单篇；手工编码 2,276+ 家媒体的 8 类所有权；Blindspot 定义为「政治色彩故事被某一侧媒体不成比例报道」，且 Low 事实性报道占比不得超过 35% — [Ground News](https://ground.news/rating-system)

### Inferences
- **许可边界（对 infohub 最直接的约束）**：newsnow、Huginn、kite-public 代码为 MIT，可直接借鉴代码；TrendRadar（GPL-3.0）与 RSSHub（AGPL-3.0）有传染性，建议只作为独立进程/旁路服务、通过 HTTP/RSS 消费其输出，不把代码并入 infohub 仓库；kite-public 的 feed 数据是 CC BY-NC 4.0，若 infohub 将来涉及商业用途（美股研究下游）需谨慎直接复用其信源清单。
- **中文热榜补源**：newsnow 已封装大量中文平台热榜（TrendRadar 就是建立在其 API 之上），infohub 可以把 newsnow（或自托管 RSSHub）作为「外部热度信号源」而非正文来源——用于热度加权和漏报检测（见 Q5），正文仍回原始出处取证。
- **Kagi 式「有限版面」**：infohub 日报可明确设定固定发布时刻与故事数上限，并采用固定故事结构（要点 / 各方观点 / 背景 / 时间线 / 来源列表+文内引用），与 infohub 已有的「事件时间线」「专题页」天然对应。
- **信源清单可审计**：仿照 kite-public 的 `kite_feeds.json` + `media_data.json`，把 infohub 信源注册表做成带版本的声明式文件（类别、语言、媒体元数据、最低类别覆盖数），信源健康页直接读取它；「每类别至少 N 个源才上线」可作为频道上线门槛（如美股/港股频道至少 N 个独立源）。
- **AINews 式多候选 + 人工挑选**：对单人审核的 infohub，可让日报同时生成 2–4 个候选（不同 prompt/模型/排序），由站长挑选并把选择写入发布台账，选择记录本身就是后续评估的偏好标注数据。
- **允许「今天没什么事」**：AINews 的做法提示日报不必凑数；infohub 可在重要性阈值不足时发布短版日报，避免低价值内容稀释。
- **信源级元数据字段**：借鉴 Ground News，为每个信源加入「所有权类型 / 一手 vs 转载 / 事实性等级 / 地区语言」字段，用于后续独立性计数和覆盖分布展示（见 Q3）。

### Gaps
- Kagi News 的聚类方法、所用模型、故事选择/排序规则、幻觉检查方式未公开（博客、仓库、HN 讨论均无）。
- Particle 的聚类与准确性保证机制在可找到的文档中没有说明。
- smol.ai AINews 的管线代码与 prompt 不公开，只有访谈层面的描述；「99% 由可定制研究代理生成」一说仅见搜索摘要，未找到原文。
- TrendRadar 三项权重的具体公式未在 README 摘要中出现；newsnow 前端框架等细节未核实。
- isimpsondu/ai-news-brief 无法访问，其 SimHash/pgvector 阈值等细节无法核实。

## Q2. 大规模新闻事件库（GDELT、Event Registry、Media Cloud、CC-NEWS）：可迁移到单机系统的架构思路

### Takeaway
这些系统可迁移的不是规模，而是几个具体机制：Media Cloud 的「渐进退避抓取 + URL/GUID/标题三重匹配 + 同源同日句级去重」、Event Registry 的「在线增量聚类 + 周期性合并/拆分 + 时间窗淘汰 + 中位文章（medoid）定标题 + 稳健事件日期」、GDELT 的「Mentions 表：记录事件被每篇文章提及的时间序列」与「按总抓取量归一化的覆盖量」。这些都能在单机 Postgres/SQLite + 后台任务中实现，并与 infohub 的版本化事件模型相契合。

### Cited Findings

**GDELT 2.0 / DOC 2.0 API**
- 每 15 分钟更新 Events、Mentions、GKG 三个流 — [GDELT Blog](https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/)
- 2.0 新增 Mentions 表：不只记录事件首次被提及，而是记录事件随时间的每一次提及及文章发布时间，用于追踪事件在全球媒体中的传播 — [GDELT Blog](https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/)
- GDELT Translingual 实时翻译 65 种语言、占其非英语监测量的 98.4%；GKG 度量 2,300+ 种情绪与主题 — [GDELT Blog](https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/)
- DOC 2.0 API 模式包括 ArtList、TimelineVol（按百分比）、TimelineVolRaw（原始计数）、TimelineVolInfo（每时间片 top 10 相关文章）、TimelineTone、TimelineSourceCountry、TimelineLang、ToneChart 等；检索滚动最近 3 个月 — [GDELT Blog](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- TimelineVol 的归一化：匹配文章数除以该时间步 GDELT 监测的全部文章数，以占全球覆盖的百分比报告；支持 `domain:`、`domainis:`、`sourcecountry:`、`sourcelang:`、`theme:` 过滤 — [GDELT Blog](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)

**Event Registry（Leban et al., WWW 2014 Companion）**
- 采集基于 IJS News Feed，约 75,000 个新闻源，每天 10–20 万篇；英语 50%、德语 10%、西语 8%、中文 5% — [WWW'14 论文](https://archives.iw3c2.org/www2014/proceedings/companion/p107.pdf)
- 每种语言一个在线聚类实例；文章用标题、正文和命名实体构成向量空间表示，实体权重远高于普通词；新文章放入最近簇；每加入 n 篇后重新评估是否合并或拆分（拆分用 BIC，合并用余弦相似度与 Lughofer 椭球准则） — [WWW'14 论文](https://archives.iw3c2.org/www2014/proceedings/companion/p107.pdf)
- 因同一事件通常只被报道几天，含超过 k 天旧文章的簇会从聚类器中删除（但不从事件库删除） — [WWW'14 论文](https://archives.iw3c2.org/www2014/proceedings/companion/p107.pdf)
- 跨语言合并用 SVM，训练数据是专家标注的「簇对是否同一事件」；特征包括跨语言簇相似度（C1 中每篇文章的最相似文章有多少落在 C2，反之亦然）、高频实体相似度（实体用语言无关 ID）、簇间时间差、簇内时间变异、簇质量 — [WWW'14 论文](https://archives.iw3c2.org/www2014/proceedings/companion/p107.pdf)
- 事件标题与摘要取最接近簇中心的文章（medoid）的标题与首段；事件日期取文中最常被提到的日期（若频次过阈值），否则取簇内文章平均日期——刻意不用最早日期，以免被错误归入的旧文章带偏；地点取 GeoNames 可识别的高频地点实体，文章开头出现的地点加权更高 — [WWW'14 论文](https://archives.iw3c2.org/www2014/proceedings/companion/p107.pdf)

**Media Cloud（ICWSM 2021；2.0 版 ICWSM 2026）**
- 2008 年起累计 17 亿+ 篇；RSS 爬虫每天约 100 万篇、约 6 万个媒体源；另有专题引擎递归爬取超链接，可建千万级专题数据集 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- Feed 发现：从媒体首页爬两层，找含 RSS/XML 等模式的链接，下载并尝试解析以验证（开源 feed_seeker）；另有人工「collections curator」定期复核并补漏 feed — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- 渐进退避：feed 出现新文章后 5 分钟内再抓；无新文章则 10 分钟、20 分钟……逐步退避直到每周一次；一旦发现新文章重置为 5 分钟 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- 去重：按 URL/GUID（有损 URL 归一化）或标题匹配已存在故事；标题归一化取以 `:`、`|`、`-` 等分隔后最长的一段；正文用 python-readability 抽取后切句，删除「同一媒体源 + 同一自然日」内重复的句子；用 cld2 判语言 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- 只下载公开可得内容；付费墙站点只索引可见部分（如 WSJ 只有首段） — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- 专题引擎（Topic Mapper）：种子查询 + 媒体集合 + 日期范围 → 检索 → 从结果文章的超链接向外爬，只有匹配种子查询的才加入；重复 15 轮；用超链接生成故事/媒体的入链数作为影响力指标；也可从社交平台搜索结果导入种子 URL — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- 国家媒体集合最初来自 ABYZ News Links（2018），之后逐个核验并请各国领域专家补充 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- Media Cloud 2.0：重新设计采集、存储、检索系统与前端，超过 18 亿篇，以「信源 Directory」和「Story Index」两个工具为核心 — [ICWSM 2026](https://ojs.aaai.org/index.php/ICWSM/article/view/42778)
- 新档案系统 2024-01 成为默认，并公开说明已知 bug 与回填计划（2023 年数据回填目标日期、之后每年 1–2 个月） — [Media Cloud Blog](https://www.mediacloud.org/blog/media-cloud-system-updates)

**Common Crawl CC-NEWS**
- 2016 年开始的新闻子数据集，与月度主爬取解耦以便时效发布；WARC 文件全月持续上传，命名 `CC-NEWS-yyyymmddHHMMSS-nnnnn.warc.gz`，按月提供 `warc.paths.gz` 清单 — [Common Crawl Blog](https://commoncrawl.org/blog/news-dataset-available); [CC-NEWS 索引](https://data.commoncrawl.org/crawl-data/CC-NEWS/index.html)
- 爬虫基于 StormCrawler，产出 WARC — [GitHub news-crawl](https://github.com/commoncrawl/news-crawl)

**LLM 增强的事件聚类（研究原型）**
- 在 GDELT 数据上，「预处理关键词抽取+嵌入 → 聚类 → LLM 事件摘要与主题标注」三段式框架，并提出 Cluster Stability Assessment Index（CSAI）评估聚类稳健性；作者报告 LLM 嵌入显著提升 CSAI（单作者预印本，2024） — [arXiv 2406.10552](https://arxiv.org/abs/2406.10552)

### Inferences
- **抓取调度**：infohub 可把 Media Cloud 的退避策略直接做成每源调度状态（`next_poll_at`、`backoff_level`、`last_new_item_at`），由持久任务（durable jobs）驱动；信源健康页显示「当前退避级别 vs 该源历史常态」，长时间处于最高退避的活跃源即为疑似失效。newsnow 的「自适应最短 2 分钟」与之同类，可作为下限。
- **去重阶梯**：URL/GUID 归一化 → 标题归一化（中文需扩展分隔符：`｜`、`—`、`_`、`-`、媒体名后缀）→ 同源同日句级去重（对中文转载/洗稿很有效）→ 嵌入近似。每层命中写入原始证据层的「重复关系」而非删除，保留可审计性。
- **事件聚类**：Event Registry 的「在线分配到最近簇 + 每 n 篇重评合并/拆分 + k 天窗口退出聚类」非常适合单机；在 infohub 的事件版本模型里，合并/拆分应生成新的事件版本并记录谱系（merged_from / split_from），API 增量同步可将其作为变更事件下发给下游研究系统。
- **事件元数据**：标题取 medoid 文章（或其 LLM 改写版但保留 medoid 引用）；事件日期用「文中最常提到日期，否则平均日期」，避免被误归入的旧文把时间线拉早——对 infohub 持久时间线尤其重要。
- **Mentions 表**：仿 GDELT，为每个事件维护「提及」记录（文档版本 ID、信源、发布时间、首见时间），即可计算传播速度、独立信源数、首发信源，并直接驱动事件时间线和热度（Q3）。
- **跨语言**：infohub 主要输入英文/中文、输出中文。Event Registry 的做法提示：先单语聚类，再用「最近邻交叉落点比例 + 实体 ID 重合 + 时间差」判断跨语言簇是否同一事件；实体以语言无关 ID（如 Wikidata QID、股票代码）表示，对美股/港股频道（ticker 就是天然的语言无关 ID）收益很大。
- **归一化覆盖量**：仿 GDELT TimelineVol，事件/专题热度曲线同时提供原始计数与「占当期全部入库文档的比例」，可区分「新闻真的少了」与「我们的采集掉了」。
- **专题扩展**：Media Cloud 的 Topic Mapper（种子查询 → 跟随超链接 → 仅保留匹配查询者 → 多轮）可用于 infohub 专题页的定向补采，但需限制轮数与域名，避免单机负载失控。

### Gaps
- GDELT 事件表中 NumMentions/NumSources/NumArticles 字段的确切去重语义本轮未核实。
- Event Registry 2023–2026 的现行架构与商业许可/价格未核实（只读了 2014 年论文）。
- Media Cloud 代码许可证、2.0 版采集监控/告警的具体实现未在可获取材料中找到；story-indexer 仓库「2026-09 仍有更新」仅见搜索摘要。
- Media Cloud 去重技术简报原链接（2020-07）已 404，无法核对更多失败案例。

## Q3. 新闻聚合的热度/排序算法，以及如何处理信源多样性与独立性

### Takeaway
公开的经典排序（HN、Reddit）都是「互动信号 × 时间衰减」，不含信源多样性；真正处理多样性/独立性的是 Techmeme（链接数 + 反刷：抑制短时间或少数人产生的大量链接，并由编辑挑「最佳报道」）和 Google News（prominence：多家信源大量且显著报道；Full Coverage 非个性化），以及 Ground News 的覆盖分布。对 infohub 而言，应把「独立信源数」而非「文章数」作为热度主信号，并以对数压缩、时间衰减和人工/LLM 重要性组合。

### Cited Findings
- Hacker News：Score = (P−1)/(T+2)^G，P 为得分（减 1 抵消提交者自投），T 为小时数，G（gravity）默认 1.8（据搜索摘要） — [Medium（Salihefendic）](https://medium.com/hacking-and-gonzo/how-hacker-news-ranking-algorithm-works-1d9b0cf2c08d)
- HN 另有惩罚：用户 flag、反滥用、降权「过热讨论」、版主干预；2013 年逆向分析显示评论数 > 20 且多于票数的帖子乘以 (票数/评论数)²，无 URL 的帖子乘 0.4（2013 年结论，现状可能已变；据搜索摘要） — [righto.com](https://www.righto.com/2013/11/how-hacker-news-ranking-really-works.html)
- Reddit hot（开源旧代码）：45,000 秒 = 12.5 小时，时间项每 12.5 小时加 1 分；票数取 log10，因此一篇帖子要有比 12.5 小时更新的帖子多 10 倍的净票才能排在其上；前 10 票的作用约等于随后 100 票（据搜索摘要） — [Evan Miller](https://www.evanmiller.org/deriving-the-reddit-formula.html); [ScienceBlogs](https://scienceblogs.com/builtonfacts/2013/01/16/the-mathematics-of-reddit-rankings-or-how-upvotes-are-time-travel); [herman.bearblog](https://herman.bearblog.dev/a-better-ranking-algorithm/)
  - 注：开源代码为 `round(sign(s)·log10(max(|s|,1)) + (t−1134028003)/45000, 7)`，即 sign 乘的是对数项；一个搜索摘要把 sign 写在时间项上，属转述错误。
- Techmeme：算法+人工编辑；算法按重要性排序，重要性取决于指向故事页面的链接数和时效；有「反刷」设计，降低短时间内或由少数人产生的大量链接的影响（据搜索摘要，来源维基百科） — [Wikipedia](https://en.wikipedia.org/wiki/Techmeme)
- Techmeme 选稿：独家且标题信息丰富者最高；非独家时选「分析、背景、概括与标题最好」的那篇；首发重要但不是主要区分因素 — [TechCrunch 2011](https://techcrunch.com/2011/10/31/techmeme-opens-the-kimono-on-how-it-chooses-headlines-and-sources/amp/)
- Techmeme 早年曾放弃全自动、引入人工编辑（TechCrunch 标题「TechMeme Gives Up On Fully Automated News」，仅见标题） — [TechCrunch](https://techcrunch.com/?p=31771)
- Google News：Full Coverage 展示围绕某故事的多来源结果，且「不个性化」，依据权威性、相关性、时效性等信号；prominence 指「多家信源大量报道某故事并在其网站上显著展示」；Top Stories 对同一国家所有用户相同；不按政治/意识形态立场排序 — [Google How News Works](https://www.google.com/intl/en_us/search/howsearchworks/how-news-works/)
- Google 发布者帮助页列出的排序因素：相关性、显著性（prominence）、权威性、时效性、可用性、位置与语言（据搜索摘要） — [Google Publisher Center](https://support.google.com/news/publisher-center/answer/9606702?hl=en)
- Ground News 用 Bias Bar 展示同一故事在左/中/右媒体的覆盖分布，Blindspot 标出一侧不成比例报道的故事 — [Ground News](https://ground.news/rating-system)
- 小型实现示例：`log2(engagement) × account_weight × time_decay(半衰期 24h)`，高权重账号 3×；同一故事多家报道合并为一条「交叉验证」条目 — [GitHub ai-daily-news](https://github.com/yeshuai508/ai-daily-news)
- TrendRadar：排名权重 + 频次权重 + 热度权重 — [GitHub](https://github.com/sansan0/TrendRadar)
- Event Registry 聚类时实体权重远高于普通词（使「同一公司/同一人物」的报道更易聚到一起） — [WWW'14 论文](https://archives.iw3c2.org/www2014/proceedings/companion/p107.pdf)

### Inferences
- **推荐的 infohub 热度公式骨架（推断）**：`hot = importance_llm × log2(1 + 独立信源数_加权) × 外部热榜加成 × 2^(−Δt/半衰期)`。
  - 「独立信源数」按信源元数据去重：同一所有权集团、转载/聚合号、同一原文的洗稿只算一次（Techmeme 反刷 + Ground News 所有权分类的思路）；一手来源（公司公告、SEC/HKEX 文件、官方博客）可给更高权重。
  - 对数压缩借鉴 Reddit：首批独立来源的边际价值最高，避免「100 篇转载」压倒「3 家独立一手报道」。
  - 半衰期按频道区分：美股/港股盘中事件衰减快（数小时），AI 研究/机器人事件可更慢（1–2 天）；HN 的 gravity 参数思路即「衰减速度可调」。
  - 「外部热榜加成」来自 newsnow/TrendRadar 等中文热榜命中，仅作加成而非主信号。
- **编辑优先位**：Techmeme 的经验（算法建议、人决定、选「最佳报道」作为主条目）适合单人审核：事件卡片的「主链接」可由 LLM 预选（依据：一手性、信息量、标题清晰度），站长一键改选，选择写入发布台账。
- **覆盖分布展示**：Ground News/Particle 的政治光谱不适用于 infohub，但可以换维度——「中文 vs 英文来源」「一手 vs 二手」「美 / 港 / 大陆媒体」覆盖分布条；对应的「盲点」可定义为「英文一手源大量报道但中文源几乎没有」的事件，这恰是中文读者最需要的增量信息。
- **非个性化**：Google Full Coverage 与 Kagi 都强调故事页不个性化；infohub 作为单站长站点，热点榜与事件页应保持全局一致，便于下游 API 快照可复现。

### Gaps
- HN 现行惩罚参数与 2013 逆向结论是否一致无法核实；HN 不公开排序代码的现行版本。
- Techmeme 当前算法细节（链接图、反刷阈值）无公开文档。
- 未找到 Google News 关于「来源独立性/转载识别」的公开具体规则。
- 未找到经同行评审、可直接套用的「多样性感知新闻热度」公式；上面的骨架是推断。

## Q4. 基于来源的 LLM 日报/简报生成：引用归因、幻觉检查与评估（2023–2026）

### Takeaway
证据表明「让 LLM 直接读多篇新闻写摘要」风险很高：NAACL 2025 Findings 研究发现多文档摘要中最高可达 75% 内容为幻觉，模型在被要求总结不存在的话题时也会照写；EBU/BBC 2025 大规模审计发现 45% 的 AI 助手新闻回答有重大问题，来源归因是最大问题来源（31%）。可行做法是「按构造可溯源」：先把来源拆成带原文片段的原子事实（claim），每句摘要以 claim ID 结尾并能解析回原文 span，再用独立核验器审计——这与 infohub 的「原始证据 + 版本 + 发布台账」架构高度契合。

### Cited Findings
- Kagi News：日报「清楚引用来源」，每个故事列出所有来源并有文内引用；先以原始语言生成再翻译 — [Kagi Blog](https://blog.kagi.com/kagi-news); [NiemanLab](https://www.niemanlab.org/2025/10/kagi-news-is-an-ai-powered-app-for-keeping-up-with-the-world/)
- smol.ai AINews：多阶段摘要、可调试可复现、4 条管线择优、薄人工策展；swyx 认为人工判断信号的作用「不会消失」 — [Heavybit](https://www.heavybit.com/library/article/ai-hidden-opportunities-for-software-developers-swyx)
- Belem et al.（NAACL 2025 Findings）「From Single to Multi」：多文档摘要中「最高 75% 的内容是幻觉」；被要求总结并不存在的话题信息时，gpt-3.5-turbo 79.35% 的情况下仍生成摘要，GPT-4o 为 44%；幻觉更多出现在摘要后段；人工检查 700+ 条 insight，主要错误是不遵循指令和过于泛泛；简单的事后缓解「只中等有效」；数据与代码已发布 — [arXiv 2410.13961](https://arxiv.org/abs/2410.13961)
- 「Attributable by Construction / CAMS」（Guan，arXiv 预印本，2026-06 提交、2026-09 定稿）：将来源拆为原子 claim，provenance 由逐字引文确定性解析到 token span；跨文档聚合等价 claim 并标记来源间冲突；选择有支撑的 claim 子集并改写，使每个摘要句以可解析回原文 span 的 claim ID 结尾；独立支撑模型审计引用质量；多源归因准确率 38% → 64%，每条 claim 的人工核验时间降低 3.4×；在 MultiNews、DiverseSumm 及零样本 WCEP 上评估 — [arXiv 2606.23989](https://arxiv.org/abs/2606.23989)
- DiverseSumm（NAACL 2024）：245 个新闻故事、每个 10 篇文章、人工校验参考；发现用 LLM 做覆盖度与忠实度评估时存在位置偏差与冗长偏差 — [ACL Anthology](https://aclanthology.org/2024.naacl-long.32.pdf)
- EBU/BBC《News Integrity in AI Assistants》（2025-10）：22 家公共媒体、18 国、14 种语言；45% 回答至少有一个重大问题，含轻微问题达 81%；来源归因是重大问题的最大原因（31%）；Gemini 有 72% 的回答存在重大来源问题 — [EBU](https://www.ebu.ch/news/2025/10/ai-s-systemic-distortion-of-news-is-consistent-across-languages-and-territories-international-study-by-public-service-broadcaste); [EBU 报告 PDF](https://www.ebu.ch/files/live/sites/ebu/files/Publications/MIS/open/EBU-MIS-BBC_News_Integrity_in_AI_Assistants_Report_2025.pdf)
- WCEP 数据集把维基百科「时事门户」中人工撰写、中立的事件摘要及其外链来源作为参考摘要，并用 Common Crawl 扩充相关文章（ACL 2020），常被用作多文档新闻摘要的评测集 — [ACL Anthology](https://aclanthology.org/2020.acl-main.120/)

### Inferences
- **Claim 级证据链（与 infohub 架构直接对接）**：在「分析版本」中引入 `claim` 表：`claim_id → evidence_doc_version_id + 原文逐字引文 + 字符 span`；日报/事件摘要每句以 claim ID 结尾，发布时把「句子 → claim → span」映射写入发布台账。下游研究系统通过 API 同步时即可拿到可验证的引用，而不是只有 URL。
- **确定性校验优先**：逐字引文必须能在证据原文中字符串匹配到（中文需做全角/半角、空白归一）；匹配不到则该句不得发布。这是不依赖 LLM 的廉价护栏，正对 EBU 报告中「引用不存在 URL / 引用错位」类问题。
- **独立核验器**：生成与核验分离（CAMS 的思路），核验可以用不同模型或 NLI 式判定「claim 是否被 span 支持」；把核验结果作为分析版本的一部分存档，单人审核时只需看被标红的句子（CAMS 报告核验时间下降 3.4× 的方向）。
- **弃权机制**：Belem 等发现模型会为不存在的话题编内容；infohub 生成事件摘要/日报段落前应设「最少证据数 / 最少独立信源数」门槛，不足则输出「信息不足」而非摘要。
- **重点检查结尾**：幻觉集中在摘要后段，核验器与人工审阅应对结论句、「影响/展望」类句子加严（这类句在美股频道最容易滑向投资暗示）。
- **冲突显式化**：CAMS 在跨文档聚合时标记来源冲突；infohub 事件页可以有「说法分歧」区块（如不同媒体给出的融资金额不同），而不是让 LLM 擅自取一个数字。
- **翻译顺序**：Kagi「先原语言生成、再翻译」与 infohub 的英→中流程相关：可先在英文证据上抽 claim 与引文（便于逐字校验），再翻译成中文，翻译版保留 claim ID 映射。
- **评估方法注意**：DiverseSumm 显示 LLM-as-judge 有位置/冗长偏差；infohub 若用 LLM 评估日报覆盖度/忠实度，应打乱来源顺序、控制长度，并保留少量站长人工标注作为校准集（与 infohub 现有「用户为唯一标注者」的做法一致）。
- **多候选 + 人工挑选**（AINews）与上面的 claim 链可组合：多个候选共享同一 claim 池，挑选成本低且可比较。

### Gaps
- Belem 等的结论基于 gpt-3.5-turbo / GPT-4o 等 2024 年模型，2026 年模型的幻觉率未在本轮找到同口径数据。
- CAMS 为单作者预印本，尚未见同行评审；其数字需谨慎引用。
- 未找到 Kagi、Particle、AINews 公开的幻觉率或事实核验流程数据。
- 未找到专门针对「中文新闻 / 跨语言（英→中）摘要」忠实度的 2024–2026 评测，本轮未覆盖。

## Q5. 覆盖率/召回测量（「我们是否漏掉了大新闻？」）这些系统公开了什么

### Takeaway
几乎没有任何系统公开「漏报率」指标。Media Cloud 明确承认「分母问题」——无法知道全部在线媒体的总体，因此改用相对/关系型的显著性度量；GDELT 用「占当期全部监测量的百分比」做归一化；Kagi 用「每类别至少 25 个源」作为上线门槛；Media Cloud 依靠人工策展与国家专家核验信源集合。对单机 infohub，可行方案是「外部参照集抽样召回」：每天用若干外部榜单/门户（如维基时事门户、Techmeme、AIHOT、newsnow 中文热榜）作为影子参照，计算在 X 小时内被 infohub 覆盖的比例，并把采集量异常与新闻淡季区分开。

### Cited Findings
- Media Cloud 论文明确讨论「分母问题」：要知道某话题占在线媒体报道的比例需要知道在线媒体总体，而穷尽采样不可能（部分内容不公开，且其采集只能覆盖开放网络的很小样本）；因此不去解决分母问题，而转向用「关系型方法」（滚雪球式链接采样 + 中心性/阈值）衡量显著性 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- Media Cloud 承认离线媒体（电视、广播、纸媒）覆盖缺失是方法上的潜在缺口 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- Media Cloud 靠人工「collections curator」定期复核集合、补充缺失 feed；国家集合从 ABYZ 导入后逐一核验并咨询各国专家，部分国家仍「核验较少」 — [arXiv 2104.03702](https://arxiv.org/abs/2104.03702)
- Media Cloud 2.0 迁移期间公开说明数据一致性问题与历史回填进度（2024 年） — [Media Cloud Blog](https://www.mediacloud.org/blog/media-cloud-system-updates)
- GDELT DOC API 的 TimelineVol 以「匹配文章数 / 当期全部监测文章数」报告，另提供原始计数 TimelineVolRaw — [GDELT Blog](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- Kagi：一个类别至少要有 25 个 feed 才会出现在 Kagi News；贡献 feed 时需检查其有近期的每日内容 — [GitHub kite-public](https://github.com/kagisearch/kite-public)
- Huginn Trigger Agent 可对高频事件计数并在激增时即时告警（可作为采集量/话题量异常检测的模式） — [GitHub](https://github.com/huginn/huginn)
- 维基百科时事门户提供中立的人工事件摘要及外链来源，已被 WCEP 用作新闻事件参考集 — [ACL Anthology](https://aclanthology.org/2020.acl-main.120/)
- smol.ai AINews 在低新闻日公开以「not much happened today」作标题，即把「无大事」作为显式判断输出 — [AINews issue](https://news.smol.ai/issues/26-01-09-not-much/)

### Inferences
- **影子参照集召回（推荐，推断）**：每天自动抓取若干外部参照列表（维基时事门户「科技/商业」条目、Techmeme 首页、AIHOT、newsnow 的中文科技热榜、港交所/SEC 重大公告清单），对每条参照用实体 + 嵌入匹配 infohub 事件；指标为「T+2h / T+12h / T+24h 覆盖率」与「首见时间差」。参照集本身有偏，所以只作相对指标与趋势监控，不宣称绝对召回（呼应 Media Cloud 的分母问题）。
- **漏报分诊**：每个未覆盖的参照条目自动归因为（a）根本没有采集到相关文档 → 信源缺口；（b）采集到但被相关性过滤掉 → 分类阈值问题；（c）采集到但未聚成事件/重要性过低 → 聚类/排序问题。infohub 的版本化原始证据层天然支持这种回溯；分诊结果可直接成为站长的标注队列。
- **采集量归一化监控**：仿 GDELT，信源健康页同时显示「每源/每频道每小时入库数」与「占总量比例」；总量骤降 → 采集故障，单一频道比例骤降 → 某类信源失效；结合 Media Cloud 的退避级别和 Huginn 式激增告警。
- **频道上线门槛**：仿 Kagi「≥25 feed」，为每个频道定义最低独立信源数与语言/地区配比（例如港股频道需同时有港交所披露、英文媒体、中文媒体），低于门槛在健康页标黄。
- **显式「平静日」**：日报允许输出「今日无重大事件」，并在台账中记录当日最高重要性分数——这本身就是召回监控的数据点（若参照集当天有大事而 infohub 判「平静」，即为强告警）。
- **人工策展节奏**：Media Cloud 依赖定期人工复核信源集合；infohub 可在信源健康页加入「季度复核」任务（由持久任务定期提醒），对长期零贡献或高重复率的信源做下线/降权决策。

### Gaps
- 未找到 GDELT、Media Cloud、Kagi、Ground News 中任何一家公开发布「漏报率/召回率」类运营指标或方法。
- 一个搜索摘要称「由于不存在完整事件库，事件检测的召回无法被公平计算、维基时事门户在数量与粒度上有限」，但未能核实其原始出处（被指向的 arXiv 1806.07573 摘要中并无此表述），故不作为事实引用。
- GDELT 的信源覆盖统计（监测的媒体数量、按国家/语言分布）在本轮抓取的博文中没有具体数字。
- 未找到关于「中文科技/财经新闻覆盖召回」的公开评测或基准。
