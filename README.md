# 行业情报站

按 [AIHOT 主题页](https://aihot.news/topics) 的信息组织方式完善。新增 **主题总览 → 主题详情 → 持久事件时间线**，实现与公开源码核查见 [AIHOT 对标说明](docs/AIHOT_ALIGNMENT.md)。

对标 [aihot.news](https://aihot.news/) 的行业信息聚合站：**AI / 机器人 / 美股港股科技企业** 三个频道，
多源抓取 → 热度聚类 → 热点榜 + 按日期时间线 + 每日日报，可选接入 LLM 做 AI 策展。

Mac 是开发端，Windows Docker 是当前生产运行端。默认本地命令使用
`.runtime/development-local/` 下的隔离数据，并且不会抓取外网或调用模型。

## 快速开始（本机 Mac）

```bash
# 1. 安装依赖（系统自带 python3 即可）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 查看当前环境和数据路径（输出不包含密钥）
.venv/bin/python cli.py runtime-config

# 3. 初始化隔离的开发数据库（导入公司清单 + 源注册表）
.venv/bin/python cli.py init-db

# 4. 启动本地门户。开发环境默认只启动网页，不运行定时任务
.venv/bin/python cli.py serve
# 浏览器打开 http://127.0.0.1:8000

# 确实需要一次性抓取开发样本时，必须显式放行该次网络任务
INFOHUB_ALLOW_NETWORK_TASKS=true .venv/bin/python cli.py crawl
```

## 旧 Mac 常驻配置（迁移兼容）

仓库仍保留旧 `launchd/com.infohub.server.plist` 供一个发布周期内识别和回退，但不再推荐
把 Mac 作为第二个生产采集器。已有旧库只有在明确设置
`INFOHUB_LEGACY_DATA_LAYOUT=true` 时才使用 `data/app.db`；不会自动搬动或修改该文件。

```bash
INFOHUB_LEGACY_DATA_LAYOUT=true .venv/bin/python cli.py runtime-config
```

## Windows 服务器与 Tailscale 访问

推荐在 Windows 的 Docker Desktop + WSL2 中常驻运行。首次部署时复制 `.env.example`
为 `.env`，按需填写模型配置，然后执行 `docker compose up -d --build`。容器配置了
`restart: unless-stopped`，Docker 恢复后会自动重新启动；SQLite 数据持久化在宿主机的
`data/` 目录。Compose 显式设置 `windows-production` 环境、`/app/data/app.db`、blob、备份
目录和调度开关，缺少生产标识或路径时应用会拒绝启动。

同一 Tailscale 网络内的设备可通过 `http://<Windows 的 Tailscale IP>:8000` 访问。
只需允许 Windows 防火墙的专用网络或 Tailscale 网络访问 8000 端口，不要在路由器上
做公网端口映射。

`/api/health` 提供机器可读的运行版本、信息源异常、AI 待处理量、主题/事件索引积压和
日报状态，并标明 `environment_id`、环境类型、进程角色和调度状态；不会暴露数据库路径。
网页 `/health` 展示相同的运维概览。通过 Windows Server Manager 部署时，构建版本会
自动记录为当前 Git 提交号。

## 实时性设计

- **财联社电报 / 华尔街见闻快讯 / 新浪 7x24** 三条分钟级中文快讯线，各每 10 分钟轮询；SEC / 港交所每 10 分钟
- **Techmeme**（美国科技圈最强聚合）30 分钟；**每家公司专属 Google News 源**每 20 分钟一轮（中英别名严格匹配，防串公司）
- 首页每 2 分钟自动刷新，顶部显示「数据更新于 X 分钟前」
- Windows 生产 Compose 运行调度；Mac 开发环境默认关闭调度，避免两台机器重复采集

> 财联社的接口签名算法与华尔街见闻快讯端点，分别借鉴了 GitHub 开源项目
> [RSSHub](https://github.com/DIYgod/RSSHub) 与 [newsnow](https://github.com/ourongxing/newsnow)
> 的公开实现，在此致谢。36氪快讯因其内容加密+WAF 反爬暂未收录；机器之心可通过自建 RSSHub 实例补上。

## 接入 AI 策展（可选）

生产环境是否启用取决于 Windows 的 `.env`；仓库不包含生产密钥，程序只在运行环境读取。生效逻辑：

- 英文源自动翻译成中文摘要；每条打 0-100 重要性评分（重大事件 80-100，例行文件 <50 沉底）
- 股市条目自动标注事件类型（财报/回购/并购/评级/内部人交易…）
- 未评分条目均进入处理队列，优先处理新条目
- 每天早 8 点由 GLM 生成三频道行业日报；AI 每 15 分钟自动处理一批新条目

想换模型/厂商：改 `.env` 里 `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY`（任何 OpenAI 兼容接口均可，
DeepSeek、本地 Ollama 等），重启服务即生效。删除或清空模型配置则退回纯聚合模式。
开发环境即使存在模型凭据，也要显式设置 `INFOHUB_ALLOW_NETWORK_TASKS=true` 才会调用。

## 防漏设计（股市频道重点）

| 层 | 源 | 频率 | 作用 |
|---|---|---|---|
| 官方一手 | SEC EDGAR（8-K/10-Q/Form 4）、港交所披露易、OpenAI 官网 | 10-60 分钟 | 官方披露优先覆盖 |
| 财经媒体 | CNBC、华尔街见闻、每公司 Google News 源 | 20-240 分钟 | 速度与覆盖面 |
| 每日对账 | Google News 按公司全网检索 | 每天 06:30 | 与库存比对，漏的自动补录并标「对账补录」 |

配合机制：URL 归一去重、每源失败退避（连续失败在「源状态」页标红）、同域名请求错峰防限流、
每天早 8 点生成前一日日报。

## 日常使用

- **首页** `/`：今日热点榜 + 频道 Tab + 按日期分组时间线；股市频道可按公司 / 事件类型（财报、回购、并购、评级…）筛选
- **热点榜** `/hot`：近期持久事件，按可识别发布方计数，点击进入报道时间线
- **主题** `/topics`：公司与模型、技术方向、内容形态三组；每个主题都有统计、近期焦点与精选
- **事件** `/story/{id}`：稳定链接、多源报道、原始信息与归并依据
- **日报** `/daily`：每日自动生成
- **搜索**：右上角，标题/摘要全文检索
- **源状态** `/health`：每个源最近成功时间、连续失败数、错误信息（绿色正常 / 红色故障）

## 加公司 / 加源

- **加公司**：编辑 `config/watchlist.yaml` 加一段（美股填 ticker，港股填 code，
  SEC CIK 和港交所 stockId 会自动解析），重跑 `python cli.py init-db`
- **加信息源**：在 `app/crawler/sources.py` 的 `SOURCES` 里加一条（RSS 填 url 即可），
  重跑 `python cli.py init-db`

## 项目结构

```
app/
├── crawler/          # 抓取层
│   ├── sources.py    #   源注册表（加源改这里）
│   ├── rss_source.py #   RSS 抓取
│   ├── sec_source.py #   SEC EDGAR（CIK 自动解析）
│   ├── hkex_source.py#   港交所披露易（stockId 自动解析）
│   ├── googlenews.py #   Google News 公司源 + 每日对账
│   └── runner.py     #   调度 / 入库去重 / 源健康
├── ai/               # LLM 策展（摘要 / 评分 / 日报），无 Key 自动降级
├── ranking.py        # 热度算法 + 热点聚类（标题相似度 + 多信源加成）
├── web/              # FastAPI + Jinja2 页面
└── database.py       # SQLite（WAL）schema
cli.py                # init-db / crawl / reconcile / ai / report / serve
config/watchlist.yaml # 关注公司清单
```

## 已知边界

- 机器之心官网为纯 JS 渲染且 RSS 已失效，暂未收录（可后续用无头浏览器补）；36氪官方 feed 的 XML 格式损坏，暂未收录
- Yahoo 个股 feed 限流极严格，已用「每公司 Google News 源（20 分钟一轮）+ 每日对账」替代
- 付费墙内消息（如 Bloomberg 终端）与 X 上的零散快讯（未接付费 API）不保证覆盖；
  但科技企业重大事件（财报 / 并购 / 监管 / 回购 / 大单 / 产品发布）经三层冗余 + 对账，覆盖率尚未经过独立样本验证，不能保证不漏


## 开发验证与本轮改进

完整评估、已修复问题和后续计划见 [项目评估](docs/PROJECT_REVIEW.md)。

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app cli.py
```

测试使用临时数据库和模拟 AI 响应，不抓取外网、不调用付费模型。
GitHub Actions 在 push / PR 时执行检查（Python 3.11 / 3.12）。

升级已有实例时，`cli.py init-db` 会先识别数据库版本。旧库需要变更时自动通过
SQLite backup API 在配置的 `INFOHUB_BACKUP_PATH` 创建带环境标签的一致性备份，再以显式事务迁移；失败会
完整回滚。未知的新版本、迁移记录被改动、完整性或外键检查失败时会停止启动，不继续写库。
每条 `schema_migrations` 记录同时保存执行迁移的 `APP_VERSION`，用于把数据库变化追溯到
具体发布版本；本地未注入构建版本时明确记录为 `unknown`。

也可以在部署门禁中单独执行：

```bash
python cli.py db-status                 # 只读检查；兼容尚未登记的已知旧库
python cli.py db-backup                 # 手动创建一致性备份，不覆盖已有文件
python cli.py db-migrate                # 仅迁移；需要变更的旧库会先备份
python cli.py db-verify                 # 要求完整性通过且schema为当前版本
python cli.py runtime-config            # 显示非敏感运行配置和实际数据路径
python cli.py jobs-status               # 显示持久任务开关与各状态数量
```

命令输出中的 `file_sha256` 是指定 `.db` 文件的校验值；`db-backup` 生成的是单文件备份，
可用该值核对传输。运行中的 WAL 数据库还可能有 `-wal` 内容，不能仅凭主文件校验值代表
整个实时数据集。

初始化会自动把历史中文标题纳入 FTS 搜索索引；重复运行不会重复迁移或重复备份。
新增 `nh3` 用于清洗日报 HTML。精选在 AI 已配置时按事件去重，只展示评分 ≥70、
官方条目或至少两家发布方共同报道的事件，
低分公司新闻仍可在「全部动态」查看；未配置 AI 时精选退回全量聚合。

健康页现在同时显示部分抓取失败和长期未更新；全部失败的源按基础间隔指数退避，部分公司失败时仍按原频率轮询，
最长 6 小时（基础间隔本身超过 6 小时的源保持其基础间隔）。

当前数据库已包含持久任务、追加式任务尝试和持久定时计划的基础表。任务领取使用有期限的
lease token；续租、完成、失败、阻塞和运行中取消都必须持有仍有效的 token，过期 worker
不能回写结果。同一幂等键只能代表同一份输入；失败按上限重试，超过上限进入 dead letter；
重启期间错过的相同定时计划合并为一个任务。`INFOHUB_DURABLE_JOBS_ENABLED` 是后续 worker
切换的发布开关，目前 Compose 明确保持 `false`，现有 APScheduler 继续工作，不会出现两个
调度器同时发任务。


## 主题与事件维护

编辑 `config/topics.yaml` 可以增加或调整主题规则；关注公司自动转为公司主题。

```bash
.venv/bin/python cli.py reindex  # 本地增量更新，不抓取外网、不调用 LLM
```

`serve` 启动时自动初始化数据库和索引。抓取、AI 定时处理结束也会更新主题与事件。
历史事件链接保留；未识别发布方的聚合入口不增加发布方数量。算法使用保守的标题、
版本、时间与实体规则，仍可能漏合并大幅改写的报道，具体边界见对标说明。
