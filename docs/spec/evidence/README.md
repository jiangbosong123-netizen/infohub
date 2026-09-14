# 审查证据、复现方法与限制

采集日期：2026-09-14 UTC；文档整理跨至英国当地9月15日。来源是代码、GitHub只读元数据、本地SQLite一致性快照、临时库实验和Windows HTTP。本目录不包含新闻原文、数据库文件、模型密钥或生产凭据。

## 1. 版本与阅读范围

- 生产/主分支基线：`88a2a1ef16e521b03b0a12170f11f43a3ab0a2a3`。
- 未合并PR #1：`b18d08130fe28f12c49833ad93a3fef322b73ff0`，见[GitHub PR](https://github.com/jiangbosong123-netizen/infohub/pull/1)与[保存的元数据](pr1.json)。
- 部署管理器：独立仓库`windows-server-manager@1f12177`；审阅README、开发部署工作流、auto-deploy.ps1与server-manager.ps1。不在本次修改该仓库。
- 文档在独立`codex/architecture-spec`工作树编写；原InfoHub工作树保留在PR #1分支。

阅读覆盖：app全部Python模块（config、database、company_match、provenance、topics、stories、ranking、crawler、AI、web routes及PR1 api/audit）；全部模板与CSS；cli.py；watchlist/topics/source配置；tests；README、旧SPEC、全部现有docs；Dockerfile/compose/env示例/依赖/ignore/launchd/CI；Git提交演进与PR1完整diff及CI结果。未读取或打印真实.env；没有调用付费模型或新增采集。

Git历史检查不仅看最终文件，也核对了从旧clusters到持久stories、发布方归一、精选去重、健康接口、收藏功能和部署管理器独立的提交，避免将历史设计当成当前实现。

## 2. 证据清单

| 文件 | 方法 | 适用范围 |
|---|---|---|
| [local-profile.json](local-profile.json) | source只读连接→SQLite backup到内存→SQL/结构检查 | Mac app.db某一时刻；11,050条；不是当前Windows导出 |
| [migration-file-profile.json](migration-file-profile.json) | 同方法 | Mac早先app.windows-migration.db；10,241条；文件名不能证明新鲜度 |
| [profile.py](profile.py) | 可复现SQL/JSON/时间/关联/完整性检查 | 无app导入、不加载.env；适合收到生产副本后运行 |
| [probes.json](probes.json)、[probes.py](probes.py) | PR1代码+临时库+mock | 重分析覆盖、同URL更正、健康、游标、主题计数、坏响应、迁移失败等 |
| [pr1-migration-rehearsal.json](pr1-migration-rehearsal.json) | 两份本地库副本init两次、全行摘要和完整性 | 首轮实验记录；仅PR1正常初始化，不是目标SPEC迁移 |
| [migration_rehearsal.py](migration_rehearsal.py)、[再次复现](pr1-migration-rehearsal-reproduced.json) | 将相同方法整理为可运行脚本后再次执行 | 复现采用按行JSON排序再hash；与首轮摘要序列化方式不同，hash不能跨文件直接比较，只比较同次before/after |
| [windows-health.json](windows-health.json) | 对已知私网地址GET /api/health | HTTP服务报告的版本/计数；不是直接数据库读取 |
| [windows-route-smoke.json](windows-route-smoke.json) | curl并发2、单请求超时8秒、9条路径 | 8条页面200、v1接口404；不是视觉/交互/吞吐测试 |
| [pr1.json](pr1.json) | GitHub CLI读取PR和CI元数据 | 当时OPEN、head SHA和checks；不代表架构获批准 |
| [validate_docs.py](validate_docs.py) | OpenAPI校验、JSON Schema例子/负例、链接锚点、证据语法 | 文档自洽检查；不代表API已经实现 |

HTTP最初使用urllib的批量尝试有超时，随后采用有界curl测试得到保存结果；没有由一次成功响应推断网络永不失败。HTTP响应时长包含私网链路，不用来验证服务端p95目标。

## 3. 运行测试

已执行项目既有离线测试：

| 版本 | 命令 | 结果 |
|---|---|---|
| main@88a2a1e | `.venv/bin/python -m unittest discover -s tests -v`，工作目录为main工作树 | 43/43通过 |
| PR1@b18d081 | 同命令，工作目录为PR1工作树 | 46/46通过 |
| PR1 GitHub CI | Python 3.11/3.12 regression + Docker build | 元数据中均SUCCESS |

本机实验运行时为Python 3.9.6、SQLite 3.51.0；这是复现环境事实，不是新生产版本建议。正式支持以Python 3.11/3.12 CI为起点，生产运行时版本仍需读取。测试没有真实模型质量指标，也没有Windows断电、恢复或模型预算演练。

## 4. 复现方式

先取得对应固定提交的独立checkout，并在一次性虚拟环境安装其requirements。以下命令中的路径均为占位；原数据库以只读方式打开，实验修改只发生在临时副本。

```bash
python docs/spec/evidence/profile.py /absolute/path/to/database-copy.db
python docs/spec/evidence/probes.py /absolute/path/to/checkout-of-b18d081
python docs/spec/evidence/migration_rehearsal.py /absolute/path/to/checkout-of-b18d081 /absolute/path/to/database-copy.db
```

文档校验使用独立临时虚拟环境，不更改项目requirements：

```bash
python -m pip install 'PyYAML>=6,<7' 'jsonschema>=4,<5' 'openapi-spec-validator>=0.7,<0.9'
python docs/spec/evidence/validate_docs.py
```

本轮版本：PyYAML 6.0.3、jsonschema 4.25.1、openapi-spec-validator 0.7.2。校验器需要这些工具但InfoHub运行时不新增依赖。证据脚本是离线审查辅助，不是生产迁移命令。

## 5. 明确未验证的事项

- Windows SSH BatchMode认证被拒绝，未获取最新生产数据库、Docker/WSL配置、硬件、磁盘、任务计划、日志与备份；不要求用户把密码发到对话中。
- `app.db`与历史迁移文件均不是当场从Windows导出的新快照；本地旧serve进程使Mac数据仍增长，不能跨采样时间强行对齐计数。
- 非空raw_summary不等于全文；缺失/空串比例与源类型、旧迁移历史有关。宏观关键词筛出的116条是待审候选，不是116条已确认误判。
- 85.41%单篇story不构成聚类召回率；没有人工真值。没有实测tone/impact准确率或置信校准。
- A23部署失败后不重试来自代码控制流分析，没有在Windows故意构造事故。
- 全部页面200不保证所有点击/移动布局正确；本轮没有进行生产写操作或视觉回归。
- 最新目标架构的迁移尚未实现；既有PR1正常路径通过不能用于批准它，更不能批准未来迁移。

## 6. 外部设计依据

- [AIHOT主题页](https://aihot.news/topics)：本轮可见主题分组与浏览形态；未取得其后端源码或数据模型，产品参考不构成架构正确性证据。
- [SQLite backup](https://sqlite.org/backup.html)、[WAL](https://sqlite.org/wal.html)：一致性副本与同主机WAL边界。
- [Python sqlite3事务](https://docs.python.org/3.12/library/sqlite3.html#transaction-control)：显式迁移事务设计依据。
- [Docker重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)、[Docker WSL](https://docs.docker.com/desktop/features/wsl/)：引擎/容器恢复与存储部署边界。
- [APScheduler 3.x用户指南](https://apscheduler.readthedocs.io/en/3.x/userguide.html)：当前进程内调度持久性限制的参考；本轮主要结论仍以实际cli配置为准。

现有AIHOT_ALIGNMENT中的2026-09-12 GitHub公开检索结论保留其历史日期，不假装本轮重新穷尽了所有仓库。
