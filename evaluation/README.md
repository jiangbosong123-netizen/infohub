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

标注定义见 [ANNOTATION_GUIDE_V1.md](ANNOTATION_GUIDE_V1.md)。分类基线报告必须同时输出 confusion、逐类 support、macro F1、coverage、abstain、missing 和 Wilson 95% 区间。

## 真实候选抽样

`app.evaluation_sampling` 以只读方式扫描现有数据库，按来源、语言、文档类型、季度和官方属性进行
确定性分层抽样。输出只有本地对象引用、内容 hash 和抽样元数据，不包含标题、正文、摘要、URL
或数据库路径。输出目录必须保存在私有运行目录中，不提交 Git：

```bash
python -m app.evaluation_sampling \
  --database /absolute/private/path/app.db \
  --output /absolute/private/path/evaluation-candidates-v1 \
  --target 600
```

`report.json` 用于检查中英文、来源、SEC/美股、事件组和困难负例覆盖缺口。候选仍是
`unlabeled`；语言、类型、公司和困难负例均为抽样启发式信息，不能作为模型质量真值。

Mac 隔离副本上的 600 条真实候选抽样已验证命令与数量分层，汇总记录见
[`p24-curation-evaluation-rehearsal.json`](../docs/evidence/p24-curation-evaluation-rehearsal.json)。
其中英文 352、中文 248、美股关联 247、SEC 相关 67；这只满足候选抽样数量，
没有人工标注、裁定、泄漏复核或模型指标。候选对象引用保存在私有运行目录，不进入 Git。

## 候选入册与人工复核

先用 `app.evaluation_admission` 将已冻结的候选清单变成**私有、未标注**数据集：

```bash
python -m app.evaluation_admission \
  --plan /absolute/private/path/evaluation-candidates-v1 \
  --output /absolute/private/path/evaluation-admitted-v1 \
  --dataset-version mac-isolated-unlabeled-v1 \
  --database /absolute/private/path/app.db
```

输出目录必须为空，版本不得原位覆盖；若放在仓库内，只允许被 Git 忽略的
`evaluation/private/`。入册保留对象引用、内容 hash、来源和抽样分层元数据，不导出正文。
传入 `--database` 时只读重算抽样快照和每条候选，来源改变会阻止入册；不传时
入册报告明确标为未验证，数据集不得成为可发布 gold。
事件组、原始出处组、相同内容哈希和同一文档形成连通分量后整体切分，以免它们横跨
train/dev/test。当前分配为确定性的 70/15/15 近似比例；**转载、翻译和公告修订的
自动识别仍不完整**，人工必须复核分组，并单独指定时间及来源盲测。修正分组需创建
新数据集版本，不得在旧版本原地修改。

初始 `cases.jsonl` 的每个 `annotation` 均为 `unlabeled` 且 `labels={}`。
正式裁定记录须附两份独立人工 review（不同 `reviewer_id`、`source=human`、
`independent=true`、各自的 `labels`、`recorded_at` 和当前 `content_sha256`），
再附第三人的 `adjudication`（不同 `adjudicator_id`、最终 `labels`、时间和同一 hash）。
最终 `annotation.labels` 必须等于裁定标签。校验器拒绝缺少复核轨迹、模型预标注冒充
人工裁定、合成正文冒充真实 gold、以及把目标数调小来提前通过。身份字段由人工填写，
代码只能检查结构和一致性；流程负责人还须在私有标注记录中核查身份、独立性和证据。

Mac 真实候选的入册演练见
[`p25-evaluation-admission-rehearsal.json`](../docs/evidence/p25-evaluation-admission-rehearsal.json)。
该批 600 条仍全部未标注；impact 还缺 300 项人工标签，security 还缺 50 条独立样本，
不能用于报告 NLP 质量或解除生产发布门槛。
