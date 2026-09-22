# 类型化只读 API（P18）

## P18a：实体目录列表

首个实现路由为 `GET /api/v1/entities`，要求 `read:catalog` scope，并受
`INFOHUB_API_CATALOG_ENABLED` 控制。开关默认 false；关闭时，已通过鉴权的调用返回结构化
`503 not_ready`。仓库测试通过不等于生产已经开放。

支持的参数只有：

- `limit`：1–100，默认 50；
- `cursor`：服务端签名的不透明游标；
- `q`：最长 120 字符，匹配当前 canonical name 或 active alias；
- `type`：正式实体类型枚举之一。

未知参数、重复参数、非规范整数或未知类型返回 `422 invalid_parameter`。列表固定按稳定实体
ID 升序，不允许调用方改变排序。响应包含 `api_version`、`schema_version`、dataset ID、epoch、
request ID、生成时间、明确为 null 的 knowledge cutoff、类型化 data 与 live pagination。

游标有效期 15 分钟，并使用该 API key 在数据库中的不可逆 secret 摘要作为 HMAC key。签名
载荷绑定 resource、filter hash、最后一个 ID、consumer ID、key ID、authz version、dataset
epoch 与过期时间。因此游标不能跨 key、消费者、权限版本、筛选条件、资源或 epoch 使用。
篡改/跨 key 返回 400，筛选变化返回 400，epoch 变化返回 409，过期返回 410。完整同步不得
使用 live cursor；P19 将提供 snapshot + changes。

当前列表读取 entity/current entity version，inactive 对外映射为 retired。只有 active、unique alias 与
`verification_status='verified'` 的 identifier/relation 可以进入公共 DTO；candidate、
legacy_unverified 和 rejected assertion 不会被悄悄提升为已验证事实。现有 schema 尚未给
merged/restricted entity 定义足够的公开 canonical/restriction 投影，因此检测到此类身份时
整个接口返回 `503 not_ready`，而不是漏行或返回不完整 canonical ID。

P18 后续会按独立 PR 增加 entity detail、topic/source 目录、item/event/analysis 等领域读取。
在统一查询服务、历史语义和生产消费者验收完成前，v1 仍不声明为稳定外部服务。

2026-09-22 的隔离 Mac 副本演练把 schema 0、44,497 条 legacy item 的当前库副本迁移到
schema 25，再同步出 23 个实体、56 个主题和 11 个 publisher。实体列表返回 23 行；现有
identifier/relation 都没有 verified 证据，所以公共 DTO 对这两类字段返回空数组，而没有把
legacy_unverified assertion 升级为事实。演练事务最后回滚且临时副本删除；它不修改 Mac
原库，也不是 Windows 生产验收。结果见
[`p18a-api-entity-contract.json`](evidence/p18a-api-entity-contract.json)。
