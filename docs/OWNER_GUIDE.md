# 所有者手册

写给系统的主人（不需要会编程）：平时看什么、收到告警邮件怎么办、哪些事千万别做。具体命令都在 Windows
上、仓库目录里的 PowerShell 执行。更细的原理见文末链接。

## 1. 现在的状态

2026-10-14 晚 Windows 已升级到新版本，用的是 Mac 的旧库（89,575 条，2026-09-11 至 10-03；Windows 原来的库整个保留在
`data-windows-20261014`，两份不合并）。网站地址 `https://windows-server.<tailnet>.ts.net`（<tailnet> 以 Tailscale 管理页为准），只有登录了你的 Tailscale 的设备能打开。
还剩：

1. 把数据搬进 Docker 命名卷（[Windows 升级指南](WINDOWS_CUTOVER.md) 第 6b 步）；
2. 补历史数据、审计和备份包（第 7、8 步），以及一次隔离恢复演练；
3. 注册 Healthchecks.io（免费），建一个推送检查（周期 5 分钟、宽限 20 分钟、通知选邮件），把推送地址填进 `.env` 的
   `INFOHUB_EXTERNAL_HEARTBEAT_URL`（这个地址等同密码，不要发给别人）。

## 2. 系统由什么组成

| 部分 | 作用 |
|---|---|
| `infohub` 容器 | 网站（只读），通过 Tailscale 私网 HTTPS 访问 |
| `worker` 容器 | 唯一的后台：定时抓取、AI 评分翻译、日报、夜间备份、告警自检 |
| `migrate` 容器 | 每次升级时运行一次：先备份再升级数据库，成功后退出 |
| Docker 卷 `infohub-data` | 下面这些都在卷里（容器里的路径 `/app/data`），Windows 资源管理器里看不到 |
| `app.db` | 数据库 |
| `blobs/` | 原始证据文件（只增不删） |
| `backups/nightly/` | 夜间自动备份，保留最新 `BACKUP_KEEP` 份（这台机器 `.env` 设为 2，因为 C 盘只剩约 30 GB），每份约 5 GB |
| `backups/pre-migration/` | 每次升级前的数据库副本，保留最新 2 份 |
| `replaced/` | 从备份恢复时被换下来的旧库，确认无误后可手工删除 |
| `host-disk` 文件夹 | 项目下的空文件夹，只读挂进容器，用来看 C 盘还剩多少空间 |
| `.env` | 模型密钥、心跳地址等私密配置，只有 worker 读取 |

查看卷里的文件：`docker compose run --rm --no-deps migrate ls -la /app/data /app/data/backups/nightly`。

## 3. 每次打开 PowerShell，先运行这两行

```powershell
cd C:\Users\serveradmin\Server\infohub
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
```

第二行告诉 Docker 现在运行的是哪个版本。不运行的话，`docker compose` 会直接报错并提示你运行它，而不会悄悄
建出一个“unknown”版本（那样网站和后台会不一致，网站显示正常但后台其实没有就绪）。下面所有 `docker compose`
命令都假定已经运行过这两行；换了新窗口就再运行一次。

## 4. 平时看什么

- **没收到告警邮件就是正常。** 系统每 5 分钟向 Healthchecks 报一次平安；有问题会立刻发邮件，问题解决后再发一封“恢复”。
- 想看一眼：浏览器打开 `https://<你的机器名>.<tailnet>.ts.net/health`，红色的是有问题的来源。
- 侧栏的小脸是过去 24 小时新闻的“情绪”：AI 给每条新闻判一个利好/利空（-2 到 2），按重要性加权平均，
  从大笑到哭共五档；不到 20 条有判断时它在睡觉。鼠标放上去能看到利好、利空各多少条和最利好、最利空的那条，
  点它会跳一下说句话。这是没有人工复核的粗略判断，只图一乐，不要据此交易。页面开着时它每 5 分钟自己更新一次，
  心情变了会跳一下告诉你；首页有新内容时顶部会出现“有 N 条新动态，点击刷新”。
- 每周一次（可选）：`docker compose ps`，`infohub` 和 `infohub-worker` 应为 `healthy`（`migrate` 升级完就退出，不在列表里是正常的）。

### 晚上关机可以吗

可以。早上开机后**只需要输入密码登录 Windows**，Docker、网站和采集会自己启动（几分钟），不用输入命令。
Docker Desktop 设置里 General → “Start Docker Desktop when you sign in to your computer” 要保持勾选。

- **夜里的新闻大多能补回来**（按 2026-09-22 至 10-01 的数据，夜里 8 小时）：新浪 7×24 约 400 条、财联社约 130 条、华尔街见闻约 90 条，
  开机后往前翻页全部补回（新浪一次最多约 1,000 条）；Google News 各公司每晚 25–40 条，SEC 与港交所公告也能补回。
  IT 之家每晚约 90 条，它的订阅只保留最近几十条，会漏一部分。
- **“精选”要等 AI 打完分**：开机后积压的新闻一轮接一轮处理，大约半小时补齐。
- **对账、日报、备份**按北京时间定时（`APP_TZ`），关机时错过的开机后补跑一次。
- **掉线提醒**：Healthchecks 的检查改用 “Cron” 计划，例如 `*/5 8-23 * * *`、时区选你所在的时区、宽限 20 分钟，
  它就只在 8 点到午夜之间要求心跳，夜里关机不会发“掉线”邮件。开机晚于开始时间时会收到一封，可以按作息调整。
- 风扇吵：ROG 笔记本在 Armoury Crate（奥创中心）里把操作模式设为“静音”。这个系统负载很轻，静音模式够用。

## 5. 收到告警邮件怎么办

邮件正文第一段是代码和原因。先运行下面这条看现在的情况（有问题时会列出来）：

```powershell
docker compose exec worker python cli.py self-check
```

| 邮件里的代码 | 意思 | 先做什么 |
|---|---|---|
| （没有代码，只说 down） | 机器关机、睡眠、断网，或 worker 卡住 | 开机、联网；`docker compose ps`；还不行就 `docker compose restart worker` |
| `sources_failing` | 超过一半的信息源最近一次抓取失败 | 多半是断网或代理问题：检查网络；`/health` 页面能看到具体哪些源 |
| `ai_off` | worker 没有模型配置 | 检查 `.env` 里 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`，改完 `docker compose up -d worker` |
| `ai_stalled` | 1–6 小时前的新闻大多还没评分 | 模型密钥失效、额度用完或服务故障：`docker compose logs --tail 200 worker` 看报错 |
| `jobs_failed` | 24 小时内有后台任务重试用尽 | `docker compose logs --tail 200 worker`；偶发一次可以忽略，第二天还在就把日志发给 Claude |
| `job_stuck` | 某个后台任务（如 AI、备份）运行超过 2 小时还没结束；采集在单独的通道里照常进行 | `docker compose restart worker`（会把手上的任务做完或在 3 分钟后强停，租约过期后任务自动重试）；之后仍反复出现就把 `docker compose logs --tail 200 worker` 发给 Claude |
| `backup_stale` | 超过 36 小时没有成功的夜间备份 | `docker compose run --rm --no-deps migrate ls -la /app/data/backups/nightly`；常见原因是磁盘空间不够（见下一行） |
| `disk_low` | C 盘（或 Docker 磁盘）剩余空间低于 10 GB | 先删项目下确认无用的 `data-before-*`、`data-windows-*` 文件夹，再 `docker image prune` 删旧版本镜像；仍不够就把输出发给 Claude。不要删卷里的 `app.db`、`blobs/` |
| `offsite_stale` | Mac 上的异地备份超过 7 天没更新 | 让 Mac 开机、连上 Tailscale；看 `~/InfohubBackups/status.json` 里的 `result`（`unreachable` 是 Windows 不在线，`failed` 把文件发给 Claude） |
| `self_check_error` | 自检本身出错 | 把 `docker compose logs --tail 200 worker` 发给 Claude |

不确定时：不要删除任何东西，把 `docker compose ps` 和 `docker compose logs --tail 200 worker` 的输出发给 Claude。

## 6. 升级、回退与恢复

- **升级**：在 PowerShell 里粘贴这一行（每次都是同一行，顺序已经写在里面）：

  ```powershell
  cd ~\Server\infohub; git pull; .\update.cmd
  ```

  它依次拉取新代码、构建并启动（数据库要升级时先自动备份）、等网站和后台都报出新版本号。最后一行是绿色的
  `DONE: InfoHub is running <版本号>` 就是成功；红色的 `STOPPED: ...` 说明停在了哪一步，之后的步骤没有执行，
  把窗口拍给 Claude。细节见 [Windows 升级指南](WINDOWS_CUTOVER.md) 第 6 步。
- **升级后有问题，回到上一版**：同一指南第 11 步。
- **数据坏了或误操作，用夜间备份恢复**：[运维手册](RUNBOOK.md)“从备份恢复到线上”，照着那几条命令做；当前库会被移到
  卷里的 `replaced/`，不会被删除。
- **上线稳定后打开正文补全**：在 `.env` 加一行 `INFOHUB_ARTICLE_TEXT_ENABLED=true`，再运行
  `docker compose up -d worker`。之后只有标题的新闻（主要是 Google News）会自动补正文，约六成能补上；
  想关掉就改回 `false` 再运行同一条命令。

## 7. 千万不要做的事

- **不要让两台机器同时采集**，也**不要把两份数据库合并**（Mac 旧库和 Windows 库只能二选一）。
- **不要在 Docker Desktop 里点 “Reset to factory defaults” 或 “Clean / Purge data”，不要卸载 Docker Desktop**，不要运行
  `docker volume rm infohub-data`、`docker volume prune`、`docker system prune --volumes`：数据都在 Docker 卷里，这些操作会把它
  全部删掉。（`docker compose down -v` 删不掉它，因为它是外部卷，但也不要用。）
- 不要开启 Tailscale Funnel，不要在防火墙或路由器上开放 8000 端口。
- 不要把 `.env`、心跳地址或 API key 发到聊天、截图或公开仓库里。
- Windows 上的备份都在同一块硬盘（而且在同一个 Docker 卷）里，只防误操作；防硬盘损坏或 Docker 被重置靠 Mac 上的异地备份
  （`~/InfohubBackups`，约 5 GB，每天自动更新，开启方法见[运维手册](RUNBOOK.md)“异地备份到 Mac”）。不要删掉这个文件夹。

## 8. 需要你保管的东西

- `.env`（模型密钥、心跳地址）；
- Tailscale 账号（私网访问）；
- Healthchecks.io 账号（告警邮件）。

## 更多

[运维手册](RUNBOOK.md) · [后台与健康信号](WORKER_OPERATIONS.md) · [Windows 升级指南](WINDOWS_CUTOVER.md) ·
[生产升级方案](CUTOVER_PLAN.md) · [决策记录](spec/DECISIONS.md)
