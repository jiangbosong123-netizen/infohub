# 分析输入清单（P11a）

P11a 在调用规则模型或 LLM 之前冻结一次分析请求。`analysis_runs` 保存主对象版本、任务类型、输出
schema、provider/请求模型、prompt 模板与哈希、渲染输入引用与哈希、pipeline、参数和完整 manifest；
`analysis_inputs` 按顺序保存文档版本、事件版本及其原始证据。

所有输入都必须引用已存在的不可变版本。原始证据只能通过实际包含它的文档版本进入清单，主对象版本
也必须出现在输入闭包内。准备 run 时核对 durable job lease 和 input_version；相同幂等键仅允许完全相同
的请求重试。run 与输入写入后由 SQLite trigger 禁止修改或删除。

本阶段只支持 document/event 主对象。报告任务所需的 `report_input_snapshots` 在报告版本阶段建立后再开放，
不能用尚未生成的报告输出反向充当模型输入。本阶段也不调用模型、不记录 attempt、不发布分析结果；这些
分别属于 P11b 与 P11c。prompt 正文可保存到受控 CAS，数据库只保存引用和哈希。
