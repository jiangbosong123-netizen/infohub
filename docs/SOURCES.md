# 信息源：选择原则、实测稳定性与候选评估

源注册表在 [`app/crawler/sources.py`](../app/crawler/sources.py)，发布方识别在
[`app/provenance.py`](../app/provenance.py)。本页记录为什么是这些源，以及评估过、没有采用的源。

## 选择原则

1. **优先一手、优先官方订阅。** 公司与实验室官网、监管机构、交易所的官方 RSS/Atom，和老牌媒体自己提供的
   RSS，接口公开、格式稳定，最不容易突然失效。逆向得到的私有接口（财联社签名接口、华尔街见闻快讯接口、
   Google News）目前可用，但最可能被对方改动或限流；雅虎财经的 13 个个股源就是被 429 限流后停用的。
2. **量小、信号密。** 每条新内容都会进入 AI 相关性与重要性判断，日均上百条的综合新闻源（CNBC 头条、
   MarketWatch、纳斯达克、Seeking Alpha）会显著增加噪声与模型调用，宁缺毋滥。
3. **必须有可信的发布时间。** 条目没有发布时间时只能记为抓取时间，第一次抓取会把旧文章当成新文章，
   这类源（苹果新闻室、Investing、BIS）暂不收录。
4. **`official` 层级要谨慎。** `tier="official"` 的条目不经 AI 筛选直接展示并进入精选，只给发布即重要、
   量很小的一手来源（实验室公告、公司新闻稿、FOMC 声明）。研究博客和内容混杂的公司新闻室设为 `info`，
   仍由 AI 判断相关性。
5. **新源的域名要登记发布方。** 否则 Google News 转载的同一发布方和直接抓取的文章会被当成两家，
   让事件的“多家来源印证”（`source_count>=2`，影响精选）被重复计数。

## 现有源的实测稳定性（2026-09-18 至 10-03，旧库只读副本）

- 启用的 42 个源共抓取 30,787 次，成功 97.5%，每个源都在 97–98%。
- 失败中约 700 次是采集机本地断网（DNS 解析失败、网络不可达），对方超时只有约 10 次。
- 真正的不稳定来自采集机：356 小时中约 40 小时没有抓取（51 段超过 30 分钟的空档，最长 1.6 小时），
  估计因此漏掉新浪 7x24 约 800 条、财联社约 1,000 条快讯；财联社最新一页只有 20 条，繁忙时段不停机
  也会漏。快讯源现已在断档后往前翻页补抓（[`catchup.py`](../app/crawler/catchup.py)）。
- 10 月 3 日的停机与源无关：服务直接从开发目录运行，重启时加载了需要新配置的代码。

## 2026-10-06 新增的 19 个源

均用项目自己的 RSS 抓取器实际抓取验证（成功、条目有发布时间、近期有更新），并在隔离库里经 `run_source`
完整入库一次，`official` 标记与发布方识别均正确。日均条数按单次抓取中条目的时间跨度估算。

| 频道 | key | 名称 | 层级 | 约日均 |
|---|---|---|---|---|
| ai | `deepmind` | Google DeepMind 博客 | official | 0.3 |
| ai | `google-ai-blog` | Google AI 官方博客 | official | 0.5 |
| ai | `mistral-news` | Mistral AI 官方动态 | official | 0.1 |
| ai | `google-research` | Google Research 博客 | info | 0.3 |
| ai | `microsoft-research` | 微软研究院博客 | info | 0.2 |
| ai | `apple-ml` | Apple 机器学习研究 | info | 0.8 |
| ai | `nvidia-newsroom` | NVIDIA 新闻室（含其博客全部文章） | info | 1.3 |
| ai | `github-blog` | GitHub 博客 | info | 0.8 |
| ai | `the-decoder` | The Decoder | info | 9.6 |
| ai | `mit-tr-ai` | MIT 科技评论 · AI | info | 1.3 |
| ai | `arstechnica-ai` | Ars Technica · AI | info | 2.8 |
| ai | `wired-ai` | WIRED · AI | info | 2.1 |
| ai | `geekpark` | 极客公园 | info | 2.6 |
| robot | `robohub` | Robohub | info | 0.4 |
| robot | `techcrunch-robotics` | TechCrunch 机器人 | info | 1.1 |
| stock | `amd-newsroom` | AMD 新闻稿 | official | 0.1 |
| stock | `fed-monetary` | 美联储货币政策（FOMC 声明等） | official | 0.1 |
| stock | `cnbc-earnings` | CNBC 财报 | media | 0.7 |
| stock | `arm-newsroom` | Arm 新闻室 | media | 0.3 |

合计约每天 25 条，相对现有每天约 4,000 条可以忽略，模型调用增加很少。同时登记了这些域名与
The Robot Report 的发布方。在旧库上，发布方标识变化的 349 条条目绝大多数只是改名；43,861 个事件中只有 1 个
的发布方数变化（原先把同一家的两个名字算成两家）。

## 评估过但未采用的候选

| 候选 | 结果 | 原因 |
|---|---|---|
| 36氪、机器之心、品玩、Palantir IR | 返回网页而非 RSS | 已无公开 RSS 或拦截程序访问 |
| 雷锋网 | 证书校验失败 | 不稳定 |
| 虎嗅；Broadcom、百度、理想、小鹏 IR | 25 秒超时 | 不稳定 |
| 微软 AI 博客 | 410 已下线 | — |
| Tesla IR | 403 | 拦截程序访问 |
| TSMC、Oracle、NVIDIA IR、Meta IR、A3 | 404 | 地址失效或无 RSS |
| Meta AI 博客、Anthropic | 400/404 | 官网没有 RSS |
| 通义千问博客、WSJ 公开 RSS、VentureBeat AI | 能抓到但一年左右未更新 | 已停更 |
| 苹果新闻室、Investing、BIS | 条目无发布时间 | 见原则 3 |
| CNBC 头条、MarketWatch、纳斯达克、Seeking Alpha | 日均 46–1,300 条综合新闻 | 量大且杂，见原则 2 |
| 钛媒体、AWS 机器学习、InfoQ 中文、少数派 | 可用 | 量大偏泛或偏开发教程，暂不收 |
| 微软新闻、Meta 新闻室、亚马逊新闻 | 可用 | 公司新闻混杂（社区、品牌活动），暂不收 |
| SEC 新闻稿、美联储全部新闻稿、欧洲央行 | 可用 | 多为执法、银行审批或欧洲事务，与科技股关联弱；美联储只收货币政策 |
| Robotics Business Review | 可用 | 已并入 The Robot Report，15 条与现有源完全重复 |

单次抓取只能证明“现在可用”。新源上线后看健康页（`/health`）的连续失败数、最近成功时间与最近新内容：
连续失败的源会自动退避并标红；抓取成功但新内容间隔远超自身节奏的源标为“静默”
（见 [WORKER_OPERATIONS](WORKER_OPERATIONS.md)），用于发现订阅停更或接口只返回旧条目。
