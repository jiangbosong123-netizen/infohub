# 运行手册

README 只保留概览；本页集中记录本机开发、Windows 生产和日常运维命令。各能力的完整契约仍以对应
`docs/*.md` 为准，是否已经上线以 [`docs/spec/IMPLEMENTATION_STATUS.md`](spec/IMPLEMENTATION_STATUS.md)
为准。

## 1. 本机开发（Mac）

需要 Python 3.11 或 3.12（CI 两个版本都测，容器使用 3.12）。macOS 自带的 Python 3.9 太旧，代码使用了
3.10+ 语法。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python cli.py runtime-config   # 环境与数据路径，不输出密钥
.venv/bin/python cli.py init-db          # 初始化隔离开发库：公司清单、源注册表、身份目录
.venv/bin/python cli.py serve            # http://127.0.0.1:8000
```

开发环境默认使用 `.runtime/development-local/` 下的隔离数据，关闭网络任务、调度和模型调用。确实需要
一次性抓取样本时，必须用 maintenance 角色并显式放行网络：

```bash
INFOHUB_PROCESS_ROLE=maintenance INFOHUB_ALLOW_NETWORK_TASKS=true .venv/bin/python cli.py crawl
```

即使 `.env` 中存在模型凭据，开发环境也要显式设置 `INFOHUB_ALLOW_NETWORK_TASKS=true` 才会调用。

### 旧 Mac 常驻配置

仓库保留 `launchd/com.infohub.server.plist` 只为识别和回退，不再推荐把 Mac 作为第二个生产采集器。
该 plist 只运行 `cli.py serve`；当前代码中 `serve` 只验证数据库并提供页面，不初始化数据库、不抓取、
不运行任务，因此旧 plist 不能再充当采集器。已有旧库只有在明确设置 `INFOHUB_LEGACY_DATA_LAYOUT=true`
时才使用 `data/app.db`，程序不会自动搬动或修改该文件：

```bash
INFOHUB_LEGACY_DATA_LAYOUT=true .venv/bin/python cli.py runtime-config
```

## 2. Windows 生产

在 Windows 的 Docker Desktop + WSL2 中常驻运行。首次部署复制 `.env.example` 为 `.env`，按需填写模型
配置，然后执行 `docker compose up -d --build`：

- `migrate`：一次性运行 `cli.py prepare-release`，先做一致性备份再迁移，并清除旧版本 worker 心跳；
- `infohub`：只读门户（web 角色），永远不抓取、不调度，端口只绑定 Windows 本机 `127.0.0.1:8000`；
- `worker`：唯一后台，负责调度、抓取和模型调用，只有它读取 `.env` 中的模型凭据。

两个常驻容器均为 `restart: unless-stopped`。SQLite、blob、备份和进程心跳持久化在宿主机 `data/`。
三个角色的权限和数据路径由 Compose 显式注入，缺少生产标识、路径或角色时应用拒绝启动。

生产入口使用 Tailscale Serve 的私网 HTTPS 地址。不要启用 Tailscale Funnel，不要为 8000 端口添加入站
防火墙规则或路由器端口映射。切换与回滚见 [私网 HTTPS 生产入口](PRIVATE_HTTPS_INGRESS.md)，web/worker
运维边界见 [web/worker 运维说明](WORKER_OPERATIONS.md)。

健康检查：

- `/api/live`：web 进程能响应；
- `/api/ready`：数据库身份正确且同版本 worker 心跳新鲜；
- `/api/pipeline`：来源、任务、索引和日报新鲜度；
- `/api/health`：完整快照，发布未就绪时返回 503；
- 网页 `/health`：即使 worker 停止也可读，并明确显示后台延迟。接口不暴露数据库路径。

## 3. 数据库、备份与证据

`init-db` 与 `prepare-release` 会先识别数据库版本。旧库需要变更时先用 SQLite backup API 在
`INFOHUB_BACKUP_PATH` 创建带环境标签的一致性备份，再以显式事务迁移，失败完整回滚。未知的新版本、迁移
记录被改动、完整性或外键检查失败时停止启动。每条 `schema_migrations` 记录保存执行迁移的
`APP_VERSION`；本地未注入构建版本时记为 `unknown`。

```bash
python cli.py db-status                      # 只读检查；兼容尚未登记的已知旧库
python cli.py db-backup                      # 一致性单文件备份，不覆盖已有文件
python cli.py db-bundle-backup               # 备份 SQLite 与实际引用的全部 CAS 证据
python cli.py db-bundle-verify PATH          # 校验备份包中的数据库与证据
python cli.py db-bundle-restore BUNDLE DEST  # 恢复到新的隔离目录，不切换当前服务
python cli.py db-bundle-smoke PATH           # 在临时副本上离线打开主要门户页面
python cli.py db-migrate                     # 仅迁移；需要变更的旧库会先备份
python cli.py db-verify                      # 要求完整性通过且 schema 为当前版本
python cli.py raw-verify                     # 校验采集原文 blob
python cli.py evidence-verify                # 另外校验 NLP 输入/响应/输出与日报证据
python cli.py jobs-status                    # 持久任务开关与各状态数量
python cli.py dataset-status                 # 数据集身份、epoch 与变化高水位
python cli.py legacy-backfill 250            # maintenance 下可续跑迁移历史记录
```

- `file_sha256` 只是指定 `.db` 文件的校验值；运行中的 WAL 数据库还可能有 `-wal` 内容，不能仅凭主文件
  校验值代表整个实时数据集。
- `db-bundle-backup` 建立快照、复制快照实际引用的 CAS 文件、写入清单并在发布目录前校验；转移到另一台
  机器后执行 `db-bundle-verify`。`db-bundle-restore` 只接受不存在的新目录，不会覆盖运行库、修改环境变量
  或启动服务。`db-bundle-smoke` 把数据库再复制到临时位置，逐项输出页面 HTTP 状态；真正切换后仍须单独
  验证 worker 与 `/api/ready`。
- 操作前先暂停 worker，避免备份期间与 blob 清理竞争；命令不会自动停止服务。
- 恢复旧备份后，只有在 worker 已停止且租约不再存活时，才可执行
  `python cli.py dataset-new-epoch EXPECTED_EPOCH REASON`；普通重启与同一最新备份恢复不切换 epoch。
  原理见 [发布账本说明](PUBLICATION_LEDGER.md)，证据与恢复细节见 [采集证据说明](INGEST_EVIDENCE.md)。

## 4. 采集、精选与 AI 策展

- 失败的源按基础间隔指数退避，上限 6 小时（基础间隔本身超过 6 小时的源保持基础间隔）；部分公司失败
  记为 partial，健康页同时显示部分失败和长期未更新。
- `INFOHUB_CURATED_FEED_ENABLED=true` 时，首页“精选”按事件去重，只展示 AI 评分 ≥70、官方来源，或所属
  事件至少有两个来源报道的条目；其余条目仍可在“全部动态”查看。该开关不授予 web 调用模型的权限。
- 模型通过 `.env` 中的 `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY` 配置（任何 OpenAI 兼容接口）。启用后
  翻译英文摘要、0-100 重要性评分、股市事件类型标注，并每天 08:00（`REPORT_HOUR/REPORT_MINUTE`）生成
  前一日日报；AI 批处理默认每 15 分钟一轮。清空模型配置即退回纯聚合。

## 5. 主题与事件维护

编辑 `config/topics.yaml` 调整主题规则；关注公司自动转为公司主题。

```bash
.venv/bin/python cli.py reindex   # 本地增量更新，不抓取外网、不调用 LLM
```

稳定事件、关系、修订、分析和报告的影子数据模型尚未替换门户读取的旧 `stories`；各自边界见
[事件候选](EVENT_CANDIDATES.md)、[事件关系](EVENT_RELATIONS.md)、[事件拆分与撤回](EVENT_TRANSITIONS.md)、
[事件事实修订](EVENT_REVISIONS.md)、[分析输入](ANALYSIS_INPUTS.md)、[分析调用审计](ANALYSIS_ATTEMPTS.md)、
[分析结果发布](ANALYSIS_RESULTS.md)、[来源时间](SOURCE_TIME.md)、[文档版本](DOCUMENT_VERSIONS.md)、
[历史回填](LEGACY_BACKFILL.md)、[身份目录](IDENTITY_CATALOG.md)、[SEC 身份](SEC_IDENTITY.md)。

主题抽样的人工复核使用 [本地审核工作台](TOPIC_REVIEW_CONSOLE.md)：只以 maintenance 角色绑定
`127.0.0.1`，不会把写入口挂到生产门户。
