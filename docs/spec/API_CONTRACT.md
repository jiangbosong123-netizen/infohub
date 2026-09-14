# 对外 API 契约 v1

状态：目标契约；PR #1 当前 `/api/v1` 实现未获此契约认可。生产 `main` 只有 `/api/health` 与页面，集成接口应在 R4 完成鉴权、契约测试与同步演练后宣布可用。

机器可读入口：[openapi.yaml](openapi.yaml)。本文补充机器 schema 无法完整表达的事务、时间与同步语义。示例是合成数据，不能当作已上线返回。

## 1. 服务边界

基础路径 `/api/v1`。JSON UTF-8。外部消费者通过 HTTPS 私网域名访问（TLS 入口为部署改造项）；现有 HTTP IP 入口只作为当前私网兼容访问，不在明文公网传 API token。

- 普通接口只读领域数据。快照创建是一次受权限控制的导出任务，不修改文档、事件或分析内容。
- 抓取、模型重跑、人工修正和部署操作不进入只读 API；内部管理 API 使用 `/internal/v1` 并单独鉴权和审计。
- 同一查询服务驱动网页与 API，避免页面过滤与研究接口各写一套不一致逻辑。
- `/api/health` 保留兼容；新 `/health/live` 与 `/health/ready` 见运维，机器运行诊断字段不混入内容列表。

## 2. 身份与权限

每个消费者独立 Bearer token，服务端仅存不可逆哈希、key_id、scopes、状态、到期时间；token 不进 URL、日志、数据库导出或网页 localStorage。私网是网络边界，应用 token 是消费者边界，二者都需要。

权限：`read:catalog`、`read:items`、`read:events`、`read:analyses`、`read:evidence`、`read:signals`、`read:reports`、`read:sync`、`read:ops`。role 只是 scope 集合；没有默认全权 key。

默认每 key 60 请求/分钟、单消费者同时 5 个查询、limit 最大 100；快照每 key 最多 1 个运行任务、每日默认 5 次。超过返回 429+Retry-After。该值是首期配置默认，负载验证后可调整。不同 key 不能互用游标/快照；权限变更触发 authz_version 更新，旧同步会话要求重建。

受限材料返回 metadata 和 restriction reason 或 403；不能因为禁止全文就把其他有权查看的事件整个漏掉。异常内容在管理员复核空间，不因 `scope=research` 越过安全限制。

## 3. 公共数据规则

- ID 是不透明字符串；旧 item 整数通过 `legacy_item_id` 返回。消费者使用新 `id` 持久引用。
- 响应带 `api_version`、`schema_version`、`dataset_id`、`dataset_epoch`、`request_id`、`generated_at`。
- UTC 时间包含时区；输入无时区/非法日期返回 422。日期窗口采用 `[from,to)`；分页排序包含唯一 ID 作为平局键。
- NULL 是未知/未有值，空数组是确认没有成员。`summary_original:null` 代表历史未保留，`""` 代表收到的摘要为空；二者不能合并成空串。
- `importance_score` 范围 0..100；tone/impact 的 confidence 范围 0..1 或 NULL；热度是排序指标，不是概率。负分不在内容协议中出现。
- 每个分析对象提供 `processing_state`、`review_status`、`stale`、`evidence_status`，未分析不能伪装为 neutral/0。
- 列表默认 `scope=research`，不套科技首页的精选规则。`scope=selected` 明确请求展示投影，返回 selection_policy_version 与选择理由。
- `channel` 为兼容展示字段；正式多标签用 topic_ids/entity_ids；实体提及与受影响实体分别过滤。

## 4. 端点清单

| 方法与路径 | 目的 | 参数/返回要点 |
|---|---|---|
| GET `/items` | 文档当前视图列表 | limit,cursor,scope,entity_id,topic_id,publisher_id,kind,published_from/to,observed_from/to |
| GET `/items/{id}` | 单文档指定视图 | version_id 或 as_of 二选一；原文可得状态、版本、分析引用 |
| GET `/items/{id}/versions` | 文档版本列表 | limit,cursor，按版本号倒序；不返回未经授权的全文 |
| GET `/items/{id}/evidence` | 原文证据 | 必须 version_id；分页，精确定位和证据类型 |
| GET `/evidence/{id}` | 按引用ID读取证据 | 不可变版本定位；受限内容返回明确状态或403 |
| GET `/events` | 稳定事件列表 | topic_id,entity_id,type,status,available_from/to,limit,cursor |
| GET `/events/{id}` | 当前或历史事件 | version_id/as_of，facts、引用、状态、canonical/replacement ID |
| GET `/events/{id}/evidence` | 支持/冲突/背景证据 | 必须 version_id，可 role；不将冲突证据隐藏 |
| GET `/analyses/{id}` | 不可变分析结果及复核状态 | 可选as_of；输出内容不可变，状态按指定知识时点解析；原始请求敏感信息不外泄 |
| GET `/entities` | 实体目录 | q,type,limit,cursor；稳定 ID、别名和证券标识 |
| GET `/entities/{id}` | 实体指定版本 | version_id/as_of；解析历史事件引用的实体名称与身份 |
| GET `/topics` | 主题目录 | group,limit,cursor；计数明确为文档/事件和统计时间 |
| GET `/topics/{id}` | 主题指定版本 | version_id/as_of；目录属性按历史版本，统计使用对应时点 |
| GET `/sources` | 来源目录与覆盖说明 | 可公开元数据，排除凭据/内部路径；运行细节另需 read:ops |
| GET `/sources/{id}` | 来源公开配置指定版本 | version_id/as_of；配置历史依然脱敏 |
| GET `/signals` | 宏观/情绪汇总 | target_id,aspect,horizon,window_from/to,as_of,formula_version,limit,cursor |
| GET `/signals/{id}` | 某信号的指定版本 | version_id/as_of；样本、覆盖与manifest标识 |
| GET `/signals/{id}/inputs` | 分页读取信号输入清单 | 必须version_id，limit/cursor；每条InputRef可进一步取事件/分析/证据 |
| GET `/reports` | 报告目录 | date_from/to,limit,cursor |
| GET `/reports/{id}` | 报告某版本 | version_id/as_of；正文、素材 manifest、引用、覆盖与生成模式 |
| POST `/sync/snapshots` | 建立一致性导出 | resources,scope；202+snapshot_id，需 read:sync 及各资源读权限 |
| GET `/sync/snapshots/{id}` | 导出状态与水位 | pending/running/ready/failed/expired，epoch,H,expires_at |
| GET `/sync/snapshots/{id}/pages` | 读取固定快照 | resource,cursor,limit；同快照数据不可漂移 |
| GET `/changes` | 可靠增量 | cursor,limit；按 seq 升序；默认订阅全授权资源集合 |

兼容 `/stories` 若保留，只作为 `/events` 的文档化别名，不形成第二套事实实体。现有 `/story/{uuid}` 页面和旧主题 slug 必须继续解析。新 v1 发布前无第三方用户，因此可以重新设计 PR #1 的未发布接口；若发现实际消费者，则先登记并提供过渡期。

## 5. DTO 字段契约

### 5.1 Item

必需：`id,version_id,kind,status,title_original,title_display,content_quality,published_at,published_precision,time_status,first_seen_at,available_at,publisher,source_refs,entity_mentions,topic_refs,event_refs,analysis_refs,processing_state,point_in_time_eligible`。

可选/可空：legacy_item_id、summary_original、summary_display、canonical_url、importance_score、selection。`title_display` 标 `source_language/translation/edited` 来源；每个生成字段有 analysis_id。`event_refs` 是数组，不能继续用单 `story_id` 限制多事件。

Example：

```json
{
  "id": "doc-demo-001",
  "version_id": "drev-demo-002",
  "legacy_item_id": 102,
  "kind": "article",
  "status": "active",
  "canonical_id": null,
  "title_original": "Example company corrects its investment plan",
  "title_display": "Example company corrects its investment plan",
  "summary_original": null,
  "summary_display": null,
  "canonical_url": "https://example.com/report",
  "language": "en",
  "display_provenance": {
    "title_method": "source_language",
    "title_analysis_id": null,
    "summary_analysis_id": null
  },
  "content_quality": {
    "origin": "legacy_unknown",
    "extent": "title_only",
    "truncated": false,
    "extraction_status": "legacy_unverified"
  },
  "published_at": "2026-09-10T09:00:00.000000Z",
  "published_precision": "minute",
  "time_status": "legacy_unverified",
  "first_seen_at": "2026-09-14T12:00:00.000000Z",
  "available_at": "2026-09-14T12:00:00.000000Z",
  "publisher": {
    "id": null,
    "name": null,
    "attribution_status": "unknown"
  },
  "source_refs": [],
  "entity_mentions": [],
  "topic_refs": [],
  "event_refs": [],
  "analysis_refs": [],
  "processing_state": "insufficient_evidence",
  "point_in_time_eligible": false,
  "importance_score": null,
  "selection": null
}
```

此为完整合成Item示例，与OpenAPI共同校验；原文缺失、未识别发布方和未完成分析有独立状态。

### 5.2 Event

`id,version_id,status,canonical_id,replacement_ids,title,event_type,event_time,first_seen_at,available_at,last_fact_change_at,latest_report_at,knowledge_status,facts,entities,topics,report_count,publisher_count,independent_origin_count,evidence_coverage,analysis_refs`。

`independent_origin_count` 在来源链不完整时NULL，并附unknown_origin_count；不默认为publisher_count。`facts`中每项有fact_id、modality和evidence_ids。未通过语义评估的legacy candidate不得升为active，也不能把knowledge_status写成confirmed_by_primary。

合并旧 ID 默认 200 返回轻量 alias 对象：status=merged、canonical_id、available_at。拆分返回 status=split 与 replacement_ids。需要完整原状态使用 as_of/version_id，不能对历史查询直接重定向到新事实。

### 5.3 Analysis

`id,view_version_id,state_as_of,task_type,schema_version,subject_ref,input_refs,input_hash,pipeline_version,provider,model_requested,model_resolved,prompt_hash,parameters_hash,analyzed_at,available_at,validation_status,review_status,stale,output,evidence_refs,evidence_status,processing_state`。

`output` 按task_type判别，完整集合由OpenAPI定义，包含策展兼容输出及独立translation/relevance/summarization/importance/entity_linking/event_extraction/event_linking/tone/impact/macro_mapping/report。不会把模型原始thought或凭据输出给消费者。token/成本/错误堆栈属于ops视图。

`id`对应不可变结果；`review_status/evidence_status/stale`是指定时点的发布/复核状态，`view_version_id`标识此状态版本，`state_as_of`是解析截止时间。查询as_of不早于结果available_at；更早则404。后续人工复核不会修改output，而会改变状态版本。ETag同时包含result及view版本，不能因id相同缓存过期复核状态。

### 5.4 Signal / Report

信号包含：target/aspect/horizon、window_start/end、as_of、formula_version、analysis_version_group、score（可空）、n_events/n_eligible/n_valid/n_unknown/n_mixed、coverage、valid_fraction、source_mix、status、input_manifest_ref。coverage是覆盖范围/缺失说明对象，valid_fraction是n_valid/n_eligible（分母0则NULL），二者不能混用。

input_manifest_ref在同一信号版本的`/signals/{id}/inputs?version_id=...`解析，不能是只有服务器能打开的路径。该接口按固定InputRef清单分页；普通文档/事件/分析引用分别通过对应详情与version_id/as_of读取，证据用/evidence/{id}。目录的历史version_id也有详情读取路径，不要求消费者预先缓存所有旧目录。

报告包含：date/timezone/as_of/version、mode=llm/structured_fallback/legacy_unknown、markdown、citations、input_manifest、coverage、supersedes。晚到数据补充报告形成新版本。

## 6. 列表分页与历史读取

普通列表返回：

```json
{
  "api_version":"v1", "schema_version":"1.0.0",
  "dataset_id":"dataset-demo", "dataset_epoch":"epoch-demo",
  "request_id":"request-demo", "generated_at":"2026-09-14T12:00:00.000000Z",
  "data":[],
  "pagination":{"limit":50,"next_cursor":null,"consistency":"live"}
}
```

普通浏览默认 `consistency=live`，用于用户阅读，不能充当数据同步。游标签名载荷包含排序位置、排序字段、资源、过滤参数 hash、调用身份/权限版本、epoch、过期时间；客户端视为不透明字符串。更换过滤条件、跨资源使用游标返回 400；过期返回 410。

默认排序固定：items按`first_seen_at DESC,id DESC`；events按`last_fact_change_at DESC,id DESC`（alias使用available_at）；版本列表按`version DESC,id DESC`；目录按`id ASC`；reports按`date DESC,available_at DESC,id DESC`；signals按`window_end DESC,as_of DESC,id DESC`；证据按`id ASC`。首版不提供任意sort字符串；改变默认顺序属于契约变更。published过滤不改变first_seen排序语义；所有范围校验from<to，无时区时间拒绝。

需要完整无漂移导出时使用 snapshot，不能把倒序 published_at 遍历说成“零遗漏增量”。`as_of` 必须选最后一个 `available_at<=as_of` 的版本，并按当时有效关系解析，而非只给当前表加日期过滤。无法支持历史的 legacy 字段返回 point_in_time_eligible=false，不伪造。

## 7. 快照与变化流：接入大系统的唯一可靠同步路径

1. 创建快照，指定资源与 research/selected scope。服务返回 202 和 ID；同一幂等请求键在 24h 内重用任务，不重复导出。
2. worker 通过 SQLite backup API 建一致性副本。**从已完成的副本读取 epoch 和 change high-water H**，不能从线上先取 H 再随意导出当前数据。
3. 基于副本生成按 ID 排序的稳定页。manifest 记录内容 hash、记录总数、H、权限版本、scope 和到期时间；只有完整校验后标 ready。
4. 消费者下载全部资源页，校验 manifest/hash，原子记录本地 snapshot_id、epoch、H。
5. 从 manifest 的 `resume_cursor` 开始拉 `/changes`，应用 seq>H 的变更。每批将对象变化和游标在消费者本地同一事务提交。
6. 网络失败从最后确认 cursor 重试。按 `(epoch,seq)` 去重，允许重复交付；不要把序列空号视为漏包。
7. cursor 过期、epoch 不符、权限版本变化或资源集合变化，服务明确要求新 snapshot，不能默默从当前时间继续。

快照只导出所选资源在水位H的当前投影；历史revision按专门版本/as_of接口读取。ResourceRecord使用resource_type、resource_id、version_id、payload包装，每资源按resource_id字节序排列、每ID一条。每页page_sha256为响应data数组的RFC8785/JCS规范化UTF-8字节的SHA-256；资源manifest的sha256为按相同顺序将每条ResourceRecord规范化后加一个LF再连接的字节流SHA-256；空资源为零字节SHA-256。change的payload_sha256仅覆盖payload。算法版本固定为`jcs-sha256-v1`，不用客户端JSON缩进/键顺序直接计算。[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)

不允许NaN/Infinity；超出安全整数范围的标识使用字符串。金额等需要精确十进制的事实值使用十进制字符串并携unit/currency，不用浮点自动改写原值；归一数值另外记录。快照的分页不能改变资源hash；消费者完成所有页后校验count/hash再提交本地水位。受权限变化影响的快照立刻失效；服务不得沿用旧导出泄露已撤销内容。

资源名映射固定：快照resources用items/events/entities/topics/sources/analyses/signals/reports/evidence；changes的resource_type用item/event/entity/topic/source/analysis/signal/report/evidence。item即领域document，不新增另一套身份。analysis的version_id指view_version_id。变化载荷例：

```json
{
  "seq": 1205,
  "resource_type": "event",
  "resource_id": "event-demo-old",
  "version_id": "ev-demo-003",
  "operation": "merge",
  "available_at": "2026-09-14T12:00:00.000000Z",
  "payload": {
    "id": "event-demo-old",
    "version_id": "ev-demo-003",
    "status": "merged",
    "canonical_id": "event-demo-survivor",
    "replacement_ids": [],
    "available_at": "2026-09-14T12:00:00.000000Z",
    "reason": "Reviewed reports describe the same event."
  },
  "payload_sha256": "4920b876a11d803f81cce7b192550bff28aee286b5ecc0ebed9cbf6bf6b6bd84"
}
```

操作集合 create/update/withdraw/merge/split/delete。真正删除内容时保留 tombstone 的最小 ID/版本/原因码；删除不能只从列表消失。事件关系、目录更名、已发布分析变化也写 change log。

changes 响应始终提供 `next_cursor`（即使无数据），并提供当前 `high_water` 与 `has_more`。权限过滤时 cursor 推进到扫描过的最高 seq，否则可能反复卡在不可见记录。服务范围变更用新 snapshot；动态 entity/topic 过滤不可作为无状态 changes 过滤，因为实体归属变化会导致漏删除。

首期 change 保留 90 天、snapshot 24h；所有过期时间对外明确。响应长度有界，不使用永不结束的长轮询；空轮询建议指数退避至 60 秒。

## 8. 错误与兼容

错误统一：`{"error":{"code":"...","message":"...","request_id":"...","retryable":false,"details":{}}}`。无原始 SQL、stack、密钥或内部路径。

| HTTP | code | 消费者行为 |
|---|---|---|
| 400 | invalid_cursor/filter_mismatch | 修正请求，不自动无限重试 |
| 401 | invalid_token/expired_token | 更新凭据 |
| 403 | insufficient_scope/restricted_content | 申请相应范围或处理缺失 |
| 404 | resource_not_found | 不认为所有历史对象被永久删除，查看变更/权限 |
| 409 | epoch_changed/version_conflict/snapshot_not_ready | 重建 snapshot 或刷新状态 |
| 410 | cursor_expired/snapshot_expired | 从新快照恢复 |
| 422 | invalid_parameter/unsupported_history | 检查时区、参数组合、支持时间范围 |
| 429 | rate_limited | 遵守 Retry-After |
| 503 | not_ready/temporarily_unavailable | 有限退避，保留原游标 |

新增可选字段允许 minor schema 升级；删字段、改类型、改含义、时间/分页语义变化需新 API major 或有明确迁移窗口。输出枚举变更评估旧消费者兼容性，不把新 enum 值默认为旧值。废弃至少提前一个已记录发布周期，当前个人使用阶段默认 30 天。

允许 ETag/If-None-Match 用于不可变版本；缓存键含权限与对象版本。禁止共享缓存泄露全文。未知 query 参数返回 422，避免拼写错误导致无意全量扫描。

## 9. 契约验收

- OpenAPI 文档校验通过；所有示例与对应 schema 一致；DTO 在运行时校验，不能再返回 `{}` 作为 schema。
- 同时间戳多条分页不重复；晚到、重新评分、主题变更、撤回、合并/拆分能从 change log 观察到。
- 导出期间持续写入：snapshot+changes 与截止高水位的服务端视图逐 ID/hash 一致。
- 两个消费者 token 互用 cursor/snapshot 失败；撤销 token 立即无效；权限变化重新建快照。
- 旧库恢复造成 epoch 变化能被消费者检测，不能悄悄丢数据。
- `as_of=T` 不包含 T 后的新版本/新模型输出/新归并关系；缺证据不会返回 neutral。
- 真实下游消费者以最小权限完成首次导出、断线续传、一次修正和一次撤回测试后，v1 才进入稳定支持状态。
