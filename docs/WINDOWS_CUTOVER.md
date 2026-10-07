# Windows 生产升级手册

生产主机：Windows 常开笔记本 `windows-server`（Tailscale：`windows-server.tail29d4dd.ts.net`），项目目录
`C:\Users\serveradmin\Server\infohub`，部署器 `C:\Users\serveradmin\Server\manager`
（[windows-server-manager](https://github.com/jiangbosong123-netizen/windows-server-manager)）。步骤与耗时来自
[生产升级方案](CUTOVER_PLAN.md) 的完整演练；以下命令都在 Windows 的 PowerShell 中、`infohub` 目录下执行。

## 0. 先知道会发生什么

- 部署器的“自动部署”一旦开启，会在 `serveradmin` 登录时自启，**启动即检查一次**，之后每 5 分钟检查 `main`，
  有新提交就 `git pull --ff-only` 并 `docker compose up -d --build`。它不检查 CI，也没有暂停开关。
- 新版本生产模式**必须**在 `.env` 中有 `INFOHUB_PUBLIC_ORIGIN`（私网 HTTPS 地址）。缺少时 `migrate` 容器报错，
  web 与 worker 都不会启动。
- 新版本只在 `127.0.0.1:8000` 监听，旧地址 `http://100.69.211.16:8000/` 会失效，改用
  `https://windows-server.tail29d4dd.ts.net/`（Tailscale Serve，见第 5 步）。
- Tailscale 显示这台机器最近在线到 2026-10-05 00:08 UTC。如果当时自动部署开着，它**可能已经自动升级过**，
  所以第 1 步先看日志。

## 1. 安全开机，先关掉自动部署

1. 开机后**先断网**（关 Wi-Fi / 拔网线），再登录 `serveradmin`。断网时自动部署拉不到代码，只会记录 fetch 失败。
2. 打开桌面 **Windows Server Manager**，选 **9** 关闭自动部署。确认启动项已删除：
   ```powershell
   Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Windows Server Auto Deploy.lnk"   # 应为 False
   ```
3. 看自动部署是否已经做过什么：
   ```powershell
   Get-Content ..\manager\logs\auto-deploy.log -Tail 30
   ```
   出现 `[infohub] deployed <版本>` 说明已经自动升级过，先跳到第 9 节；只有 `fetch failed`、`skipped` 或没有
   infohub 记录，说明还没升级。
4. 重新联网。

## 2. 笔记本常开的电源设置（插电时不睡眠、合盖不睡眠）

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

保持插电。用电池时仍会按原设置睡眠，睡眠期间不采集；每天早上的对账只能补回部分遗漏。

## 3. 只读检查，决定用哪份数据

```powershell
git fetch origin
git rev-parse HEAD; git status --short; docker compose ps
cmd /c "git show origin/main:deploy/windows/inspect_host.py > %TEMP%\inspect_host.py"
docker run --rm -v "${PWD}\data:/data:ro" -v "${PWD}\.env:/env/.env:ro" -v "$env:TEMP\inspect_host.py:/inspect_host.py:ro" python:3.12-slim python /inspect_host.py
```

用 `cmd /c` 写文件是因为 PowerShell 5.1 的 `>` 会存成 UTF-16，Python 读不了。`git fetch` 不改动工作区；检查脚本只读，只输出 `.env` 的**键名**，不输出任何值。把输出发给 Claude，对照本机
Mac 旧库（89,575 条，2026-09-11 至 2026-10-03）决定：

- **用 Windows 的数据**：直接进入第 4 步。
- **用 Mac 的数据**：Mac 上生成一致性副本并通过 Tailscale 发送（Claude 可以准备），Windows 上接收后放入
  `data\app.db`（第 4 步之后、第 6 步之前，把 Windows 原有 `data` 整个保留为归档）。两份数据不能合并。

### 3b. 存储实测（回答决策 U02，约 1 分钟，可与第 3 步一起做）

数据库现在放在 Windows 目录挂载进容器（`.\data`），web 与 worker 两个容器同时读写。下面分别在同类挂载目录和
Docker 命名卷上测：小文件 fsync、SQLite 逐笔提交、顺序读写，以及两个进程同时写同一个 WAL 库是否丢失更新。
脚本只在新建的临时子目录里写入、结束即删除，不碰 `data`，不联网：

```powershell
cmd /c "git show origin/main:deploy/windows/probe_storage.py > %TEMP%\probe_storage.py"
mkdir probe-tmp
docker run --rm -v "${PWD}\probe-tmp:/probe" -v "$env:TEMP\probe_storage.py:/probe_storage.py:ro" python:3.12-slim python /probe_storage.py /probe
docker volume create infohub-probe
docker run --rm -v infohub-probe:/probe -v "$env:TEMP\probe_storage.py:/probe_storage.py:ro" python:3.12-slim python /probe_storage.py /probe
docker volume rm infohub-probe
Remove-Item probe-tmp
```

把两段输出一起发给 Claude。任一结果的 `verdict` 为 `UNSAFE` 时，该位置不能放运行库。是否把数据搬到命名卷仍按
`docs/spec/OPERATIONS.md` 另开搬迁 PR，不在本次升级中顺手切换。

## 4. 升级前完整备份

```powershell
docker compose down
$stamp = Get-Date -Format yyyyMMdd-HHmm
Copy-Item -Recurse data "data-before-upgrade-$stamp"
```

`down` 不带 `-v`，不会删除数据。容器停止后复制整个 `data` 目录（数据库与证据文件）是一致的。迁移时应用还会
再自动做一次一致性备份到 `data\backups\`。

## 5. 私网 HTTPS 与配置

按 [私网 HTTPS 生产入口](PRIVATE_HTTPS_INGRESS.md) 第 2–4 步执行 `tailscale serve --bg 8000`，然后在 `.env`
加入（不带结尾斜杠）：

```text
INFOHUB_PUBLIC_ORIGIN=https://windows-server.tail29d4dd.ts.net
```

以 Serve 实际输出的地址为准。不要启用 Funnel，不要开放 8000 端口。

建议（所有者已选 Healthchecks.io + 邮件，见 [D25](spec/DECISIONS.md)）：在 healthchecks.io 建一个推送检查（周期 5 分钟、
宽限约 20 分钟，通知方式选邮件），把推送地址作为 `INFOHUB_EXTERNAL_HEARTBEAT_URL=https://...` 加进同一个 `.env`。笔记本关机、睡眠或断网时由外部服务通知（见
`docs/WORKER_OPERATIONS.md`）。

## 6. 升级（停机约 4–5 分钟）

```powershell
git pull --ff-only origin main
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose up -d --build
docker compose logs -f migrate      # 首次需要把旧聚类全部重算，演练约 5 分钟（现在预计约 4 分钟）；看到“发布准备完成”后 Ctrl-C
docker compose ps                   # infohub 与 infohub-worker 应为 healthy
curl.exe -s http://127.0.0.1:8000/api/ready
curl.exe -s http://127.0.0.1:8000/api/health   # runtime.sqlite_version 应为 3.53.4，sqlite_wal_reset_safe 为 true
```

镜像自带从 SQLite 官网下载、核对 SHA3-256 后编译的 3.53.4。Debian 自带的 3.46.1 有 WAL 并发写入可能损坏数据库的已知
问题（3.51.3 修复，见 `docs/WORKER_OPERATIONS.md`）；生产环境如果加载到受影响的版本会拒绝启动。

数据库变大后，web 与 worker 每次启动都会先完整校验数据库才开始响应（4.9 GB 的库在 Mac 上约 0.5–1.5 分钟，视文件
缓存冷热；Windows 的 Docker 挂载目录读写更慢，可能要几分钟）。健康检查为此留了 10 分钟启动宽限，期间 `docker compose ps`
显示 `starting` 属正常。

## 7. 补历史数据（门户可用，Mac 同机实测约 25 分钟；演练时的旧版本为 3.5–4 小时，Windows 可能更慢）

回填期间建议暂停采集，避免与 worker 争用数据库写锁；如果更在意采集连续，也可以不停（会变慢但不会出错）。

```powershell
docker compose stop worker
docker compose run --rm migrate python cli.py legacy-backfill 1000      # 约 3 分钟
docker compose run --rm migrate python cli.py legacy-topic-backfill 1000
docker compose run --rm migrate python cli.py legacy-event-project
docker compose run --rm migrate python cli.py legacy-curation-run       # 约 20 分钟（含排队）；中断后重新运行即可续跑
docker compose run --rm migrate python cli.py projection-builders-run   # 约 3 分钟
docker compose start worker
```

`legacy-curation-run` 结束时若报告 `remaining` 不为空（等待重试的任务），过几分钟再运行一次。

## 8. 审计与备份包

```powershell
foreach ($c in "db-verify","raw-verify","evidence-verify","db-curation-audit","db-projection-audit","db-event-audit","db-coverage") {
  docker compose run --rm migrate python cli.py $c
}
docker compose run --rm migrate python cli.py db-bundle-backup
```

全部应为 `ok`。之后再按 [生产升级方案](CUTOVER_PLAN.md) 逐个开启新的读取开关（修改 `.env` 后
`docker compose up -d`）；主题读取需要先完成主题抽样复核与准入，否则会按设计返回 503。

## 9. 如果已经被自动升级过

```powershell
docker compose ps
docker compose logs --tail 60 migrate
```

- `migrate` 报 `INFOHUB_PUBLIC_ORIGIN is required in production`：web/worker 没有启动。完成第 2、5 步后运行
  `docker compose up -d`，再从第 6 步的检查继续。
- `migrate` 成功、服务 healthy：迁移已完成，应用在迁移前已自动备份到 `data\backups\`。完成第 2、5 步，再执行第 7、8 步。
- 其他错误：不要反复重试，保留日志发给 Claude。

## 10. 之后的自动部署

建议在部署器的带 CI 检查与回滚的版本（`codex/deployment-state-machine` 分支）审核合并之前，保持自动部署关闭，
由所有者确认后手动更新：`git pull --ff-only origin main` → 第 6 步。

## 11. 回退

```powershell
docker compose down
Rename-Item data "data-failed-$(Get-Date -Format yyyyMMdd-HHmm)"
Copy-Item -Recurse "data-before-upgrade-<时间>" data
git checkout <升级前的提交号>
docker compose up -d --build
tailscale serve off      # 如果回到不支持私网 HTTPS 的旧版本
```

升级前的提交号就是第 3 步 `git rev-parse HEAD` 的输出。
