# 来源时间契约实现（P06b）

P06b 保存来源声称的原值、语义和解析结果，不再用一个 `published_at` 混合发布、更新、SEC 接收、
申报日期、报告期间和系统首次看到时间。每个结果追加到不可变 `source_time_values`，关联具体
`raw_record`，并记录 `source-time-v1` 规则与实际 tzdb 版本（系统无法报告版本时明确为
`system-unknown`）。同一原始记录可并存不同规则或 tzdb 版本；相同规则和 tzdb 重放必须得到
相同结果。

## 逐来源规则

| 来源 | 原字段 | 角色与规则 |
|---|---|---|
| RSS | `entry.published`, `entry.updated` | 分别保存 published/updated；只有 updated 时 published 明确 missing，不提升为首次发布 |
| Google News | `entry.published` | `other`，解释为聚合入口报告时间，不冒充发布者核实时间 |
| SEC | `acceptanceDateTime`, `filingDate`, `reportDate` | accepted、filing_date、report_period 分离；naive acceptance 不猜 UTC/ET；日期保存日历精度且不造瞬间 |
| HKEX | `DATE_TIME` | 固定 `Asia/Hong_Kong` 和分钟精度，不读取门户 `APP_TZ` |
| 财联社 / 华尔街见闻 | `ctime` / `display_time` | 来源规则明确为 Unix 秒，不按数值大小猜秒或毫秒 |
| 新浪 7x24 | `create_time` | 固定 `Asia/Shanghai` |
| 无时间 HTML 列表 | 无 | 保存 `missing`，不声称页面抓取时间是发布时间 |

带 offset 的输入按原 offset 转 UTC。无 offset 且无已确认来源时区的值标 `missing_timezone`。
DST 回拨重复小时标 `ambiguous_local_time`，春季不存在小时标 `nonexistent_local_time`；日期自然
区间可因此为 23 或 25 小时。超过观察时间十分钟的 published 值保留原 UTC 并标
`future_suspect`，不改写证据。

连接器在完整取得每个 entry/record 时写 `observed_at`，批次结束时间不代替它。JSON API record
和解析后的 RSS entry 以稳定 JSON envelope 写 CAS，分别标 `api_record` / `feed_entry`；HTML
列表当前只有 `generated_metadata`，不能宣称保存了发布方正文。

## 兼容边界

门户仍读取旧 `items.published_at NOT NULL`。缺失、不可信或仅 updated/accepted 的来源时间，在
legacy 投影中仍暂用抓取时间维持页面排序；可信原值和解析状态只在新表中，绝不能把 legacy
fallback 用于历史研究。新写入会在 `items.extra._legacy_time_basis` 标明
`source_published`、`connector_observed` 或 `item_inserted`。P06c 的 nullable document version 只从
同一 raw record 上有效的 published 证据生成正式投影；旧数据仍等待 P07 标记和回填。本阶段不重写
历史 items，也不把 legacy fallback 提升为来源事实。
