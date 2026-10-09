# 所有者手册

写给系统的主人（不需要会编程）：平时看什么、收到告警邮件怎么办、哪些事千万别做。具体命令都在 Windows
上、仓库目录里的 PowerShell 执行。更细的原理见文末链接。

## 1. 现在的状态

代码已经完成并用真实数据完整演练过，但**目前两台机器都没有在采集**：Mac 上的旧采集器 2026-10-04 起已停用，
Windows 还没有开机升级。上线前需要你做三件事：

1. 打开 Windows，按 [Windows 升级指南](WINDOWS_CUTOVER.md) 第 1–3 步做只读检查，把输出发给 Claude，再决定用哪份数据；
2. 注册 Healthchecks.io（免费），建一个推送检查（周期 5 分钟、宽限 20 分钟、通知选邮件），把推送地址填进 `.env` 的
   `INFOHUB_EXTERNAL_HEARTBEAT_URL`（这个地址等同密码，不要发给别人）；
3. 按升级指南第 4–8 步升级。

## 2. 系统由什么组成

| 部分 | 作用 |
|---|---|
| `infohub` 容器 | 网站（只读），通过 Tailscale 私网 HTTPS 访问 |
| `worker` 容器 | 唯一的后台：定时抓取、AI 评分翻译、日报、夜间备份、告警自检 |
| `migrate` 容器 | 每次升级时运行一次：先备份再升级数据库，成功后退出 |
| `data\app.db` | 数据库 |
| `data\blobs\` | 原始证据文件（只增不删） |
| `data\backups\nightly\` | 夜间自动备份，保留最新 3 份，每份约 5 GB |
| `data\backups\pre-migration\` | 每次升级前的数据库副本，保留最新 2 份 |
| `data\replaced\` | 从备份恢复时被换下来的旧库，确认无误后可手工删除 |
| `.env` | 模型密钥、心跳地址等私密配置，只有 worker 读取 |

## 3. 平时看什么

- **没收到告警邮件就是正常。** 系统每 5 分钟向 Healthchecks 报一次平安；有问题会立刻发邮件，问题解决后再发一封“恢复”。
- 想看一眼：浏览器打开 `https://<你的机器名>.<tailnet>.ts.net/health`，红色的是有问题的来源。
- 每周一次（可选）：`docker compose ps`，`infohub` 和 `infohub-worker` 应为 `healthy`（`migrate` 升级完就退出，不在列表里是正常的）。

## 4. 收到告警邮件怎么办

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
| `backup_stale` | 超过 36 小时没有成功的夜间备份 | `dir data\backups\nightly`；常见原因是磁盘空间不够（见下一行） |
| `disk_low` | 磁盘剩余空间低于 10 GB | 删除确认无用的 `data\replaced\` 和 `data-before-upgrade-*`；不要删 `data\app.db`、`data\blobs\` |
| `self_check_error` | 自检本身出错 | 把 `docker compose logs --tail 200 worker` 发给 Claude |

不确定时：不要删除任何东西，把 `docker compose ps` 和 `docker compose logs --tail 200 worker` 的输出发给 Claude。

## 5. 升级、回退与恢复

- **升级**：[Windows 升级指南](WINDOWS_CUTOVER.md) 第 6 步（停机几分钟，升级前自动备份）。
- **升级后有问题，回到上一版**：同一指南第 11 步。
- **数据坏了或误操作，用夜间备份恢复**：[运维手册](RUNBOOK.md)“从备份恢复到线上”，四条命令；当前库会被移到
  `data\replaced\`，不会被删除。

## 6. 千万不要做的事

- **不要让两台机器同时采集**，也**不要把两份数据库合并**（Mac 旧库和 Windows 库只能二选一）。
- 不要运行 `docker compose down -v`（`-v` 会删数据卷），不要手工删除 `data\app.db`、`data\blobs\`。
- 不要开启 Tailscale Funnel，不要在防火墙或路由器上开放 8000 端口。
- 不要把 `.env`、心跳地址或 API key 发到聊天、截图或公开仓库里。
- 备份只在同一块硬盘上，防误操作，不防硬盘损坏；异地备份（放到 Mac）会在 Windows 检查后再做。

## 7. 需要你保管的东西

- `.env`（模型密钥、心跳地址）；
- Tailscale 账号（私网访问）；
- Healthchecks.io 账号（告警邮件）。

## 更多

[运维手册](RUNBOOK.md) · [后台与健康信号](WORKER_OPERATIONS.md) · [Windows 升级指南](WINDOWS_CUTOVER.md) ·
[生产升级方案](CUTOVER_PLAN.md) · [决策记录](spec/DECISIONS.md)
