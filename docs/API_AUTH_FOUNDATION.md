# API 消费者身份底座（P17a）

Schema 22 新增 `api_consumers` 与 `api_keys`，不改业务数据表。消费者有独立身份与 `authz_version`；每把密钥有独立 `key_id`、权限集合、到期与撤销时间。密钥由系统安全随机生成，只返回一次，数据库只保存随机 secret 的 SHA-256 摘要。验证使用恒定时间摘要比较，并同时检查消费者状态、密钥撤销、到期与请求权限。撤销密钥或消费者会递增 `authz_version`，供后续游标/快照失效机制使用。

权限仅允许正式 API 契约中的 `read:catalog/items/events/analyses/evidence/signals/reports/sync/ops`，新密钥必须显式选择至少一项；没有默认全权密钥。当前模块提供受控 Python 函数 `create_consumer`、`issue_api_key`、`authenticate_api_key`、`revoke_api_key`、`revoke_consumer`，调用方负责数据库事务。任何日志、异常、结构化报告都不应输出完整 token；`IssuedApiKey` 的 `repr` 会隐藏它。获得明文 token 的维护者需要当场存入自己的安全凭据库，不能写入 `.env`、Git、URL 或网页 localStorage。

**当前尚未对外提供新 `/api/v1` 接口或管理命令，也没有给真实消费者发密钥。** 后续独立 PR 才会接入 Bearer 中间件、维护者操作、每 key 限流/并发、审计、私网 HTTPS 与权限绑定的游标。本次 schema 扩展不意味着现有 `/api/health` 或网页已经有应用层鉴权；在这些能力和生产验收完成前，不发布 v1 数据 API。

迁移按既有 `migrate_database` 流程先备份、在事务中扩展、再执行完整性校验。旧镜像可以忽略新增表，回滚代码时保留已创建的消费者与密钥记录；绝不通过恢复旧数据库来清除它们。密钥内容与私有消费者记录不得提交仓库。
