# API 消费者身份底座（P17a）

Schema 22 新增 `api_consumers` 与 `api_keys`，不改业务数据表。消费者有独立身份与 `authz_version`；每把密钥有独立 `key_id`、权限集合、到期与撤销时间。密钥由系统安全随机生成，只返回一次，数据库只保存随机 secret 的 SHA-256 摘要。验证使用恒定时间摘要比较，并同时检查消费者状态、密钥撤销、到期与请求权限。撤销密钥或消费者会递增 `authz_version`，供后续游标/快照失效机制使用。

权限仅允许正式 API 契约中的 `read:catalog/items/events/analyses/evidence/signals/reports/sync/ops`，新密钥必须显式选择至少一项；没有默认全权密钥。当前模块提供受控 Python 函数 `create_consumer`、`issue_api_key`、`authenticate_api_key`、`revoke_api_key`、`revoke_consumer`，调用方负责数据库事务。任何日志、异常、结构化报告都不应输出完整 token；`IssuedApiKey` 的 `repr` 会隐藏它。获得明文 token 的维护者需要当场存入自己的安全凭据库，不能写入 `.env`、Git、URL 或网页 localStorage。

**当前尚未对外提供新 `/api/v1` 数据接口或管理命令，也没有给真实消费者发密钥。** `/api/v1` 请求现在先经过默认拒绝的 Bearer 入口，只有目标契约列出的只读方法和路径能够进入校验；校验通过后，尚未实现的路由仍返回 404。缺失/无效/撤销密钥返回不含敏感内容的 401，权限不足返回 403，鉴权数据库不可用返回 503；请求 ID 与错误结构遵循目标契约。未经列明的 v1 路径和方法直接 404。现有网页和 `/api/health` 等运行探针保持原行为。

Schema 23 为消费者创建、密钥签发/撤销、消费者撤销增加追加式 `api_key_audit`。四种写操作现在都需要显式传入操作者标签，并在同一事务内写审计记录；审计保存动作、时间、消费者/密钥 ID 及安全元数据，不保存 token 或摘要。SQLite 触发器阻止常规 UPDATE/DELETE；本机数据库管理员仍能修改文件，所以这不是防管理员篡改的合规账本。

在数据库主机上使用 `python cli.py api-admin --help` 查看命令。先运行 `consumer-create NAME`，再运行 `key-issue CONSUMER_ID --scope read:items --expires-at 2026-12-31T00:00:00Z`。明文 token **仅在该命令成功提交后打印一次**，请立即放入安全凭据库；终端回滚记录也按密钥对待，不要复制到聊天、截图、Git、日志或普通备份。`consumer-list`、`key-list CONSUMER_ID`、`audit-list` 只显示不含 token/摘要的元数据；`key-revoke KEY_ID` 与 `consumer-revoke CONSUMER_ID` 可即时撤销。命令读取当前环境配置的数据库，先验证其 schema，不自动迁移；操作者标签取本机操作系统用户名，主要用于追踪，不能代替操作系统账户权限。

Schema 24 加入跨 web 进程共享的短时配额状态。默认每把 key 每个固定 UTC 分钟窗口最多 60 次、每个消费者同时最多 5 个正在处理的 v1 请求；可通过 `INFOHUB_API_KEY_RATE_PER_MINUTE`、`INFOHUB_API_CONSUMER_CONCURRENCY` 调整。超限返回 `429 rate_limited` 和 `Retry-After`，未获准请求不消耗额度。准入和并发占用在 SQLite `BEGIN IMMEDIATE` 事务中原子完成；请求结束释放占用，进程崩溃后的占用按 `INFOHUB_API_REQUEST_LEASE_SECONDS`（默认 300 秒）过期。v1 同步等长任务必须异步排队并快速返回，不能在一个 HTTP 请求内运行超过占用期限。配额表是临时运行状态，不是长期审计或计费记录；旧窗口和过期占用在后续请求时清理。固定分钟窗口允许边界附近突发，负载验证后可考虑更平滑的算法。各 web 进程必须使用同一数据库和同一配额配置。

Schema 25 为已经识别消费者的 v1 请求增加追加式审计事件。准入与拒绝事件和配额事务一起提交；处理完成或异常时记录状态码与基于 monotonic clock 的耗时。字段只允许 request ID、消费者/密钥 ID、HTTP 方法、抽象资源类别、所需 scope、结果与时间，不接受原始路径、对象 ID、query、Authorization、token、响应正文或异常详情。`python cli.py api-admin request-audit-list` 可按消费者或 key 查看这些安全字段。无效 token 和未知路径只写不含路径/token 的轮转应用日志，不逐条写数据库，避免匿名垃圾流量无限扩大持久表。审计触发器阻止普通 UPDATE/DELETE，但本机数据库管理员仍可修改文件，因此不声称防管理员篡改。

后续独立 PR 才会接入私网 HTTPS、具体 v1 数据路由与权限绑定的游标。入口鉴权不意味着现有 `/api/health` 或网页已经有应用层鉴权；在这些能力和生产验收完成前，不发布 v1 数据 API。新增路由必须明确登记其方法和权限，并单独验证字段级限制；单靠入口 scope 不允许返回受限全文。`POST /sync/snapshots` 当前只预留 `read:sync` 入口，未来实现仍须检查每个请求资源对应的读权限，并单独实现快照配额。

迁移按既有 `migrate_database` 流程先备份、在事务中扩展、再执行完整性校验。旧镜像可以忽略新增表，回滚代码时保留已创建的消费者与密钥记录；绝不通过恢复旧数据库来清除它们。密钥内容与私有消费者记录不得提交仓库。
