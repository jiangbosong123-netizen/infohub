# NLP 评估数据

`datasets/foundation-v1` 是 12 条中英文合成工程样本，只验证 manifest、内容哈希、分组切分和标注状态工具。
它不是模型质量 gold，不得用其计算或宣传准确率。生成的 `report.json` 明确记录相对正式计划仍缺 588 份
文档、143 个事件组、300 条 impact 标注和 48 条安全样本。

真实数据集必须按事件组和原始出处分组，确保转载、翻译、同一公告和同一事件不跨 train/dev/test。
受限正文只保存对象引用与 hash，不提交 Git。关键标签需要两名标注者与裁定；当前只能单人标注时状态为
`single_annotator`，impact 只能标 experimental。模型预标注不能变成 `adjudicated` gold。

验证命令：

```bash
python -c "from app.evaluation import validate_evaluation_dataset as v; print(v('evaluation/datasets/foundation-v1').to_dict())"
```
