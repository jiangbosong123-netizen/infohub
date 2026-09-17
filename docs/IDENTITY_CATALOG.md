# 版本化身份目录（P08a）

P08a 在旧 `companies`、`topics` 和发布方识别逻辑旁建立只追加的影子目录。门户和现有事件算法仍读取旧表；
这一阶段不切换展示、不批量重写 `item_companies`/`item_topics`，也不把旧关键词升级成已核实事实。

## 实体

`entities` 保存稳定身份，`entity_versions` 保存名称、类型、状态和属性的历史版本。旧 company 通过
`legacy_company_entities` 一对一映射；名称变化只追加版本，稳定 entity ID 不变。

旧 watchlist 的 `aliases` 混有公司名、人物名、产品名和宽泛关键词，因此统一导入为
`match_mode=candidate_only`、`ambiguity=unreviewed`。只有公司中英文正式名称进入可精确匹配别名，且同名会标
`ambiguous`。候选别名不得直接发布 entity mention；P08 后续识别流程必须保留 unresolved/ambiguous。

旧 ticker、CIK 和港股代码保留为 `legacy_unverified` identifier。US ticker 在没有交易所和有效期证据前使用
`legacy_ticker` namespace；不会生成正式 `exchange_ticker` 或 `security_listings`。P08b 将按 SEC 单来源样本
建立 issuer、security、listing、ADR/多股类及表单修订语义。

## 主题

`topic_catalog` 是稳定身份，`topic_versions` 保存 slug、名称、分类、描述和规则版本；
`topic_slug_aliases` 永久保留可解析的历史 slug。主题改名时在 `config/topics.yaml` 对新主题增加：

```yaml
previous_slugs: [old-slug]
```

同步会在同一 topic ID 下追加新版本并同时保留新旧 slug。规则变更不会覆盖旧版本。当前 legacy
`item_topics` 仍是页面投影；正式 `document_topic_assignments` 只追加，留给后续分类 PR 写入。

## 发布方、来源与出处

`publishers`/`publisher_versions` 保存稳定发布方，已知域名和名称分别进入 `publisher_domains`、
`publisher_names`。`sources` 仍表示采集入口；publisher 表示刊登者；`document_attributions` 预留
original/syndicated/cites/unknown 关系。未知 Google News 发布方暂不自动创建可信目录身份，也不据此声称来源独立。

从源配置移除 source 会软停用旧行；以后重新加入同 key 会恢复 `enabled=1`，保留原 source ID 和健康历史。

## 同步、验证与回滚

`prepare-release`/`init-db` 在旧公司、源和主题同步完成后执行 `sync_identity_catalog`。重复同步不新增版本；
只有目录内容变化才追加版本。schema 8 迁移只建空表，目录回填发生在显式配置同步中。

回滚时旧门户继续读旧表；停止身份目录同步即可。新 entity/topic/publisher ID、版本、别名和映射必须保留，
不能删除后重新生成。P08a 不创建正式证券上市关系，也不修改现有文档、公司或主题归类结果。

2026-09-17 的本地一致性生产副本演练建立 23 个 company entity、56 个 topic 和 11 个已配置
publisher；重复同步新增身份和版本均为 0，13 个 US company 全部有 organization 映射，旧
company/source/topic 指纹未变，完整性与外键检查通过。机器可读结果见
[P08a 演练证据](evidence/p08a-identity-catalog-rehearsal.json)。这仍不是新 Windows 导出副本验收。
