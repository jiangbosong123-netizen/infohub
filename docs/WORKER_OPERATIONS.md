# web / worker 运行与健康规范

## 进程职责

生产 Compose 有三个角色，共用同一只读代码镜像和同一个 Windows `data` 挂载：

| 服务 | 角色 | 允许做什么 | 不允许做什么 |
|---|---|---|---|
| `migrate` | maintenance | 安全迁移、同步公司/源配置、刷新派生索引、清除旧心跳；成功后退出 | 常驻调度、对外提供网页 |
| `infohub` | web | 验证当前 schema、读取 SQLite、提供门户和健康 API | 建库、迁移、抓取、调用模型、登记任务 |
| `worker` | worker | 登记持久计划、领取任务、续租、抓取、AI、对账、日报和清理 | 提供网页、绕过任务租约直接启动第二套调度 |

生产配置拒绝含糊角色：web 必须关闭网络与 scheduler；worker 必须同时开启网络、scheduler 和
durable jobs；maintenance 不得启动 scheduler。Mac 默认是离线 web。手动抓样本要显式使用
maintenance 角色，不能把 Mac 变成第二个生产 worker。

## 启动与发布顺序

1. `migrate` 执行 `cli.py prepare-release`。数据库迁移仍使用一致性备份、校验和事务；失败则
   Compose 不启动 web/worker。
2. 发布准备删除上一次的 `worker-heartbeat.json`，防止同 SHA 重试误认旧进程。
3. web 与 worker 在 migrate 成功后分别启动。web 只调用严格数据库验证，不再隐式修改数据。
4. worker 写入包含环境、构建 SHA、worker ID 和时间的原子心跳，并登记五类持久计划。
5. `/api/health` 只有在当前 web 版本对应的 worker 心跳新鲜且 dataset owner 匹配时返回 200。
6. Windows Server Manager 连续验收后，才把目标 SHA 记为健康版本。

旧版本 Compose 只有 `infohub` 单进程。发布管理器按旧 Compose 的实际服务列表保存镜像；若新
版本失败，重置旧检出并用 `--remove-orphans` 删除新 worker/migrate，再启动旧单进程镜像。
P05不增加数据库 schema，因此旧镜像仍可读；后续 schema PR 仍须逐项证明向后兼容。

## 持久计划与恢复

worker 维护以下 schedule：

- 到期来源抓取：按 `CRAWL_TICK_MINUTES` 扫描，具体来源仍遵守自己的间隔和失败退避；
- AI 策展：默认每 15 分钟；没有模型配置时安全完成空轮次，派生索引仍会刷新；
- Google News 对账：每天配置时间；
- 日报：每天配置时间；
- `fetch_log` 清理：每天 04:05，只保留 14 天运行日志。

schedule 的下一次时间保存在 SQLite。进程停机期间错过多个周期时只合并为一个到期 job。job
领取后有带 token 的租约，心跳线程持续续租；worker 被杀死后租约到期，新进程会把旧 attempt
标为 `lease_expired` 并重试同一 job。失败按任务上限进入 retry_wait 或 dead_letter。

现有 handler 是 at-least-once 兼容层：URL 唯一约束、旧日报定时任务的仅首次插入和派生索引事务提供基础
幂等；手工 `report` 命令仍可明确要求覆盖旧日报。启用 `INFOHUB_REPORT_READ_ENABLED=true` 和 `INFOHUB_REPORT_WRITE_ENABLED=true` 后，
日报任务改用冻结输入及不可变版本，同一天已有旧日报或已发布版本时跳过。
它们尚未全部通过 `publish_job_result` 生成对外 change。P06及后续领域 PR 必须逐步把
可见结果接入原子发布入口，不能因为 P05 有 durable job 就宣称所有领域写入已经 exactly-once。

## 三类健康信号

- `/api/live`：只检查 web 进程响应，不访问 SQLite。数据库或 worker 故障时仍为 200，便于确认
  门户进程没有崩溃。
- `/api/ready`：检查数据库查询、dataset owner 和当前构建 SHA 的 worker 心跳。任一失败返回
  503，供发布门禁使用。
- `/api/pipeline`：报告来源从未成功、失败、部分失败或过期，job blocked/dead-letter/过期租约，
  AI与索引积压和日报日期。业务数据延迟会标记 degraded，但不会让已有门户内容不可读。

`/api/health` 是兼容的完整快照，HTTP 状态采用 readiness，JSON 中的 `pipeline.status` 独立表达
数据新鲜度。网页 `/health` 同时展示三者。一个启用来源从未成功抓取也计入异常，不能用
“没有消息”掩盖“从未抓到”。

## 故障判断

- web 可打开、worker 显示 stale：保留门户读服务，检查 `infohub-worker` 日志和 jobs 状态；不要
  为了恢复 worker 删除数据库。
- worker 重启后：心跳应在 45 秒内恢复，过期任务由租约机制重领；核对 attempt 历史而不是手工
  重复插入任务。
- `/api/ready` 版本不一致：说明旧 worker 或旧心跳仍存在；发布准备正常情况下会清理心跳，先
  检查容器镜像 SHA，不要放宽版本检查。
- 来源异常但 ready：发布本身可接受，pipeline 仍 degraded；根据来源错误和退避时间处理。
- migrate 失败：web/worker 不应切换。使用已生成备份和迁移报告排查，不能跳过 migrate 强启。
