# 类型化只读 API（P18）

## P18a：实体目录列表

首个实现路由为 `GET /api/v1/entities`，要求 `read:catalog` scope，并受
`INFOHUB_API_CATALOG_ENABLED` 控制。开关默认 false；关闭时，已通过鉴权的调用返回结构化
`503 not_ready`。仓库测试通过不等于生产已经开放。

支持的参数只有：

- `limit`：1–100，默认 50；
- `cursor`：服务端签名的不透明游标；
- `q`：最长 200 字符，匹配当前 canonical name 或 active alias；
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

## P18j：主题目录与可审计统计

`GET /api/v1/topics` 和 `GET /api/v1/topics/{id}` 使用 `read:catalog` scope，并继续受
`INFOHUB_API_CATALOG_ENABLED` 总开关控制。列表只接受 `limit`、`cursor` 和正式 group 枚举；
详情当前只读完整发布中的 current topic version，不接受尚未定义的历史参数。未知、重复、空值或
非规范参数返回 `422 invalid_parameter`。

响应不会读取可变 legacy `item_topics` 计数。它只读取完整的 topic-statistics publication，并明确
返回 publication/build ID、两个计数策略版本、topic version、计数时间和 input manifest hash。
`unreviewed_assignments_excluded=true` 是契约字段：旧候选映射没有经人工或正式流程接受时不会冒充
公开计数。完整策略计算得到的 0 是有效数据；没有发布、dirty 非空、目录缺口、版本状态冲突或
count/member 不一致则整个接口返回 `503 not_ready`。

列表游标绑定 API key、consumer、权限版本、dataset epoch、group 过滤条件和 publication ID。
因此翻页期间发布发生变化会拒绝旧游标，调用方必须从第一页重启。详情返回包含权限、dataset、
publication 和完整 DTO 的 ETag，支持 `If-None-Match`。merged/restricted topic 不会从列表中静默
漏掉：列表 fail closed；restricted 详情返回 403，merged 详情在 canonical 投影完成前返回 503。

这两个路由不会切换现有 `/topics` 门户页面，也不会自动开启后台统计构建。生产开放仍需要审核
覆盖、真实非零/零语义验收和消费者契约验收。

2026-09-22 的隔离 Mac 副本演练把 schema 0、44,497 条 legacy item 的当前库副本迁移到
schema 25，再同步出 23 个实体、56 个主题和 11 个 publisher。实体列表返回 23 行；现有
identifier/relation 都没有 verified 证据，所以公共 DTO 对这两类字段返回空数组，而没有把
legacy_unverified assertion 升级为事实。演练事务最后回滚且临时副本删除；它不修改 Mac
原库，也不是 Windows 生产验收。结果见
[`p18a-api-entity-contract.json`](evidence/p18a-api-entity-contract.json)。

## P18b：实体详情与逻辑历史

`GET /api/v1/entities/{id}` 使用同一个 `read:catalog` scope 和功能开关，提供三种互斥读取：

- 不带历史参数：读取 current version；
- `version_id`：读取属于该实体的精确不可变版本；
- `as_of`：选择 `available_at<=as_of` 的最后一个实体版本，并把 alias、identifier、relation
  以及 relation target version 一起限制到该时间。

`as_of` 必须带时区并规范化为 UTC。它只是逻辑应用历史，响应中的 knowledge cutoff 明确写成
`basis=logical_as_of`、`clock_status=unknown`，不声称证明当时的严格可见范围。当前
`knowledge_checkpoint_id` 返回 `422 unsupported_history`；P19 完成数据库高水位、时钟证据与
快照语义后才能启用。`version_id` 与 `as_of` 不能同时提供，未知、重复、空值和超长参数都
拒绝。

精确旧版本只包含在该版本 `available_at` 时已经存在的 verified assertion，后来的 alias、
identifier 或 relation 不会倒灌进旧响应。历史 relation target 也解析为当时最后一个可用
target version。restricted identity 返回 403；merged identity 在 canonical projection 未完成
前返回 503。identity/current version 缺失或 type/status 不一致同样 fail closed。

详情响应生成 permission-aware ETag，摘要覆盖 consumer、authz version、scope、dataset/epoch、
knowledge cutoff 和完整类型化 DTO。`If-None-Match` 命中返回无正文 304；响应不把 token 或
token hash 放进 ETag。

2026-09-23 的隔离 Mac 副本演练覆盖 schema 0、47,101 条 legacy item 和 23 个同步实体。
current、精确 version 和相同时间的 logical as-of 都解析到同一预期版本，logical as-of 保持
`clock_status=unknown`，ETag 格式通过。演练使用临时副本和回滚事务，不修改源数据库，也不
代表 Windows 生产已经开启。结果见
[`p18b-api-entity-history.json`](evidence/p18b-api-entity-history.json)。

## P18c：实体精确标识查询

`GET /api/v1/entities` 增加 `identifier_namespace`、`identifier_value` 和 `exchange`。namespace 与
value 必须成对出现；`exchange_ticker` 还必须同时给出 exchange，其他 namespace 禁止携带
exchange。ticker 和 exchange ticker 在进入查询及游标签名前规范化为大写，其他标识保持精确
字节值。空值、超长值、缺少配对参数、未知参数或重复参数返回 `422 invalid_parameter`。

查询只匹配 `verification_status='verified'` 的 assertion，并走
`idx_entity_identifiers_lookup(namespace,value,verification_status)`。同一个交易所 ticker 如果存在
多个已核验候选，接口按稳定实体 ID 返回全部候选并分页，不擅自挑选第一个。标识筛选与 q/type
可以组合，全部规范化筛选值都进入签名游标；翻页时改变 namespace、value 或 exchange 返回
`400 filter_mismatch`。

当前列表仍是 current catalog view；标识有效期的历史解释将与列表 `as_of`/P19 checkpoint 一起
实现。返回的 identifier DTO 保留 valid_from/valid_to，消费者不能把当前列表当成完整历史证券
主数据。2026-09-23 的 Mac 数据库隔离副本中有 65 条 identifier，全部是
`legacy_unverified`，verified 和 verified exchange_ticker 都是 0；因此真实副本查询不会把旧
watchlist ticker 冒充为已核验身份。合成契约测试覆盖大小写规范化、交易所限定、多候选分页、
未核验排除和游标过滤绑定。结果见
[`p18c-api-entity-identifiers.json`](evidence/p18c-api-entity-identifiers.json)。
