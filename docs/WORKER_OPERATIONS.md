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

## 批量维护命令的连接复用

每次 `get_db()` 新建连接本身很快，但新连接的第一条语句要解析整个 schema（约 740 个对象，约 1.8 ms），而一个旧 AI
策展导入任务要开约 10 次连接，这部分占了每个任务的大部分时间。`legacy-curation-run` 与 `projection-builders-run`
在 `database.reused_connections()` 内运行：每个 `with get_db()` 照常在退出时提交或回滚，取出时重新设置标准 PRAGMA
与 row factory，只是不立即关闭，供同一线程的下一次 `get_db()` 复用；命令结束时全部关闭。演练规模副本上抽样 3,000 个
导入任务从约 30 ms/个降到 6.6 ms/个；300 条的对照导入中，复用与逐次新建连接的结果逐项一致。web 与 worker
不使用该作用域，行为不变。

## 任务领取索引（schema 45）

`claim_job` 先按 priority、再按到期时间领取。schema 45 增加只覆盖 `pending/retry_wait` 的有序部分索引
`idx_jobs_claim_order`，使数据库按索引顺序直接读出第一条可领取任务，不再在每次领取时把整个积压排序。领取顺序
不变；在 34.8 万条积压的演练副本上，单次领取查询从约 93 ms 降到 0.03 ms（见
[`docs/evidence/perf-job-claim-index.json`](evidence/perf-job-claim-index.json)）。

## 发布与预算查询索引（schema 46）

每次分析结果发布都会按 `version_id` 查 `change_log` 做幂等检查，每次模型调用授权都会汇总当天已预留预算。
这两个查询原先没有可用索引，耗时随全部历史线性增长（演练副本 4.8 万行时分别约 15 ms 与 5 ms）。schema 46 增加
`idx_change_log_version` 与覆盖索引 `idx_analysis_authorizations_budget`，查询语句与结果不变（抽样 300 个已有与
50 个不存在的 version_id、各 provider × 各日预算汇总全部一致）；`change_log` 查询降到约 0.002 ms。

预算汇总仍要扫描当天该 provider 的全部授权：旧数据导入在同一天产生 35.8 万个 0 元授权，求和随之线性变慢，到 25 万个
时每个任务约 10 ms，占任务耗时的三分之二。预留金额有 `CHECK(reserved_cost_microusd>=0)`，0 元行对总和没有贡献，
所以汇总改为只取 `reserved_cost_microusd>0` 的行（`DAILY_RESERVED_SQL`），覆盖索引直接跳过 0 元行：同一副本上 9 ms
降到 0.003 ms，结果不变（随机 2,000 条混合授权的各 provider × 日期汇总逐一相等）。

## 队列触发器的冲突处理（schema 47）

搜索、派生分类和主题统计的待处理队列（`curation_search_dirty`、`derived_dirty`、`topic_statistics_dirty`）由触发器
写入。原触发器使用 `INSERT OR REPLACE` / `INSERT OR IGNORE`，而 SQLite 会用**触发它的那条语句**的冲突策略覆盖触发器
内的策略：分析结果发布用 UPSERT 更新当前发布指针，其 `DO UPDATE` 带 ABORT 策略，所以同一文档同一任务（翻译、摘要、
相关性）**再次发布**时，只要该条目还在搜索队列里（搜索开关默认关闭时队列不会被消费），整笔发布就会以
`UNIQUE constraint failed: curation_search_dirty.item_id` 失败（队列不清空，重试也一直失败）；`UPDATE OR IGNORE` 还会让队列保留旧原因。
schema 47 把 13 个队列触发器改为触发器自己的 `ON CONFLICT … DO UPDATE/DO NOTHING`（不受外层语句影响），入队结果
不变，只重建触发器、不改写数据。`tests/test_queue_trigger_conflicts.py` 禁止任何触发器再依赖外层冲突策略。

