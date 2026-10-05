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
- 例行任务清理：同一次清理删除 30 天前已成功的例行任务（抓取、AI、对账、日报、清理与三个构建器）及其尝试记录；
  被变更日志、分析运行或同步快照引用的任务一律保留（见“例行任务的保留期（schema 50）”）。

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

## 启动校验的耗时

migrate、web、worker 启动时都会对数据库做完整校验（`verify_database`）：除 schema 结构外，还逐条核对分析账本
（运行、输入、授权、尝试、结果、发布指针及其 change_log 行），最后执行 SQLite 的 `integrity_check` 与
`foreign_key_check`。账本检查原先每条分析记录要发 5–10 次单独查询；现在每个账本用一次按主键关联的流式查询，
逐行判断逻辑不变（`tests/test_analysis_ledger_verification.py` 对每一种篡改都要求报出同一账本、同一计数，另做过
2,800 次随机篡改的新旧对照，结果逐字一致）。校验连接使用 64 MB 页缓存，使两项 SQLite 检查少重读索引页。

在演练规模的库（4.9 GB、35.8 万条分析）上，Mac 文件缓存已热时整次校验约 44 秒降到约 34 秒，执行的 SQL 从
394 万条降到 224 条；文件缓存冷时 SQLite 自身的 `integrity_check` 就要 30–40 秒。Windows 上 Docker 挂载目录读写更慢，
时间会更长。因此 web 与 worker 的健康检查 `start_period` 设为 600 秒：启动校验期间显示 starting，校验结束开始响应
后立即转为 healthy。部署管理器的验收等待也需要覆盖这段时间。

## 批量维护命令的连接复用

每次 `get_db()` 新建连接本身很快，但新连接的第一条语句要解析整个 schema（约 740 个对象，约 1.8 ms），而一个旧 AI
策展导入任务要开约 10 次连接、历史回填每条约 5 次，这部分占了大部分时间。`legacy-backfill`、`legacy-topic-backfill`、
`legacy-curation-run` 与 `projection-builders-run` 在 `database.reused_connections()` 内运行：每个 `with get_db()` 照常在退出时提交或回滚，取出时重新设置标准 PRAGMA
与 row factory，只是不立即关闭，供同一线程的下一次 `get_db()` 复用；命令结束时全部关闭。演练规模副本上抽样 3,000 个
导入任务从约 30 ms/个降到 6.6 ms/个；300 条的对照导入中，复用与逐次新建连接的结果逐项一致。历史回填同机
1,237 → 147 秒，固定 ID 与时钟后全部 146 张表除时间戳外逐行一致。web 与 worker 不使用该作用域，行为不变。

## 同步快照的内存占用

`sync-snapshot` 任务（仅在默认关闭的同步 API 开启后才会产生）先用 SQLite backup API 复制整个数据库，再把每种资源的
最新公开记录写成页文件。原实现为算备份摘要把整个副本读进内存，又把一种资源的全部记录放进列表后再分页：演练规模
（4.9 GB、35.8 万条分析）的一次快照峰值内存约 4.8 GB，并随数据库继续增长。现在备份摘要按 1 MB 分块计算，
记录按 SQLite 的字节序逐条读出、每满一页立即写出，峰值约 39 MB，耗时 26 秒降到 15.5 秒；同一副本上新旧
实现生成的清单与全部 3,583 个页文件逐字节相同。若读到的记录不是 UTF-8 字节序（例如 UTF-16 编码的库），任务直接失败，
不会写出顺序不同的快照。

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

## 按结果查发布版本的索引（schema 51）

`GET /api/v1/analyses/{id}` 与启动校验中的语气结果检查都按 `result_id` 查发布版本，而该列没有索引：演练规模
（35.8 万条分析）上每次 API 请求要扫描全部发布版本，热缓存约 0.3 秒、冷缓存约 2.5 秒。schema 51 增加
`idx_analysis_publication_versions_result`（建索引约 0.1 秒），同一查询降到 0.01 ms 以下。日报冻结时读取发布指针的查询
与门户一样固定从本次文档开始（约 0.86 s → 0.02 s）。`tests/test_query_plans.py` 检查这些查询实际执行时的计划，
不允许整表扫描。

## 例行任务的保留期（schema 50）

每个计划到点都会生成一条持久任务。三个投影构建器开启后每分钟各一条，加上抓取、AI 等，每天约 4,700 条，按每条（含
尝试记录与索引）约 1 KB 计一年约 1.7 GB，此前从不清理。每日清理现在删除 30 天前已成功的例行任务；失败、进行中、非例行
（如分析、旧数据导入、同步快照）以及被变更日志、分析运行或同步快照请求引用的任务都不删除。计划的去重键随到期时间前移，
删除旧任务不会让计划重复执行。

开启外键时，删除任务要检查引用它的行。`analysis_runs.job_id` 与 `change_log.lease_token` 原先没有索引，每删一条都要扫描
全部分析运行和变更；schema 50 为二者建索引（演练规模副本上约 20 秒；正式升级时在旧库上执行，这两张表尚为空），之后
删除 13 万条例行任务约 1.2 秒。`tests/test_job_retention.py` 要求所有引用任务或尝试记录的外键都有索引。

## 热度队列只记录真实变化（schema 49）

增量聚类每轮（默认每 15 分钟）都会重写最近 14 天内每个事件的标题、链接、时间等列以刷新衰减的旧热度，而
`AFTER UPDATE OF` 触发器只要这些列出现在 SET 中就会执行，即使值没变。演练规模上每轮因此把约 2.2 万个事件塞进新版
热度队列，worker 每分钟最多处理 1,000 个，队列永远清不空，新版热度一直处于“不可用”而回退旧逻辑；空闲时热度任务每分钟
还要空转 10 批（约 0.15 秒）。schema 49 让该触发器只在列值真正改变时入队：空闲时热度任务约 4 ms、1 批，新增 50 条新闻
后只处理真正受影响的事件；增量维护的热度与全量重算对 76,586 个事件逐行一致（热度值与计算时间除外，读取时按同样的
18 小时半衰期衰减）。

## 门户最近更新时间索引（schema 48）

首页每次显示“最近更新”都执行 `SELECT MAX(fetched_at) FROM items`。`fetched_at` 没有索引时这要扫描全部条目：
演练规模（89,575 条）上热缓存约 22 ms（占首页 32 ms 的大部分），冷缓存约 160 ms。schema 48 增加
`idx_items_fetched`，同一查询降到约 0.02 ms、结果不变；建索引约 0.03 秒，不改写数据。

## 队列触发器的冲突处理（schema 47）

搜索、派生分类和主题统计的待处理队列（`curation_search_dirty`、`derived_dirty`、`topic_statistics_dirty`）由触发器
写入。原触发器使用 `INSERT OR REPLACE` / `INSERT OR IGNORE`，而 SQLite 会用**触发它的那条语句**的冲突策略覆盖触发器
内的策略：分析结果发布用 UPSERT 更新当前发布指针，其 `DO UPDATE` 带 ABORT 策略，所以同一文档同一任务（翻译、摘要、
相关性）**再次发布**时，只要该条目还在搜索队列里（搜索开关默认关闭时队列不会被消费），整笔发布就会以
`UNIQUE constraint failed: curation_search_dirty.item_id` 失败（队列不清空，重试也一直失败）；`UPDATE OR IGNORE` 还会让队列保留旧原因。
schema 47 把 13 个队列触发器改为触发器自己的 `ON CONFLICT … DO UPDATE/DO NOTHING`（不受外层语句影响），入队结果
不变，只重建触发器、不改写数据。`tests/test_queue_trigger_conflicts.py` 禁止任何触发器再依赖外层冲突策略。

