# Windows 生产升级手册

生产主机：Windows 常开笔记本 `windows-server`（Tailscale：`windows-server.<tailnet>.ts.net`，以 Tailscale 管理页显示为准），项目目录
`C:\Users\serveradmin\Server\infohub`，部署器 `C:\Users\serveradmin\Server\manger`（文件夹名就是 `manger`，少一个 a）
（[windows-server-manager](https://github.com/jiangbosong123-netizen/windows-server-manager)）。步骤与耗时来自
[生产升级方案](CUTOVER_PLAN.md) 的完整演练；以下命令都在 Windows 的 PowerShell 中、`infohub` 目录下执行。

## 0. 先知道会发生什么

- 部署器的“自动部署”一旦开启，会在 `serveradmin` 登录时自启，**启动即检查一次**，之后每 5 分钟检查 `main`，
  有新提交就 `git pull --ff-only` 并 `docker compose up -d --build`。它不检查 CI，也没有暂停开关。
- 升级到新版本后，每个新打开的 PowerShell 窗口在运行任何 `docker compose` 命令前都要先运行
  `$env:APP_VERSION = (git rev-parse --short=12 HEAD)`；没有它 compose 会报错拒绝运行（防止建出版本为 unknown 的镜像）。
- 2026-10-15 起数据（数据库、证据文件、备份、心跳）在 Docker 命名卷 `infohub-data` 里（[D29](spec/DECISIONS.md)，第 6b 步），
  不再是项目下的 `data` 文件夹。**不要**在 Docker Desktop 里点 “Reset to factory defaults” 或 “Clean / Purge data”，不要运行
  `docker volume rm infohub-data`、`docker volume prune` 或 `docker system prune --volumes`：这些会删掉卷里的全部数据。compose
  把它声明为外部卷：卷不存在时直接报错，而不是用一个空数据库开始采集。
- 新版本生产模式**必须**在 `.env` 中有 `INFOHUB_PUBLIC_ORIGIN`（私网 HTTPS 地址）。缺少时 `migrate` 容器报错，
  web 与 worker 都不会启动。
- 新版本只在 `127.0.0.1:8000` 监听，旧地址 `http://<Windows 的 Tailscale IP>:8000/` 会失效，改用
  `https://windows-server.<tailnet>.ts.net/`（Tailscale Serve，见第 5 步）。
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
   Get-Content ..\manger\logs\auto-deploy.log -Tail 30
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

最省事的做法是一次跑完第 3 步和 3b（结果同时存到桌面的 `infohub-inspect.txt`）：

```powershell
cd ~\Server\infohub
git fetch origin
git show origin/main:deploy/windows/inspect.ps1 > $env:TEMP\inspect.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\inspect.ps1
```

也可以按下面的命令逐条执行：

```powershell
git fetch origin
git rev-parse HEAD; git status --short; docker compose ps
cmd /c "git show origin/main:deploy/windows/inspect_host.py > %TEMP%\inspect_host.py"
docker run --rm --network none -v "${PWD}\data:/data:ro" -v "${PWD}\.env:/env/.env:ro" -v "$env:TEMP\inspect_host.py:/inspect_host.py:ro" python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 python /inspect_host.py
```

用 `cmd /c` 写文件是因为 PowerShell 5.1 的 `>` 会存成 UTF-16，Python 读不了。`git fetch` 不改动工作区；检查脚本只读，只输出 `.env` 的**键名**，不输出任何值。把输出发给 Claude，对照本机
Mac 旧库（89,575 条，2026-09-11 至 2026-10-03）决定：

- **用 Windows 的数据**：直接进入第 4 步。
- **用 Mac 的数据**：Mac 上生成一致性副本并通过 Tailscale 发送（Claude 可以准备），Windows 上接收后放入
  `data\app.db`（第 4 步之后、第 6 步之前，把 Windows 原有 `data` 整个保留为归档；第 6b 步再把 `data` 整个搬进命名卷）。
  两份数据不能合并。

### 3b. 存储实测（回答决策 U02，约 1 分钟，可与第 3 步一起做）

数据库现在放在 Windows 目录挂载进容器（`.\data`），web 与 worker 两个容器同时读写。下面分别在同类挂载目录和
Docker 命名卷上测：小文件 fsync、SQLite 逐笔提交、顺序读写、随机 4 KB 读取（SQLite 读取未缓存页面的方式），
两个进程同时写同一个 WAL 库是否丢失更新，以及容器在该位置看到的磁盘总量与剩余空间（命名卷看到的是 Docker 的虚拟磁盘，
不是 C 盘）。脚本只在新建的临时子目录里写入、结束即删除，不碰 `data`，不联网：

```powershell
cmd /c "git show origin/main:deploy/windows/probe_storage.py > %TEMP%\probe_storage.py"
mkdir probe-tmp
docker run --rm -v "${PWD}\probe-tmp:/probe" -v "$env:TEMP\probe_storage.py:/probe_storage.py:ro" python:3.12-slim python /probe_storage.py /probe
docker volume create infohub-probe
docker run --rm -v infohub-probe:/probe -v "$env:TEMP\probe_storage.py:/probe_storage.py:ro" python:3.12-slim python /probe_storage.py /probe
docker volume rm infohub-probe
Remove-Item probe-tmp
```

把两段输出一起发给 Claude。任一结果的 `verdict` 为 `UNSAFE` 时，该位置不能放运行库。2026-10-14 的实测和升级当晚的启动
耗时见 [D29](spec/DECISIONS.md)：数据在升级之后单独搬进命名卷（第 6b 步），不和数据库升级一起做。

## 4. 升级前完整备份

第一次升级时数据还在 `data` 文件夹：

```powershell
docker compose down
$stamp = Get-Date -Format yyyyMMdd-HHmm
Copy-Item -Recurse data "data-before-upgrade-$stamp"
```

`down` 不带 `-v`，不会删除数据。容器停止后复制整个 `data` 目录（数据库与证据文件）是一致的。

数据在命名卷之后（第 6b 步以后的每次升级），在线做一个完整备份包（约 5 GB，写在卷里的 `backups/`，夜间清理不会删它）：

```powershell
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose run --rm --no-deps migrate python cli.py db-bundle-backup
```

两种情况下，迁移时应用都还会再自动做一次一致性备份到 `backups/pre-migration/`。

## 5. 私网 HTTPS 与配置

按 [私网 HTTPS 生产入口](PRIVATE_HTTPS_INGRESS.md) 第 2–4 步执行 `tailscale serve --bg 8000`，然后在 `.env`
加入（不带结尾斜杠）：

```text
INFOHUB_PUBLIC_ORIGIN=https://windows-server.<tailnet>.ts.net
```

以 Serve 实际输出的地址为准。不要启用 Funnel，不要开放 8000 端口。

建议（所有者已选 Healthchecks.io + 邮件，见 [D25](spec/DECISIONS.md)）：在 healthchecks.io 建一个推送检查（周期 5 分钟、
宽限约 20 分钟，通知方式选邮件；晚上关机的话改用 Cron 计划，见[所有者手册](OWNER_GUIDE.md)“晚上关机可以吗”），把推送地址作为 `INFOHUB_EXTERNAL_HEARTBEAT_URL=https://...` 加进同一个 `.env`。笔记本关机、睡眠或断网时由外部服务通知；
系统还在跑但抓取大面积失败、AI 停摆、后台任务失败、备份过期或磁盘快满时，也会立刻收到邮件，正文写明原因（见
`docs/WORKER_OPERATIONS.md` 的“告警自检”）。

## 6. 升级（停机约 4–5 分钟）

第一次升级（2026-10-14）时数据还在 `data` 文件夹；之后的版本要求命名卷 `infohub-data`，数据还在文件夹里时先做第 6b 步。

日常升级只用一行（`deploy/windows/update.ps1`：拉取，拉取没到 GitHub main 就停下；以新版本号构建启动；等 `/api/ready`
里网站和 worker 都报出新版本号，最多 15 分钟）：

```powershell
cd ~\Server\infohub; git pull; powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1
```

2026-10-15 分三行输入时，两次把 `docker compose up` 放在了 `git pull` 前面，重启后仍是旧版本，所以改成一个脚本。
下面是脚本做的事，排查时可以逐行运行：

```powershell
git pull --ff-only origin main
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose up -d --build
docker compose logs -f migrate      # 首次需要把旧聚类全部重算，Mac 演练 290 秒（2026-10-14）；看到“发布准备完成”后 Ctrl-C
docker compose ps                   # infohub 与 infohub-worker 应为 healthy
curl.exe -s http://127.0.0.1:8000/api/ready
curl.exe -s http://127.0.0.1:8000/api/health   # runtime.sqlite_version 应为 3.53.4，sqlite_wal_reset_safe 为 true
```

镜像自带从 SQLite 官网下载、核对 SHA3-256 后编译的 3.53.4。Debian 自带的 3.46.1 有 WAL 并发写入可能损坏数据库的已知
问题（3.51.3 修复，见 `docs/WORKER_OPERATIONS.md`）；生产环境如果加载到受影响的版本会拒绝启动。

数据库变大后，web 与 worker 每次启动都会先完整校验数据库才开始响应（4.9 GB 的库在 Mac 上约 0.5–1.5 分钟，视文件
缓存冷热；Windows 的 Docker 挂载目录读写更慢，可能要几分钟）。健康检查为此留了 10 分钟启动宽限，期间 `docker compose ps`
显示 `starting` 属正常。

## 6b. 把数据搬进 Docker 命名卷（只做一次，[D29](spec/DECISIONS.md)）

2026-10-14 升级后发现：数据在 Windows 文件夹挂载里时，每次启动前的完整数据库检查极慢（同一数据第二次启动，Mac 1.7 秒，
Windows 37 分钟仍未结束，CPU 约 5%，都在等磁盘），补完历史数据后库还会长到约 5 GB。命名卷在 Docker 自己的 Linux 磁盘里，
读写快几十倍。约 5–10 分钟，期间网站停止：

```powershell
cd ~\Server\infohub
git fetch origin
git show origin/main:deploy/windows/move-to-volume.ps1 > $env:TEMP\move-to-volume.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\move-to-volume.ps1
```

脚本依次：

1. 检查：`data\app.db` 存在、卷还不存在、C 盘空间够；
2. 停止 InfoHub（worker 最多等 3 分钟做完手上的任务）；
3. 新建卷 `infohub-data`，把 `data` 整个复制进去，逐个文件比对 SHA-256，并对复制后的数据库做完整性检查
   （`deploy/windows/copy_data.py`）；
4. 拉取最新代码（compose 改用卷）；
5. 把 `data` 改名为 `data-before-volume-<时间>` 保留；
6. 启动。

前四步任何一项不通过都会停下并说明原因，`data` 不变，旧方式仍可启动。如果第 3 步报 `not closed cleanly`，是被停下的任务（例如还在运行的 `prepare-release`）留下了未合并的 WAL：数据没有问题，把屏幕发给 Claude 处理后再运行。之后：

- 新窗口先运行 `$env:APP_VERSION = (git rev-parse --short=12 HEAD)`，再看 `docker compose ps`，两个服务应在几分钟内变为
  healthy。
- `data-before-volume-<时间>` 留到夜间备份包的隔离恢复演练通过后再删（2026-10-15 已通过，见第 8b 步；存储方式变更后必须做恢复演练，见
  `docs/spec/OPERATIONS.md` 3.2）。
- 查看卷里的文件：`docker compose run --rm --no-deps migrate ls -la /app/data /app/data/backups/nightly`。
- 项目下会多一个空的 `host-disk` 文件夹：容器只读挂载它，用来看 C 盘还剩多少空间（卷在容器里只显示 Docker 的虚拟磁盘）。

只在搬迁后出问题时回退到文件夹方式（搬迁之后新采集的内容只在卷里，回退会丢失这部分；卷本身不会被删除）：

```powershell
docker compose down
git checkout <搬迁前的提交号>
Rename-Item data-before-volume-<时间> data
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose up -d --build
```

## 7. 补历史数据（门户可用；10-16 的生产备份在 Mac 上约 6 分钟，另加需联网的 SEC 时间校正约 4 分钟，Windows 可能慢一倍）

第 7、8 步合成一行（`deploy/windows/backfill.ps1`：暂停 worker、依次运行下面的回填和第 8 步的审计，任何一步失败就停下；
无论成败最后都重新启动 worker；输出同时存到桌面的 `infohub-backfill.txt`）。旧 AI 评分记录的导入暂缓，见第 7b 步和 [D32](spec/DECISIONS.md)：

```powershell
cd ~\Server\infohub; git pull; powershell -ExecutionPolicy Bypass -File deploy\windows\backfill.ps1
```

最后一行绿色 `DONE` 是成功；红色 `STOPPED at: <步骤>` 说明停在哪一步，worker 已恢复，把窗口或桌面那个文件发给 Claude。
每一步都可以重跑，已经做完的会跳过。第 8 步的手动备份包脚本不做：主机常开后，当晚 03:30 的夜间备份就包含回填后的数据，
而手动包放在 `nightly/` 之外，不会被自动清理。下面是脚本做的事，排查时可以逐行运行。

回填期间建议暂停采集，避免与 worker 争用数据库写锁；如果更在意采集连续，也可以不停（会变慢但不会出错）。

```powershell
docker compose stop worker
docker compose run --rm migrate python cli.py legacy-backfill 1000      # 约 3 分钟
docker compose run --rm migrate python cli.py legacy-topic-backfill 1000
docker compose run --rm migrate python cli.py legacy-event-project
docker compose run --rm migrate python cli.py routine-filings-mark --apply   # 几秒；旧库的 SEC 内部人申报与港交所翌日披露報表不再进精选
docker compose run --rm migrate python cli.py sec-acceptance-times --apply   # 约 4 分钟，需联网；按 SEC 文件头改正旧 SEC 卡片时间（演练数据 641 份中 119 份早了 4 小时），中断后重新运行即可续跑
docker compose start worker
```

## 7b. 旧 AI 评分记录导入新分析结构（暂缓，[D32](spec/DECISIONS.md)）

只在要打开新精选读取（`INFOHUB_CURATION_READ_ENABLED` 等）之前做。10-16 的数据上会让数据库从约 1.6 GB 涨到约 5 GB，
所以先确认 C 盘空间和备份保留份数。

```powershell
docker compose stop worker
docker compose run --rm migrate python cli.py legacy-curation-run       # Mac 上 27 分钟（10-16 数据）；中断后重新运行即可续跑
docker compose run --rm migrate python cli.py projection-builders-run   # Mac 上约 4 分钟
docker compose run --rm migrate python cli.py db-curation-audit
docker compose run --rm migrate python cli.py db-projection-audit
docker compose start worker
```

`legacy-curation-run` 结束时若报告 `remaining` 不为空（等待重试的任务），过几分钟再运行一次。

## 8. 审计

```powershell
foreach ($c in "db-verify","raw-verify","evidence-verify","db-event-audit") {
  docker compose run --rm migrate python cli.py $c
}
```

逐条看输出：`db-verify` 的 `state` 应为 `current`、`integrity` 为 `ok`；各审计命令不应报错或列出问题；`db-coverage`
只是统计数字，没有对错。任何一条报错就停下，把输出发给 Claude。之后再按 [生产升级方案](CUTOVER_PLAN.md) 逐个开启新的读取开关（修改 `.env` 后
`docker compose up -d`）；主题读取需要先完成主题抽样复核与准入，否则会按设计返回 503。

## 8b. 异地备份到 Mac

按 [运维手册](RUNBOOK.md)“异地备份到 Mac”开启：Windows 上打开开关并签发只读备份的密钥，用 Tailscale 发给 Mac，
Mac 上运行 `deploy/macos/offsite-install.sh`。第一次拉到后在 Mac 上做隔离恢复演练，通过后才可以删除
`data-before-volume-<时间>`。（2026-10-15 已完成：第一份夜间备份包拉到 Mac，恢复约 10 秒，门户 8 项读检查全部通过，
见 `docs/evidence/offsite-restore-drill-20261015.json`。）

## 9. 如果已经被自动升级过

```powershell
docker compose ps
docker compose logs --tail 60 migrate
```

- `migrate` 报 `INFOHUB_PUBLIC_ORIGIN is required in production`：web/worker 没有启动。完成第 2、5 步后运行
  `$env:APP_VERSION = (git rev-parse --short=12 HEAD)` 和 `docker compose up -d`，再从第 6 步的检查继续。
- `migrate` 成功、服务 healthy：迁移已完成，应用在迁移前已自动备份到 `data\backups\pre-migration\`。完成第 2、5、6b 步，再执行第 7、8 步。
- 其他错误：不要反复重试，保留日志发给 Claude。

## 10. 之后的自动部署

建议在部署器的带 CI 检查与回滚的版本（`codex/deployment-state-machine` 分支）审核合并之前，保持自动部署关闭，
由所有者确认后手动更新：`git pull --ff-only origin main` → 第 6 步。

## 11. 回退

新版本改过数据库时，旧版本打不开新库，所以回到上一版要连同升级前的数据库一起换回。数据在命名卷里时：

```powershell
docker compose down
docker compose run --rm --no-deps migrate ls -la /app/data/backups/pre-migration   # 挑升级前那一份
docker compose run --rm --no-deps migrate sh -c "mkdir -p /app/data/replaced/<时间> && mv /app/data/app.db* /app/data/replaced/<时间>/ && cp -p /app/data/backups/pre-migration/<名称> /app/data/app.db"
git checkout <升级前的提交号>
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose up -d --build
```

换下来的库留在卷里的 `replaced/<时间>/`，确认无误后再删。数据还在 `data` 文件夹时（第一次升级），用第 4 步的副本：

```powershell
docker compose down
Rename-Item data "data-failed-$(Get-Date -Format yyyyMMdd-HHmm)"
Copy-Item -Recurse "data-before-upgrade-<时间>" data
git checkout <升级前的提交号>
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose up -d --build
tailscale serve off      # 如果回到不支持私网 HTTPS 的旧版本
```

升级前的提交号就是第 3 步 `git rev-parse HEAD` 的输出。
