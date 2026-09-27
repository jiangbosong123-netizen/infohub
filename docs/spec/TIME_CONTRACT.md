# 时间、历史可知性与市场日历契约

状态：目标规范，尚未实现。适用于所有采集器、数据库版本、NLP、人工修正、API 和门户；与 [数据模型](DATA_MODEL.md)、[API](API_CONTRACT.md) 共同约束后续 PR。本文区分“数据声称何时发生”“本系统何时取得”“消费者何时真正收到”，不把时间格式规范等同于时钟准确。

## 1. 必须保存的时间

| 字段/记录 | 含义、写入责任 | 不允许的替代 |
|---|---|---|
| source_time_values | 每个来源时间字段的路径、原值、语义、解析规则、时区和结果；connector 保存原值，normalizer 解析 | 不用解析后的时间覆盖原始值 |
| published_at / published_range | 发布者可核实的公开发布时间/精度区间，可 NULL | updated、接受申报、抓取、数据库创建时间均不能默认充当首次发布 |
| source_updated_at | 来源声称的更新时间；存于 source_time_values 的 updated 角色 | 不自动改变文档首次发布或首次看到时间 |
| source_accepted_at | 申报接收系统接受时间；存于 accepted 角色 | 不等同实际公开传播时间 |
| filing_date / report_period | 申报日期与报告所属期间；保留 date/区间与原值 | 不是新闻发生瞬间，不是可消费时间 |
| observed_at | connector 完整收到这个原始载荷的本机 UTC 时间；raw_observation 记录 | 不推迟到整批公司抓完、入库时才统一补记 |
| ingested_at | raw_record 持久化事务的记录时间；storage 写入 | 不当作精确物理 COMMIT 瞬间 |
| first_seen_at | 此 document/event 在本数据集首次登记时间，创建后不回拨 | 历史回补、更早报道、改标题不提前这个时间 |
| normalized_at | 本次规范化完成时间；新解析规则产生新版本 | 不更改旧版本的处理时间 |
| event_time | 事实发生、宣布、生效或计划执行区间，带角色与精度 | 未来计划日期不是“新闻发布时间异常”；财报期间不等同发布日 |
| started_at / analyzed_at | 本次模型任务开始/完成时间 | 不能把今天重分析的时间写成去年发布时间 |
| available_at | 当前不可变版本的应用发布事务时间戳，语义为 transaction_recorded；只在提交成功后对外可读 | **不是精确的物理提交/公网首次出现/消费者接收时间** |
| checkpoint.observed_at | 读到已提交高水位 H 后记录的 UTC 时间，见第 4 节 | 不能在发布事务提交前制作“已可见”证明 |
| consumer_received_at | 消费方完整收到结果并持久化时自行记录 | InfoHub 无法替消费者声明实际收到了什么 |
| as_of | 本次选材的知识截止时间 | 不等同新闻日期、报告展示日期或任务开始时间 |

原始时间字段用 `source_time_values[]` 统一保存，角色为 published/updated/accepted/filing_date/report_period/other；每项包含 `field_path,raw_value,role,timezone,utc,precision,interpretation,status`。原值未知时保留 NULL，不能编造字符串。`published_at` 是此集合中获准公开发布时间的投影，须能定位其原始字段；分歧必须有解释，不能同时存在两套独立真值。

`filing_date` 保持 ISO date，报告期间另存结构化起止与来源定义；季度/财年不能用固定 90/365 天推算。API 原始日期项可通过 raw_value + precision 表达，`utc=NULL`；结构化报告期间进入版本化事实。时间证据不足不妨碍阅读，但会限制历史分析用途。

## 2. 解析、精度与时区

1. 持久化 UTC 比较值固定为 `YYYY-MM-DDTHH:mm:ss.ffffffZ`，数据库范围查询不得混排旧 `+00:00`、本地时间与不等长字符串。精度字段描述输入知识，6 位小数只是存储格式。
2. 带 Z/offset 的值按其显式偏移解释并保留原值。发现来源字段与官方语义矛盾时标 suspect、保留证据、版本化修订规则，不能擅自将 Z 改成美东时间。
3. 无 offset 的值只在有明确来源规则时指定 IANA 时区；未知则 utc=NULL、status=missing_timezone。禁止由 Windows、Mac、Docker 或 `APP_TZ` 的默认值决定。
4. 来源时区、门户展示时区、报告统计时区是三个配置。HKEX 本地披露时间用 Asia/Hong_Kong；经确认的美东本地时间用 America/New_York；不能固定写 UTC-5。
5. DST 回拨的重复小时要求 offset/fold 或来源证据，否则 ambiguous_local_time；春季不存在的时间为 nonexistent_local_time。不得静默选择一个值。
6. 只有日期/月的值以源时区自然期间 `[start,end)` 保存范围，published_at=NULL。一天可能是 23/25 小时；缺时区时连 UTC 边界也未知。minute 精度允许保存分钟起点，但必须返回 precision=minute，历史可知性仍用系统时间。
7. 无值、非法值、疑似未来值、历史不确定分别标记。未来发布超出来源容差（默认 10 分钟）记录 future_suspect，保留解析结果但不用于可信发布时间排序；绝不改成 now。若确属 embargo/计划发布，保存对应角色。事件计划时间可合法在未来。
8. 发布时间与抓取时间可能因延迟、来源时钟偏差而逆序，不能为了满足排序修改证据。来源层检测偏差，系统处理阶段的因果顺序由事务序列与任务依赖验证。
9. 每个规范化版本保存 normalizer_version、time_rule_version、tzdb_version；旧载荷重放不改旧值。tzdb版本不能取到时填unknown，不伪造；需要严格日历验证的批次不放行为 verified。

## 3. 当前来源的具体规则与缺口

### SEC

目前使用 acceptanceDateTime 生成 published_at，仅保留 form/cik 辅助字段；每公司只取 recent 前 40 条。应首先保存 accession、CIK、form、primaryDocument、acceptanceDateTime、filingDate、reportDate 及原 API record，随后按字段语义分别解析。

接收、归属申报日、公开传播并不相同；SEC 官方说明，多数美东 17:30 后提交的文件会归入下一工作日且推迟公开传播，同时存在表单例外。[SEC 申报状态说明](https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/determine-status-my-filing)

所以，不能仅凭 acceptanceDateTime 断言“市场当时已经得到文件”。只有来源明确提供公开发布时间或经验证的来源规则时才填写 published_at；否则保持 NULL，在门户显示“SEC 接收时间”并附系统首次收到时间。本文未验证全部 SEC 输入格式及每种表单的延迟规则，不把任何 naive 值推定为 UTC/ET。后续 PR 需冻结真实脱敏格式样本及规则依据。

来源本身支持更长历史提交文件，现有 recent[:40] 是采集器的截取；不能把最近 40 条全部抓完标成历史完整。[SEC API 文档](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

### RSS / Google News / HKEX / 快讯

- RSS published 和 updated 分开；只有 updated 的条目可收录，但 published_at=NULL，不把更新当首次发布。
- Google News 中的时间是聚合入口报告的时间；应标明来源角色。尚未抓取发布者原页时，不声称已核实发布者时间。重复对账不能回拨 first_seen_at。
- HKEX 解析时区固定由源协议决定，不能随 APP_TZ 改变。公告日与报告期间分别保存。
- 快讯含 Unix 秒/毫秒时按来源字段规则解析，禁止按数值大小无限猜测；有效空响应与缺失字段分开。每个采集器均需缺时区、错误日期、更新和迟到样本。

## 4. 历史查询与可知性，避免虚假的精确保证

### 4.1 逻辑历史视图

`available_at` 在写事务中取得，版本、关系、当前投影和 change_log 原子发布。因 SQLite 不能在提交之前预知真实提交的墙钟时刻，这个字段只表示应用事务时间，不能单独证明某微秒前已对外可读。长事务和时钟回拨要告警，禁止仅在文案中把它称为精确提交时间。

普通 `as_of=T` 是**逻辑历史视图**：选择 available_at<=T 的版本及该时点有效的关系/人工操作。结果引用的完整输入闭包也必须满足此约束；分析可以晚于选材截止完成，因此它的 available_at 不能继承输入的旧时间。该查询不承诺微秒级交易可用性，也不能凭格式规范把记录标成历史合格。

### 4.2 严格历史使用：已提交知识检查点

持久化 `knowledge_checkpoints(id,dataset_id,epoch,high_water,observed_at,clock_status,clock_check_id)`：独立读事务固定快照，读取已提交 change_seq 最大值 H，随后记录该观察的 UTC 时间与时钟证据，结束读取并持久化检查点。H 之后的新提交不会进入这个检查点；检查点记录自身不是领域变化，不递归增长 H。发布后每批/最长 30 秒尝试建立检查点，崩溃未记录只造成保守延迟，不倒填检查点。

- `knowledge_checkpoint_id` 作为可选的历史读取约束；与 as_of 同用时要求该检查点 observed_at<=as_of、epoch匹配、clock_status=verified，否则422。有效视图先限制 seq<=H，再按时间筛选；关系、发布决定、实体和证据全受约束。
- 无 as_of 时检查点自身的 observed_at 为知识截止；与 version_id 不能同用。历史列表的 cursor 必须绑定检查点；跨页不能偷偷切成当前快照。
- `point_in_time_eligible` 是版本化的**数据质量资格**：取得时的时钟证据与输入/关系溯源完整才允许true，legacy/缺失/时钟未知为false；它不代表本次查询已经执行严格PIT。消费者必须同时满足返回的知识检查点约束和所用记录的资格。该字段不随读取方式变值，修订资格须发布新投影版本/change。这样snapshot、changes与同版本payload保持同一内容/hash。
- 实际严格可用条件为：检查点已核验且observed_at<=截止、版本及全部依赖seq<=H、资源质量资格合格；这些只证明InfoHub已存在数据，不证明外部消费者已收到或真实市场参与者可访问。HTTP响应外层`knowledge_cutoff`说明读取上下文，不能混入不可变资源payload。
- 恢复旧备份后切换 epoch；旧检查点在当前 epoch 请求返回409要求重建，历史归档仍保留其原 dataset/epoch，不伪装成新流。
- 已提交观察点可从同步 snapshot 的 `knowledge_cutoff` 取得。长期研究保存检查点和固定 manifest；内容受保留/授权限制无法重放时明确历史不可用，不能回退到最新值。检查点元数据与被引用的版本长期保留，不依赖 90 天 change_log。每个版本/关系保存 publication_seq 以供日志清理后的历史查询。

严格样例：新闻声称 09:00 发布，09:07 首次收到，09:09 模型完成，09:10 检查点才观察到分析。09:05 的知识中没有这篇文档；09:08 可能有文档但没有分析；09:10 检查点才能证明分析已经可见。今天根据补录材料重算旧日报，产生“重述”版本；不得替换旧日报或归入过去的实时输出。

### 4.3 时钟健康与消费者

记录宿主/worker 的 UTC 时钟同步状态、测量时刻、偏移估计和测量来源；无测量为unknown。初始 verified 门槛为可用同步证据且绝对偏移估计<=1秒、证据不超过5分钟，后续用Windows现场测量调整并版本化。该门槛只允许秒/分钟级研究，不构成低延迟交易保证。

运行中时钟后跳、偏移超阈或同步状态不明时仍可保留原始采集，clock_status=suspect/unknown，暂停发放verified检查点和正式历史合格信号。不得修正历史 observed_at 掩盖事故。seq 负责提交顺序；monotonic clock 负责任务耗时，UTC墙钟负责可解释时间。lease超时/重试受时钟异常影响的恢复步骤见运维，应防止异常时自动抢占形成双执行。

消费者持久化 dataset/epoch/seq、输入manifest、consumer_received_at 和自身时钟状态。仅使用它实际收到的范围，不能把后来下载的旧文件当作当时收到。

## 5. 美股交易日、报告窗口与展示

- company、security、listing、交易场所/日历不得混为一字段。文章可关联美国上市实体，但公司也可能在多个市场上市；美国上市不等于注册地美国。
- 首期按美国主上市场常规时段解释数据时，需指定交易场所及 calendar_version。常规时段通常为美东 09:30–16:00，但休市、提前收盘、盘前/盘后及场所差异必须由带生效日期的日历处理。[NYSE 日历](https://www.nyse.com/trade/hours-calendars)、[Nasdaq 日历](https://www.nasdaq.com/market-activity/stock-market-holiday-schedule)
- 美东 09:30 在冬季为 14:30 UTC、夏季为 13:30 UTC。门户可显示伦敦/北京用户时区，同时在详情显示原时区和UTC，不改变底层数据。
- source交易日标签与系统按日历推导的交易日分开。只有日期或歧义时间时 session=unknown；不得为方便聚合归入开盘。
- 官方已发布未来时段变更，例如 Nasdaq 公告的 2026-12-06 起夜间时段；2026-09 的历史数据不能提前套用。这里仅据其说明日历要版本化，不代表当前系统已经支持夜盘。[Nasdaq 公告 ETA2026-46](https://www.nasdaqtrader.com/TraderNews.aspx?id=ETA2026-46)
- 保留当前自然日日报作为一种 report_type；未来“美股交易日情报”另定 report_type。每个报告/信号的 input manifest 固定 `window_start,window_end,window_basis,timezone,calendar_id,calendar_version,as_of,knowledge_checkpoint_id`；非交易日窗口 calendar_id/version=NULL。
- `window_basis` 为 published/event/first_seen/fact_change 之一。发布、获知、事实变化不是同一分桶；沿用情绪规范的 fact_change 默认值时不得改成文章数量。日期不明的记录进入未定位栏并计数，不静默丢弃。
- earnings发生时间、计划财报日、报告季度、公告修订时间分别建模。第一次数据与修订后数值分别版本化；预期值只有可追溯外部来源才保存，不让LLM补造。

## 6. 迁移、验收与已验证边界

历史原时间字符串保留在 legacy 引用/迁移清单；现有 published_at 可能由 now 回退或 updated 产生，不能只格式化成Z就标verified。映射旧字段时保留time_status=legacy_unverified、availability_basis=legacy_unknown，point_in_time_eligible=false。新发现的原始发布证据创建新版本和现在的可用时间；不改旧版本。

[时间验收案例](evidence/time-contract-cases.json) 是冻结的合成预期，不是业务代码已通过测试的声明。后续P04/P06/P18/P19/P20/P21/P23必须实现相应断言，覆盖：明确偏移、无时区、DST重复/缺失小时、23/25小时日、日期精度、RSS更新、SEC各时间、未来计划、晚到/重跑/人工改正、长事务、时钟回拨、检查点、消费者延迟、交易日历版本。

当前代码已离线复现的缺陷见 [us-time-review.json](evidence/us-time-review.json)：同一SEC无时区值随宿主TZ产生不同UTC；RSS仅有updated时仍生成published_at。未证明目前每条生产SEC时间都错，也未测得Windows时钟异常。本SPEC修的是可复现的逻辑与缺失保证，不能将未测风险叙述成已发生事故。
