# 行业情报站

**AI / 机器人 / 美股港股科技企业** 三个频道的行业信息聚合站：多源分批抓取 → 去重与热度聚类 → 主题、
持久事件时间线、热点榜与每日日报，可选接入 LLM 做 AI 策展。信息组织方式对标
[AIHOT 主题页](https://aihot.news/topics)，核查见 [AIHOT 对标说明](docs/AIHOT_ALIGNMENT.md)。

它无人值守运行。真正要解决的不是“抓到新闻”，而是**确信数据库里的内容就是你以为的那样**。

> English: [README.md](./README.md)

## 当前状态一览

| 层 | 状态 |
|---|---|
| 门户 + 抓取 + 每日日报 | 可用。运行中的实例目前提供的就是这一层。 |
| v1 数据底座（原始证据、版本化文档/事件/分析、持久任务、发布账本、鉴权只读 API、同步快照） | 已在 `main`，全部由**默认关闭**的开关控制；生产尚未升级启用。 |
| NLP 质量评估（relevance、tone、impact） | 契约、校验器和复核工具已有；**还没有真实标注数据，也没有任何模型质量结论**。仓库里的数据集都是只用于验证工具的合成样本。 |

实现与规划的边界以 [`docs/spec/IMPLEMENTATION_STATUS.md`](docs/spec/IMPLEMENTATION_STATUS.md) 为准，
目标架构见 [`SPEC.md`](SPEC.md)。

## 快速开始（本机开发）

需要 Python 3.11 或 3.12（CI 两个版本都测，容器使用 3.12）。macOS 自带的 Python 3.9 太旧。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python cli.py runtime-config   # 查看环境与数据路径（不含密钥）
.venv/bin/python cli.py init-db          # 初始化隔离开发库
.venv/bin/python cli.py serve            # 浏览器打开 http://127.0.0.1:8000
```

`requirements.txt` 锁定了全部依赖（含间接依赖）的版本和校验哈希，CI、Docker 镜像和 macOS 部署包装的都是这同一套。
版本范围写在 `requirements.in`；要改或升级依赖，改那里后用
`uv pip compile requirements.in --universal --python-version 3.11 --generate-hashes -o requirements.txt`
重新生成。镜像的 Python 基础镜像在 `Dockerfile` 里按摘要固定。

开发环境默认是“静止”的：数据在 `.runtime/development-local/`，网络任务、调度和模型调用全部关闭。
确实需要一次性抓取样本时必须显式放行：

```bash
INFOHUB_PROCESS_ROLE=maintenance INFOHUB_ALLOW_NETWORK_TASKS=true .venv/bin/python cli.py crawl
```

没有模型密钥时作为纯聚合站运行，LLM 层自动降级而不是报错。

## 页面与接口

| 页面 | 内容 |
|---|---|
| `/` | 今日热点榜 + 频道 Tab + 按日期分组时间线；股市频道可按公司 / 事件类型（财报、回购、并购、评级…）筛选 |
| `/hot` | 近期持久事件，按可识别发布方计数，点击进入报道时间线 |
| `/topics`、`/topics/{slug}` | 公司与模型、技术方向、内容形态三组主题，各有统计、近期焦点与精选 |
| `/story/{id}` | 事件稳定链接：所有报道来源、原始条目与归并依据 |
| `/daily` | 每日自动生成的日报 |
| `/search`、`/saved` | 标题/摘要全文检索；本地收藏 |
| `/health` | 每个源最近成功时间、连续失败数与错误信息 |

机器可读接口：`/api/live`（web 进程可响应）、`/api/ready`（数据库身份正确且同版本 worker 心跳新鲜）、
`/api/pipeline`（来源、任务、索引与日报新鲜度）、`/api/health`（完整快照，未就绪时 503）。
`/api/v1/*` 只读 API（items、events、evidence、analyses、目录、同步快照、changes）需要带对应 scope 的
API key，且对应开关打开后才可用，见 [API 鉴权](docs/API_AUTH_FOUNDATION.md) 与
[API 读取契约](docs/API_READ_CONTRACT.md)。

## 防漏设计（股市频道重点）

| 层 | 源 | 频率 | 作用 |
|---|---|---|---|
| 官方一手 | SEC EDGAR（8-K/10-Q/Form 4）、港交所披露易、公司与实验室官方动态（OpenAI、Google DeepMind、Mistral、AMD 等）、美联储货币政策 | 10–60 分钟 | 官方披露优先覆盖 |
| 财经媒体 | CNBC、华尔街见闻、财联社电报、新浪 7x24、每家关注公司一个 Google News 源 | 10–30 分钟 | 速度与覆盖面 |
| 每日对账 | Google News 按公司全网检索 | 每天 06:30 | 与库存比对，漏的自动补录并标「对账补录」 |

配合机制：URL 归一去重、每源失败指数退避（连续失败在健康页标红）、同域名请求错峰防限流、每天早 8 点
生成前一日日报。调度按交易所当地时间锚定，不写死 UTC 偏移。

全部信息源的选择原则、实测稳定性和评估过的候选见 [信息源说明](docs/SOURCES.md)。

三个分钟级快讯源（新浪 7x24、财联社电报、华尔街见闻快讯）每次只返回最新一页。最新一页里若没有一条已入库
（快讯发得比轮询快，或采集机离线过），就往前翻页，直到接上已有内容；最多再翻 20 页，且不早于该源上次成功
抓取的时间（`app/crawler/catchup.py`）。正常轮询仍只发一次请求。按 2026-09-18 至 10-03 的旧库记录，采集机
356 小时里约 40 小时没有抓取，估计因此漏掉新浪约 800 条、财联社约 1,000 条；财联社最新一页只有 20 条，繁忙
时段即使不停机也会漏。

## 项目结构

```
app/
├── crawler/               抓取层：源注册表、RSS/SEC/港交所/Google News 连接器、调度与去重
├── ingest.py, documents.py, source_time.py
│                          不可变原始观察（内容寻址）、文档版本、来源时间规则
├── worker.py, jobs.py, publication.py, runtime_health.py
│                          带租约的持久任务、原子发布账本、web/worker 健康
├── catalog.py, sec_identity.py, company_match.py
│                          版本化实体目录、SEC 发行人/证券语义、中英别名严格匹配
├── event_*.py, stories.py, ranking.py, topics.py, topic_*.py, curation_*.py
│                          稳定事件（匹配、关系、修订）、主题、策展投影
├── analysis_*.py, tone_*.py, impact_*.py, ai/
│                          版本化 NLP：固定输入、可审计调用、证据校验后的结果
├── report_*.py            日报快照、草稿、复核与发布
├── api_*.py, web/         FastAPI 门户与鉴权 /api/v1 只读 API
├── evaluation*.py, review_intake.py
│                          评估数据集、防泄漏检查、人工复核导入、指标
└── database.py, db_admin.py, evidence_backup.py
                           SQLite（WAL）schema 与迁移、可校验的备份与恢复

cli.py                     全部运维命令（init-db、serve、worker、db-*、复核工具…）
config/watchlist.yaml      关注公司清单
config/topics.yaml         主题规则
evaluation/                数据集契约、合成样本、基线（私有数据被 Git 忽略）
```

- **加公司**：编辑 `config/watchlist.yaml`（美股填 ticker，港股填 code，SEC CIK 与港交所 stockId 自动
  解析），重跑 `init-db`。
- **加信息源**：在 `app/crawler/sources.py` 的 `SOURCES` 里加一条，重跑 `init-db`。

## 无人值守运行

生产是 Windows 上的 **Docker Compose**：一次性 `migrate` 容器（`cli.py prepare-release`，先备份再迁移）、
永不抓取的只读 `infohub` 门户容器，以及唯一负责调度、抓取和模型调用的 `worker` 容器。SQLite、证据
blob、备份和心跳持久化在 `./data`。门户端口只绑定 `127.0.0.1`，通过 Tailscale Serve 私网 HTTPS 访问
（[私网 HTTPS 生产入口](docs/PRIVATE_HTTPS_INGRESS.md)）。

配套部署管理器（独立仓库）轮询本仓库并以 fast-forward 方式应用 `main` 的更新，把提交 SHA 传入容器，
使 `/api/health` 报告确切的运行版本。日常命令、备份与恢复见 [运行手册](docs/RUNBOOK.md)。第一次生产升级的方案（基于真实数据完整演练）见
[生产升级方案](docs/CUTOVER_PLAN.md)。在这台 Mac 上用 launchd
运行同样三种角色的备选方案见 [`deploy/macos/`](deploy/macos/README.md)，两台机器只能有一台采集。

## 测试与 CI

```bash
.venv/bin/python -m compileall -q app cli.py
.venv/bin/python -m unittest discover -s tests
```

测试使用临时数据库和模拟 AI 响应，不抓取外网、不调用付费模型，全套一分钟内跑完。GitHub Actions 在
每次 push / PR 时用 Python 3.11 和 3.12 执行，构建容器镜像后在镜像里再跑一遍并按生产方式启动。另有一项检查运行
`ruff check --select F`（未定义名称、未使用的导入）和对锁定依赖的 `pip-audit` 漏洞检查；测试还会核对所有 Markdown
相对链接和命令行用法说明与代码一致。

## 接入 AI 策展（可选）

通过 `.env` 的 `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY` 接入任何 OpenAI 兼容接口，凭据只注入 worker。
启用后：英文源翻译成中文摘要；每条打 0–100 重要性评分；股市条目标注事件类型；每天生成三频道日报。
清空模型配置即退回纯聚合模式。

## 评估与标注

数据集格式、防泄漏分割和复核工具见 [`evaluation/README.md`](evaluation/README.md)。目前项目只有一名
人工标注者（所有者）。按 [SPEC §8.2](docs/spec/NLP_AND_EVALUATION.md)，所有者单人标注的结果只算
*experimental*，不冒充多人 gold；模型输出也不能给自己出正式真值。单人标注路径正在建设，进度见实施
状态页。

## 已知边界

- 两个源未收录：一个纯前端渲染且 RSS 已失效，一个 feed 格式损坏；都需要无头浏览器才能妥善修复。
- 一家快讯因内容加密且有 WAF 暂未收录。
- 某大型门户的个股 feed 限流过严，已用“每公司 Google News 源（20 分钟一轮）+ 每日对账”替代。
- 付费墙内容与未接付费 API 的零散社交消息不覆盖。
- 事件聚类使用保守的标题、版本、时间与实体规则，大幅改写的报道仍可能漏合并。
- **重大公司事件的覆盖率尚未用独立样本验证。** 三层冗余加对账旨在让遗漏很少，但没有测量过，不是保证。
- **任何 NLP 输出都还没有经过测量的质量。** 在有真实标注数据之前，tone 与 impact 结果保持 review-only。

## 致谢

财联社的接口签名算法与华尔街见闻快讯端点，分别借鉴了 [RSSHub](https://github.com/DIYgod/RSSHub) 与
[newsnow](https://github.com/newsnext/newsnow) 的公开实现。
