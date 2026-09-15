# 发布账本与恢复代际

本页描述 P04b 已实现的底层契约。领域文档、事件、分析和报告表将在后续 PR 建立。P05 已把
生产调度切到独立 durable worker；现有 crawler、策展与日报 handler 仍是兼容任务，尚未成为
对外发布的版本化领域对象。

## 数据身份

- `dataset_id` 标识同一逻辑 InfoHub 数据集，普通发布、重启和同一连续备份恢复不改变它。
- `epoch` 标识一段可连续消费的变化历史。恢复到已经对外发布过的旧备份、无法证明序列连续，
  或执行破坏连续性的重置时必须创建新 epoch。
- `change_log.seq` 使用 SQLite `AUTOINCREMENT`，在同一数据库中只向前增长；消费者始终同时
  保存 `dataset_id + epoch + seq`，不能只保存整数。
- 数据库记录 `owner_environment_id`。生产库副本若被开发环境直接打开，任务入队和发布会拒绝，
  防止 Mac 测试结果混入 Windows 数据身份。

迁移 schema v3 时为该数据库生成一次 dataset 与初始 epoch。备份会保留二者，`db-verify`、
`db-backup` 输出同时包含当前 identity 和变化高水位。若 schema v2 已有排队或持租约任务，迁移
会把它们原样归入初始 epoch，保留原任务 ID、请求哈希和幂等关系，不留下无法领取的旧任务。

## 唯一发布入口

有对外可见影响的 worker 使用 `app.publication.publish_job_result`。调用前完成网络请求、模型
调用和计算；事务内 `persist` 回调只执行有界数据库写入。发布按以下顺序处于同一
`BEGIN IMMEDIATE` 事务：

1. 核对运行环境、当前 epoch、仍有效的 lease token、job 输入版本和 schedule 配置版本。
2. 用 RFC 8785/JCS 生成变化 payload 的规范字节和 SHA-256。
3. 追加一条或多条 `change_log`，取得 publication sequence。
4. `persist` 写不可变领域版本，并把对应 sequence 保存为 `publication_seq`。
5. 把 job attempt 和 job 一起标为成功。

任一步抛错都会整体回滚。重试同一成功调用会核对原变化后返回既有结果，不再次执行
`persist`。同一幂等键若对应不同资源、版本、操作或 payload 会失败。确实没有领域变化的
任务可以完成，但不能执行内容写入，也不会制造 change 或增加高水位。

`persist` 不得请求外部服务、等待模型或扫描全库。后续领域 PR 必须通过这个入口发布，不能
分别提交内容、变化和任务状态。

## 恢复与 epoch 切换

普通进程重启不切 epoch。连续恢复到从未被更晚状态取代的最新一致性备份，也可保留 epoch。
若恢复会让消费者见过的高水位或对象状态倒退，按下面顺序处理：

1. 停止 worker，确认不会再领取任务。
2. 恢复并验证数据库与对象文件。
3. 运行 `python cli.py dataset-status`，记录 dataset、epoch 和高水位。
4. 用该 epoch 作为乐观锁执行
   `python cli.py dataset-new-epoch EXPECTED_EPOCH "restore BACKUP_ID"`。
5. 验证新 epoch、高水位和运行环境，再启动 worker；消费者必须重建 snapshot。

切换会拒绝仍有有效 lease 的数据库，并取消旧 epoch 中未运行的 pending/retry/blocked job。
已完成的旧变化和检查点继续保留供审计。epoch 切换不是数据库备份，也不能修复缺失内容。

## 知识检查点与时钟

`clock_checks` 保存外部时钟测量证据。只有来源存在、绝对偏移不超过 1000ms、证据在 300 秒内
且时间顺序有效，检查才标为 `verified`；否则为 `suspect` 或 `unknown`。这些阈值来自当前
SPEC，Windows 现场验收后再通过独立 PR 版本化调整。

`create_knowledge_checkpoint` 在短写事务中先读取当前 epoch 已提交的高水位，再记录观察时间和
时钟状态。checkpoint 本身不写 `change_log`，所以不会递归增加高水位。没有合格时钟证据的
checkpoint 仍可用于保守同步，但不能宣称为严格 point-in-time 证明。

## 当前限制

- 本 PR 只提供发布基础，不开放 `/changes`、snapshot 或领域 API。
- 当前旧 crawler、AI pipeline 和页面尚未调用发布事务；它们会在各自领域迁移 PR 中接入。
- 时钟检查记录接口不会自行测量 Windows 偏移；真实采样和告警属于生产验收工作。
- change 保留与清理任务、权限游标和快照文件属于 P19；在此之前不删除 change 记录。
