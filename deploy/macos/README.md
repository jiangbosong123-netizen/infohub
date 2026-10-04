# macOS 单机部署套件（备选方案）

生产的默认目标仍是 Windows Docker Compose（见 [`docs/RUNBOOK.md`](../../docs/RUNBOOK.md)）。本目录提供
**另一个选项**：在这台 Mac 上用 launchd 运行同样的三种角色。是否启用、以及哪一份数据作为生产数据，由所有者
决定；**两台机器绝不能同时充当生产采集器**（SPEC §3 否决方案），脚本检测到 Tailscale 上的 `windows-server`
在线时会拒绝启动。

## 与 Compose 一一对应

| Compose | macOS |
|---|---|
| `migrate` 一次性容器（`prepare-release`） | `infohub-macos.sh migrate`：先停 web/worker，再以 maintenance 角色迁移，迁移前自动一致性备份 |
| `infohub` 只读门户 | launchd `com.infohub.production.web`：web 角色，禁止网络任务与调度，只绑定 `127.0.0.1` |
| `worker` 唯一后台 | launchd `com.infohub.production.worker`：唯一调度与抓取进程，**只有它读取模型与 SEC 配置** |
| `./data` 卷 | `~/infohub-production/data`（数据库、blob、备份、心跳；目录权限 700） |

目录布局：

```text
~/infohub-production/
  RELEASE            当前代码的 commit SHA（同时作为 APP_VERSION，web 与 worker 一致才 ready）
  code/              固定在该 SHA 的独立 git worktree + Python 3.12 .venv（不得含 .env）
  config/common.env  非密钥配置，三种角色共用（由模板渲染，开关默认值与 compose.yaml 相同）
  config/worker.env  只给 worker：LLM_*、SEC_USER_AGENT、抓取/对账/日报时间（白名单复制，权限 600）
  data/              app.db、blobs/、backups/、runtime/
  logs/              web/worker 的标准输出与错误
```

## 首次部署（从旧 Mac 数据升级）

以下耗时来自 2026-10-04 在本机旧库副本（89,575 条）上的完整演练：

| 步骤 | 命令 | 演练耗时 | 门户 | 采集 |
|---|---|---|---|---|
| 1 检查 | `infohub-macos.sh check` | 秒级 | — | — |
| 2 安装代码 | `infohub-macos.sh install-code`（默认 origin/main，要求 CI 全绿） | 约 1 分钟 | — | — |
| 3 配置 | `infohub-macos.sh configure /Users/jumbo/infohub/.env`（私网地址自动取本机 Tailscale MagicDNS 名，也可用 `INFOHUB_PUBLIC_ORIGIN` 指定） | 秒级 | — | — |
| 4 导入旧库 | `infohub-macos.sh import-legacy /Users/jumbo/infohub/data/app.db` | 秒级（SQLite backup，旧文件不动） | — | — |
| 5 迁移 | `infohub-macos.sh migrate` | 约 5 分钟（首次需把旧 `titles-v2` 聚类全部重算） | 停 | 停 |
| 6 启动 | `infohub-macos.sh start` | 1 分钟内 ready | 可用 | 恢复 |
| 7 历史回填 | `infohub-macos.sh backfill` | 约 20 分钟 | 可用 | 见下 |
| 8 旧 AI 策展导入等 | 见下文 | 约 2 小时 | 可用 | 见下 |

生产模式要求 `INFOHUB_PUBLIC_ORIGIN=https://<机器名>.<tailnet>.ts.net`。要从其他设备访问，还需由所有者手动开启
Tailscale Serve 把私网 HTTPS 转发到 `127.0.0.1:8000`（`tailscale serve --bg 8000`），不要开启 Funnel；脚本不会修改
任何网络配置。

步骤 7、8 只写新的版本化表，门户读取的旧投影不受影响，可以在 web 运行时进行。为避免与 worker 争用 SQLite
写锁，演练是在 worker 停止时执行的；正式执行时同样建议 `stop` 后只启动 web，回填完成再 `start`。步骤 8 使用
维护角色依次运行 `legacy-curation-enqueue` / `legacy-curation-process`（可续跑）、`curation-search-advance`、
`curation-hot-advance`，完成并通过审计后，才逐个把 `config/common.env` 里的读取开关改为 `true` 并重启 web。

## 日常

- 升级：`install-code <新 SHA>` → `migrate` → `start`。数据库只做兼容扩展，迁移前自动备份到 `data/backups/`。
- 状态：`infohub-macos.sh status`（等待 `/api/ready` 最多 180 秒，并输出 worker 心跳）。
- 停止：`infohub-macos.sh stop`。

## 回滚

1. `infohub-macos.sh stop`。
2. 代码回退：`install-code <上一个 SHA>`。新版本迁移过的数据库，旧代码可能拒绝打开未知 schema——这是设计上的保护。
3. 数据回退：把 `data/backups/` 中迁移前的备份复制为 `data/app.db`（先保存当前文件），再 `start`。回退旧备份后，
   若曾有新数据写入并对外同步过，按 [发布账本说明](../../docs/PUBLICATION_LEDGER.md) 处理 epoch。
4. 旧 `com.infohub.server` 已禁用；恢复它只适用于旧代码与旧 `data/app.db`：
   `launchctl enable gui/$(id -u)/com.infohub.server`。

## 测试方式

`DRY_RUN=1 infohub-macos.sh start` 只渲染并校验 launchd 文件（`launchd-preview/`），不加载。也可以用
`INFOHUB_PREFIX` / `INFOHUB_PORT` / `INFOHUB_LABEL_PREFIX` 指向独立测试目录，在不同端口完整演练。
