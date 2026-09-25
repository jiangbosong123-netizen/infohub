# SEC 身份与申报语义

这一层把 SEC 抓取结果投影成可追溯的发行人、证券、上市关系和申报身份。门户仍读取旧
`items` 表；本阶段不切换页面、不增加关注公司，也不回填历史 SEC 条目。

## 来源边界

- CIK、ticker 与 exchange 来自 SEC 的
  [`company_tickers_exchange.json`](https://www.sec.gov/files/company_tickers_exchange.json)。
- 申报元数据来自 SEC submissions JSON。原始 `filing`、`issuer` 和当次使用的
  ticker/exchange 关联行一起进入 CAS，投影必须与这份原始证据逐字段一致。
- SEC 明确说明 ticker 关联文件会定期更新，且不保证覆盖范围或准确性。因此目录中的这些
  标识和上市关系状态是 `candidate`，不是 InfoHub 独立核验后的 `verified`。
- SEC 关联文件不能单独证明某证券是 ADR、普通股或存托凭证。`listing_type` 保持
  `unknown`，直到后续 PR 引入能够支持该判断的明确证据。系统不得根据公司国籍、表单类型
  或 ticker 猜测 ADR。

## 稳定身份

`legacy_company_entities` 继续提供关注公司到组织实体的稳定映射。SEC 投影只为该组织追加
带证据的 CIK assertion，不替换公司目录当前版本，避免静态配置同步与 SEC 名称之间来回覆盖。

证券身份使用 `(CIK, exchange, ticker)` 作为来源内稳定键，记录在
`sec_security_keys`。同一 CIK 的多个 ticker 会得到不同的 security entity，例如 GOOG 与
GOOGL；它们通过 `issues` 关系指向同一发行人。重复抓取返回原身份，不重复创建实体、关系或
上市记录。如果同一个稳定键后来指向另一个发行人，事务失败并保留原数据，等待人工审查。

## 申报与修订

`sec_filings` 使用 `(dataset_id, CIK, accession number)` 作为稳定身份。accession number
在这里是 EDGAR 记录定位键，不被解释为“已经通过监管审核”。每次可观察变化追加一个
`sec_filing_versions`，并引用：

- 对应的不可变 `document_version_id`；
- 对应的 CAS `raw_record_id`；
- form、base form、filing date、report period、accepted time 与 items；
- 修订关系的当时判断。

`/A` 表单只在同一发行人、同一 base form、同一 report period 下存在唯一原始申报时建立
`amends_filing_id`。没有候选时为 `unresolved`，多个候选时为 `ambiguous`。如果修订先到、
原始申报后到，系统追加一个 `linked` 版本，保留之前的未解析版本，不覆盖历史判断。

6-K、20-F、40-F 及其 `/A` 版本使用明确的外国发行人描述。它们仍是申报文档，不等同于
新闻事件，也不证明证券类型。

## 事务和失败行为

SEC 投影与旧 `items` 兼容写入、原始证据校验、文档版本写入处于同一数据库事务中。以下任一
情况会让该候选整体回滚，并由现有 ingest run 记录为 rejected：

- 公司尚未同步到稳定身份目录；
- 规范化字段与 CAS 中的 SEC 原始记录不一致；
- 同一 SEC 稳定键指向冲突身份；
- 原始载荷丢失或哈希不匹配；
- 外键、版本链或修订约束失败。

迁移 9 只创建空表和约束，不读取网络、不修改旧公司、旧条目或旧目录记录。生产发布仍必须
按现有流程先做一致性备份、迁移副本演练和严格验证。历史 SEC 记录回填属于后续独立 PR。

## 当前验收范围

- 固定测试覆盖同一 CIK 的多股类、6-K、20-F、20-F/A、重复抓取和修订晚绑定。
- 固定测试证明无 ADR 证据时保持 `unknown`，不会把猜测提升为事实。
- 数据库验证检查 SEC current-version 指针和 amendment link 的完整性。
- 本阶段没有调用生产 SEC、没有修改生产数据库、没有扩大当前 13 家美股公司范围。
