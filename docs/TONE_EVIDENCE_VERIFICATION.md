# Tone 引用逐字核验（P15b）

P15b 将 `infohub.tone/1.1` 的引用坐标绑定到不可变 raw CAS JSON。新 tone run 必须使用 1.1；1.0
只保留兼容读取，不能伪装成已逐字核验。该模块不运行模型、不回填历史数据、不开放 `valid`，也不改变
Windows 生产环境。

发布 `needs_review` 结果时，系统在写数据库前执行以下检查：

1. evidence_id 已属于该 run 的冻结输入；
2. raw record 存在，media type/encoding 是 canonical `application/json` / `utf-8`；
3. payload_ref 不能逃逸 blob 根目录，文件存在，SHA-256 与 size 均匹配账本；
4. payload 能以 UTF-8 JSON 解析；
5. RFC 6901 pointer 存在且最终指向字符串，数组下标必须为规范十进制；
6. `[start_offset,end_offset)` 使用 Unicode code point，切片逐字等于 quote；
7. target/speaker entity_id 先预检，并在 publication 写事务内再次核对实体目录存在性和类型。

任一步失败，analysis result、publication pointer、change log 和 job completion 都不会写入。成功后
`validation_report_json.task_validation` 记录 validator version、payload SHA、pointer、offset 和 quote SHA，
不复制额外全文。

当前 raw CAS 由采集器保存为 JSON envelope。RSS/API 通常从 `/source_record/...` 定位；generated metadata
从顶层字段定位。pointer 指向的必须是原始 envelope 中的字符串，不能把页面展示的清洗文本偏移冒充 raw
offset。HTML/PDF 正文尚无版本化规范文本坐标系，因此不在本 PR 宣称支持；未来应先生成带 extractor version
和源 hash 的不可变 text artifact，再升级 schema。
