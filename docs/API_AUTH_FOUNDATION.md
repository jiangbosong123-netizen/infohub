# API 消费者身份底座（P17a）

Schema 22 新增 `api_consumers` 与 `api_keys`，不改业务数据表。消费者有独立身份与 `authz_version`；每把密钥有独立 `key_id`、权限集合、到期与撤销时间。密钥由系统安全随机生成，只返回一次，数据库只保存随机 secret 的 SHA-256 摘要。验证使用恒定时间摘要比较，并同时检查消费者状态、密钥撤销、到期与请求权限。撤销密钥或消费者会递增 `authz_version`，供后续游标/快照失效机制使用。

权限仅允许正式 API 契约中的 `read:catalog/items/events/analyses/evidence/signals/reports/sync/ops`，新密钥必须显式选择至少一项；没有默认全权密钥。当前模块提供受控 Python 函数 `create_consumer`、`issue_api_key`、`authenticate_api_key`、`revoke_api_key`、`revoke_consumer`，调用方负责数据库事务。任何日志、异常、结构化报告都不应输出完整 token；`IssuedApiKey` 的 `repr` 会隐藏它。获得明文 token 的维护者需要当场存入自己的安全凭据库，不能写入 `.env`、Git、URL 或网页 localStorage。

**当前尚未对外提供新 `/api/v1` 数据接口或管理命令，也没有给真实消费者发密钥。** `/api/v1` 请求现在先经过默认拒绝的 Bearer 入口，只有目标契约列出的只读方法和路径能够进入校验；校验通过后，尚未实现的路由仍返回 404。缺失/无效/撤销密钥返回不含敏感内容的 401，权限不足返回 403，鉴权数据库不可用返回 503；请求 ID 与错误结构遵循目标契约。未经列明的 v1 路径和方法直接 404。现有网页和 `/api/health` 等运行探针保持原行为。

后续独立 PR 才会接入维护者操作、每 key 限流/并发、审计、私网 HTTPS、具体 v1 数据路由与权限绑定的游标。入口鉴权不意味着现有 `/api/health` 或网页已经有应用层鉴权；在这些能力和生产验收完成前，不发布 v1 数据 API。新增路由必须明确登记其方法和权限，并单独验证字段级限制；单靠入口 scope 不允许返回受限全文。`POST /sync/snapshots` 当前只预留 `read:sync` 入口，未来实现仍须检查每个请求资源对应的读权限。

迁移按既有 `migrate_database` 流程先备份、在事务中扩展、再执行完整性校验。旧镜像可以忽略新增表，回滚代码时保留已创建的消费者与密钥记录；绝不通过恢复旧数据库来清除它们。密钥内容与私有消费者记录不得提交仓库。
