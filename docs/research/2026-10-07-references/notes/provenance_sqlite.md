# 数据溯源 / 证据存储模式与大规模 SQLite 运维（面向 infohub 的追加写入采集系统）

> 研究日期：2026-10-07。背景：infohub 为单机 Python 3.12 / FastAPI + SQLite（WAL，synchronous=FULL），web 与 worker 两个进程同库，Docker Compose 跑在常开的 Windows 笔记本上；库约 4.9 GB，年增 1–2 GB；原始载荷放在 sha256 内容寻址目录（CAS）；证据表由触发器禁止 UPDATE/DELETE；每次进程启动做完整 `integrity_check` + 自定义账本校验（约 34 s）；备份 = SQLite backup API + 被引用 CAS 文件打包 + SHA-256 清单（约 6 分钟）；SQLite 内置持久任务队列带租约。
> 仓库核对（本次只读检查）：`Dockerfile` 第一行为 `FROM python:3.12-slim`；`app/database.py:199-200` 只设置了 `journal_mode=WAL` 与 `busy_timeout=30000`，未设置 `synchronous`、`wal_autocheckpoint`、`journal_size_limit`、`cache_size`（仅校验时临时放大）、`optimize`。本机（macOS，仓库 .venv 的 Python 3.9.6）`sqlite3.sqlite_version` = 3.51.0。
> 每条结论均附来源；未能证实的内容放在各节的 Gaps 中。

## Q1. 多 GB SQLite（多读一写）运维最佳实践：WAL 调优、synchronous、page/cache/mmap、optimize/ANALYZE、增量/完整校验、VACUUM，以及 Windows 上 Docker 挂载的坑

### Takeaway
最紧迫的一条不是调参，而是 **SQLite 版本**：2026-03 公布的 "WAL-reset bug" 影响 3.7.0–3.51.2，触发条件正是 infohub 的形态（WAL + 两个以上进程连接同时写/checkpoint）；`python:3.12-slim` 若基于 Debian trixie 则自带 libsqlite3 3.46.1 且 Debian 未回移修复，需要在镜像里换上 ≥3.51.3（或 3.50.7 / 3.44.6）。其次：数据库应放在 Docker 命名卷（Linux ext4）而非 Windows 主机目录的 bind mount；设置 `journal_size_limit`、调大 `cache_size`、按官方建议运行 `PRAGMA optimize`；`synchronous=NORMAL` 在 WAL 下不会损坏库，但断电可能回滚最近提交——对向外同步 change_log 的系统，这是需要权衡的真实语义差异。

### Cited Findings

**A. 2026 WAL-reset 损坏 bug（最高优先级）**
- 官方：该 bug 存在于 "all version of SQLite from 3.7.0 (2010-07-21) through 3.51.2 (2026-01-09)"，"fixed in version 3.51.3 (2026-03-13) and later. Backports of the fix are available for some earlier releases: 3.44.6 and 3.50.7"。— [SQLite WAL 文档 §WAL-reset bug](https://sqlite.org/wal.html#walresetbug)
- 触发前提（官方原文）："The bug only affects databases in WAL mode when there are two or more database connections open on the same file, in separate threads or processes, and when those two connections attempt to write or checkpoint at the same instant." 官方称其时序窗口极窄、"unlikely to occur in common use"，文档未给出无法升级时的规避办法。— [SQLite WAL 文档](https://sqlite.org/wal.html#walresetbug)
- 机理：checkpoint 未察觉 WAL 已被另一连接重置，沿用过期的 WAL 头信息，导致后续 checkpoint 跳过部分已提交事务页，造成损坏/静默丢写。— [SQLite WAL 文档](https://sqlite.org/wal.html#walresetbug)；[Tailscale 博客](https://tailscale.com/blog/sqlite-wal-reset-bug)
- Tailscale（2026-08-12 发文）："19 separate instances of database corruption over six months"；他们 "take manual control of the checkpoint process so we can run fast and consistent backups"，且 checkpoint 很激进，因而更容易命中；通过对备份持续运行 `PRAGMA integrity_check` 的监控发现损坏；临时缓解包括检测到损坏即停机、构建事务日志管线以便对已知良好备份重放、自动化恢复（<1 小时）。总结一句："running boring technology in a non-standard way is a risk"。— [Tailscale: How Tailscale helped find the SQLite WAL-Reset bug](https://tailscale.com/blog/sqlite-wal-reset-bug)
- 3.52.0 因 "stale expression index"（表达式索引在计算结果变化后与表不一致，被新校验报告为损坏）而被撤回，改推 3.51.3。— [Tailscale 博客](https://tailscale.com/blog/sqlite-wal-reset-bug)
- Debian 各版本 `libsqlite3-0`：bookworm 3.40.1-2+deb12u2，trixie 3.46.1-7+deb13u2，forky/sid 3.53.4-2。— [packages.debian.org 搜索 libsqlite3-0](https://packages.debian.org/search?keywords=libsqlite3-0&searchon=names&suite=all&section=all)
- 第三方追踪称 trixie 的 3.46.1-7+deb13u2 只带 FTS5 CVE 修复、**没有** WAL-reset 回移（来自搜索摘要，非 Debian 官方声明）。— [Debian 安全追踪 sqlite3 源包](https://security-tracker.debian.org/tracker/source-package/sqlite3)（搜索结果汇总）；有项目报告其 Docker 镜像中 Python 使用的是系统层的 SQLite 3.46.1、需在构建时打补丁或换基础镜像：[NousResearch/hermes-agent#71851](https://github.com/NousResearch/hermes-agent/issues/71851)
- Debian 13 "trixie" 于 2025-08-09 发布；Docker 官方 python 镜像提供 `3.12-slim-trixie` 标签。— [Debian 新闻](https://www.debian.org/News/2025/20250809)；[Docker Hub python](https://hub.docker.com/_/python)
- APSW（Python SQLite 包装器）的 PyPI wheel "includes SQLite statically inside which does not interfere with any system SQLite"，且版本号跟随 SQLite（文档当前为 3.53.4.0）。— [APSW PyPI](https://pypi.org/project/apsw/)；[APSW 文档：sqlite3 module differences](https://rogerbinns.github.io/apsw/pysqlite.html)

**B. synchronous：NORMAL vs FULL（官方语义）**
- "WAL mode is safe from corruption with synchronous=NORMAL … WAL mode is always consistent with synchronous=NORMAL, but WAL mode does lose durability. A transaction committed in WAL mode with synchronous=NORMAL might roll back following a power loss or system crash." — [PRAGMA synchronous](https://sqlite.org/pragma.html#pragma_synchronous)
- 官方推荐："The synchronous=NORMAL setting provides the best balance between performance and safety for most applications running in WAL mode." FULL 在 WAL 下是完整 ACID。— [PRAGMA synchronous](https://sqlite.org/pragma.html#pragma_synchronous)
- 机制：NORMAL 只在 checkpoint 前同步 WAL、checkpoint 完成后同步主库；FULL 在每次提交后额外 fsync 一次 WAL。— [PRAGMA](https://sqlite.org/pragma.html)；[wal.html](https://sqlite.org/wal.html)
- 消费级存储 "lie about syncing"：在数据真正落盘前就报告完成。— [How To Corrupt An SQLite Database File](https://sqlite.org/howtocorrupt.html)

**C. WAL 增长、checkpoint 与 journal_size_limit**
- 自动 checkpoint 默认在提交使 WAL ≥1000 页时触发（4 KB 页约 4 MB）；`wal_autocheckpoint` 默认 1000。— [wal.html](https://sqlite.org/wal.html)；[PRAGMA wal_autocheckpoint](https://sqlite.org/pragma.html)
- checkpoint 饥饿："If another connection has a read transaction open, then the checkpoint cannot reset the WAL file"；读者持续重叠时 "the WAL file will grow without bound"。— [wal.html](https://sqlite.org/wal.html)
- "The checkpoint does not normally truncate the WAL file (unless the journal_size_limit pragma is set)"；`journal_size_limit` 默认 -1（不限制），设置后 WAL 超限会被截断到该值。— [wal.html](https://sqlite.org/wal.html)；[PRAGMA journal_size_limit](https://sqlite.org/pragma.html)
- checkpoint 模式：PASSIVE（默认，不等待）/ FULL（等待写者并让读者处于最新快照）/ RESTART（再等所有读者结束）/ TRUNCATE（RESTART + 截断 WAL 为 0 字节）/ NOOP。— [PRAGMA wal_checkpoint](https://sqlite.org/pragma.html)
- 大事务："WAL works best with smaller transactions"；超过约 100 MB 的事务回滚日志可能更快，超过 1 GB WAL 可能报 I/O 或磁盘满错误（文中同时注明 3.11.0 起大事务效率已与回滚模式相当）。— [wal.html](https://sqlite.org/wal.html)
- WAL 要求所有进程在同一主机、共享内存（wal-index 用 mmap 文件实现）："WAL does not work over a network filesystem"。WAL 模式下不能修改 page_size。— [wal.html](https://sqlite.org/wal.html)

**D. 事务模式与 SQLITE_BUSY**
- "EXCLUSIVE and IMMEDIATE are the same in WAL mode"；读事务升级为写事务时若另一连接持有写锁，"the write statement will fail with SQLITE_BUSY"。— [Transaction 文档](https://sqlite.org/lang_transaction.html)

**E. page_size / cache_size / mmap_size**
- `page_size` 默认自 3.12.0 起为 4096；`cache_size` 默认 -2000（约 2 MB）。— [PRAGMA](https://sqlite.org/pragma.html)
- 大 BLOB：">100KB" 的 BLOB 从独立文件读取更快；8192/16384 页大小对大 BLOB I/O 最佳（2011 年 Ubuntu/ext4 测试，官方提醒需在目标硬件复测）。— [Internal Versus External BLOBs](https://sqlite.org/intern-v-extern-blob.html)
- mmap：默认 0（关闭）；优点是减少内核/用户态拷贝、与 OS 页缓存共享；缺点 "An I/O error on a memory-mapped file cannot be caught … results in a program crash"、性能不一定提升、"Windows is unable to truncate a memory-mapped file"（VACUUM 缩容会静默失效）。— [Memory-Mapped I/O](https://sqlite.org/mmap.html)；另 howtocorrupt 指出 mmap 下野指针会直接写坏库文件。— [howtocorrupt](https://sqlite.org/howtocorrupt.html)

**F. PRAGMA optimize / ANALYZE / 查询计划稳定**
- 官方用法："Applications with short-lived database connections should run 'PRAGMA optimize;' once, just prior to closing each database connection"；"Applications that use long-lived database connections should run 'PRAGMA optimize=0x10002;' when the connection is first opened, and then also run 'PRAGMA optimize;' periodically"；通常 "nearly a no-op … very fast"。`analysis_limit=N` 让 ANALYZE 每个索引约扫 N 行即停。— [PRAGMA optimize / analysis_limit](https://sqlite.org/pragma.html)
- 查询计划问题清单：多数性能问题源于缺索引；"low-quality index"（最左列同值超过 10–20 行）需靠 ANALYZE 写入的 sqlite_stat1 让规划器知情；用 PRAGMA optimize 保持 stat1 更新；likely()/CROSS JOIN/INDEXED BY 等技巧官方 2024 更新后称 "you should never need to use any of the hacks"。— [Query Planner NG 检查清单](https://sqlite.org/queryplanner-ng.html)

**G. 完整 vs 快速/部分校验**
- `quick_check` 不校验 UNIQUE 约束、不校验索引内容与表一致，"runs in O(N) time whereas PRAGMA integrity_check requires O(NlogN) time"。`integrity_check(N)` 最多报告 N 个错误（默认 100）；`integrity_check(TABLENAME)` 仅检查该表及其索引，但无法发现未使用区域或页重复使用等错误。— [PRAGMA integrity_check / quick_check](https://sqlite.org/pragma.html)
- `PRAGMA cell_size_check` 在 B-tree 页首次读入时做额外检查，"corruption is detected earlier"。— [PRAGMA cell_size_check](https://sqlite.org/pragma.html)

**H. VACUUM / auto_vacuum**
- VACUUM 需 "as much as twice the size of the original database file" 的空闲空间；WAL 模式下 VACUUM 只能改 auto_vacuum，不能改 page_size；auto_vacuum "can lead to extra database file fragmentation" 且不压缩半满页；incremental 模式需手动 `incremental_vacuum(N)`。— [VACUUM](https://sqlite.org/lang_vacuum.html)；[PRAGMA auto_vacuum](https://sqlite.org/pragma.html)

**I. 已知损坏路径（与备份/文件系统相关）**
- 网络文件系统锁有 bug（尤其 NFS）；POSIX advisory lock 会被同进程任意 `close()` 取消；事务进行中复制库文件会得到混合新旧内容的坏备份；删除/改名 hot journal 或 WAL 会使恢复失效；硬/软链接让不同进程用不同 journal。— [howtocorrupt](https://sqlite.org/howtocorrupt.html)

**J. Docker Desktop + WSL2 + Windows 挂载**
- Docker 官方："Performance is much higher when files are bind-mounted from the Linux filesystem, rather than accessed from the Windows host filesystem"，并建议避免 `-v /mnt/c/...`；官方页面没有关于命名卷、文件锁或数据库的说明。— [Docker Desktop WSL 2 best practices](https://docs.docker.com/desktop/features/wsl/best-practices/)
- 实测个案（24 MB 库，单一 GitHub issue，非基准测试）：同一查询本机 2.1 ms、复制进容器 4.7 ms、Windows bind mount 196 ms；插入/删除约 25 ms；拟议修复为改用命名卷，代价是 Windows 资源管理器/IDE 无法直接访问库文件。— [mostlypixels/a-vibe-coded-story-organizer#298](https://github.com/mostlypixels/a-vibe-coded-story-organizer/issues/298)
- 长期社区报告：Windows 共享目录上的 SQLite 出现 "database is locked"。— [docker/for-win#11](https://github.com/docker/for-win/issues/11)；[Docker 论坛](https://forums.docker.com/t/sqlite-db-locked-when-on-a-windows-volume/13757)

### Inferences
- **WAL-reset 风险直接适用于 infohub**：web + worker 为不同进程的连接，都会写、都可能触发 autocheckpoint，满足官方前提。`python:3.12-slim` 不带后缀的标签现在大概率解析为 trixie（Debian 13 已为 stable，且官方提供 slim-trixie 变体），则容器内 `sqlite3.sqlite_version` 很可能是 3.46.1 → 受影响。建议：(1) 在容器内实际打印 `sqlite3.sqlite_version` 确认；(2) 启动时断言版本 ≥3.51.3（或恰为 3.50.7/3.44.6 回移版），否则拒绝启动；(3) 修复路径按改动量排序：在 Dockerfile 中编译/安装 3.51.3+（推荐 3.53.x）的 libsqlite3 共享库并让 `_sqlite3` 链接它（需核实动态链接路径）；或基础镜像换到带新 SQLite 的发行版；或改用自带 SQLite 的驱动（APSW，改动大）。本机 3.9 venv 的 3.51.0 同样在受影响区间（开发用途，风险低）。
- 在升级 SQLite **之前**，不要新增"手动/激进 checkpoint"或引入 Litestream 等会在另一进程做 checkpoint 的工具——Tailscale 的经验表明这会放大命中概率。
- **synchronous**：NORMAL 不会损坏库，但断电/系统崩溃可能回滚最后几次提交。对 infohub 而言，CAS 文件先写、DB 后提交，回滚只会留下孤儿 blob（无害）；真正的问题是 change_log：若某个序号在回滚前已被外部同步方读到，回滚后同一序号可能被新内容复用。infohub 已有 epoch 机制，可在"启动时检测到崩溃恢复"后提升 epoch 来化解；否则保留 FULL。笔记本有电池，掉电风险低于台式机，但 Windows 蓝屏/强制更新重启仍属 "system crash"。可选折中：证据写入连接用 FULL、只读/队列心跳连接用 NORMAL（synchronous 是按连接设置的）。
- **WAL 尺寸**：`journal_size_limit` 默认不限，大批量采集或 6 分钟备份/34 秒校验期间的长读事务会让 WAL 膨胀且之后不收缩；建议设如 64–256 MB。是否在低峰期执行 `wal_checkpoint(TRUNCATE)` 应在 SQLite 升级后再做。
- **cache_size**：默认约 2 MB 对 4.9 GB 库过小；可对长生命周期连接设为 -131072～-262144（128–256 MB），笔记本内存允许即可（需实测）。
- **mmap**：容器内为 Linux，Windows 截断问题不适用；但 I/O 错误会变成进程崩溃，对证据系统的收益/风险比一般，可不开或小值开启并实测。
- **page_size**：blob 已外置在 CAS，4096 默认值无需更改（WAL 下更改还需切出 WAL 再 VACUUM）。
- **VACUUM**：追加写入库几乎无空闲页，常规不必 VACUUM；不建议开启 auto_vacuum（碎片化且对追加库收益小）。
- **启动时校验**：完整 `integrity_check` 为 O(NlogN)，随库增长线性以上增长；更合理的是"启动做 `quick_check` 或仅做头部/账本增量校验，完整 `integrity_check` 移到后台定时任务并对备份副本执行"（同 Tailscale 做法）。详见 Q5。
- **Docker 存储选择**：应选 **命名卷**（位于 Docker Desktop WSL2 发行版内的 ext4）或 WSL 发行版内路径的 bind mount，不要 bind mount `C:\` 目录。命名卷的代价是需要通过容器（或 `\\wsl$`）导出备份到 Windows/异地，备份流程需相应设计。

### Gaps
- 未能在官方来源确认 `python:3.12-slim`（无后缀）当前精确指向 trixie 还是 bookworm，也无法在本机运行 Docker 实测其 `sqlite3.sqlite_version`（本机无 docker）。bookworm 的 3.40.1 同样在受影响区间，因此结论不变。
- Debian 是否会在 trixie 安全更新中回移 WAL-reset 修复：只找到第三方汇总，未见 Debian 官方公告。
- 未找到 2023–2026 年 synchronous=NORMAL vs FULL 在类似硬件上的可信吞吐对比数据；也未找到 Docker Desktop 命名卷（VHDX 上的 ext4）在 Windows 主机断电时 fsync 是否真正穿透到物理盘的权威资料——这直接影响 FULL 的实际意义，建议列为需实测项。
- Docker 官方文档对 bind mount 上 SQLite 锁/WAL 共享内存的行为没有正式说明；锁问题证据来自社区 issue。
- APSW 的许可证未核实。

## Q2. 连续备份/复制工具：Litestream（2025 重构）、LiteFS、sqlite3_rsync、VACUUM INTO、backup API —— 哪个适合单台 Windows 主机 + 异地副本

### Takeaway
对 infohub：**sqlite3_rsync（经 SSH，增量页哈希）或 VACUUM INTO 生成快照后再异地同步** 最贴合"单机 + 定期异地副本"；Litestream v0.5.x 可作为 Linux 侧车容器提供秒级 RPO 与时间点恢复，但它通过长读事务接管 checkpoint，且只覆盖数据库不覆盖 CAS 文件，必须在 SQLite 升级（WAL-reset）后再上；LiteFS 支持有限，不适合。

### Cited Findings
- **Litestream v0.5（2025 重构）**：用 LTX 文件取代原始 WAL 段，记录按事务的页范围并可压缩；三级压缩（30 秒 / 5 分钟 / 1 小时），"restore a SQLite database to any point in time, using only a dozen or so files on average"；取消 generations 概念改用单调 TXID；**每个数据库只能有一个副本目标**（破坏性变更）；去掉 CGO，改用 `modernc.org/sqlite`；后端 S3、GCS、Azure Blob，新增 NATS JetStream；v0.3 升级"Just start using the new version"。— [Fly 博客：Litestream v0.5.0 is Here](https://fly.io/blog/litestream-v050-is-here/)；设计说明见 [Litestream: Revamped](https://fly.io/blog/litestream-revamped/)
- Litestream 工作方式："starts a long-running read transaction to prevent any other process from checkpointing and restarting the WAL file"，持续读取新 WAL 页并 "manually calls out to SQLite to perform checkpoints"；L0 为每次同步批次，L1–L3 为压缩层，快照默认每 24 小时，保留期默认 24 小时且始终至少保留一个快照；恢复 = 最近快照 + 按 TXID 应用 LTX。— [Litestream: How it works](https://litestream.io/how-it-works/)
- Litestream 会在源库创建内部表 `_litestream_lock`（写入在回滚的事务中进行），"a database under Litestream should not be expected to remain byte-identical"；许可证 Apache-2.0；仓库目录含 s3、gs、sftp、file、nats、webdav 等副本类型。— [benbjohnson/litestream](https://github.com/benbjohnson/litestream)
- 版本：v0.5.17（2026-08-31）发布说明含 "fix(deps): remove release-blocking SQLite WAL-reset corruption exposure"；v0.5.16 "fix(replica): fsync directory after durable renames"；v0.5.10 "feat(restore): add post-restore integrity validation"；每个版本均声明 "Windows binaries are provided for convenience but Windows is NOT an officially supported platform"。— [Litestream releases](https://github.com/benbjohnson/litestream/releases)（注：一个第三方 PR 称 v0.5.17 发布于 2026-10-04，与 releases 页不一致，以 releases 页为准）
- Litestream 提供 Windows 服务运行指南（v0.5.x 起为 .zip 需手动注册服务）。— [Running as a Windows Service](https://litestream.io/guides/windows/)
- **LiteFS**：Fly 表示未停止但投入有限，"We are not able to provide support or guidance for this product. Use with caution."；LiteFS Cloud 已于 2024-10-15 停止；项目仍为 beta。— [Fly 社区：What is the status of LiteFS?](https://community.fly.io/t/what-is-the-status-of-litefs/23883)；[Sunsetting LiteFS Cloud](https://community.fly.io/t/sunsetting-litefs-cloud/20829)（以上为搜索摘要，社区帖需登录）
- **sqlite3_rsync**：副本向源端发送页（或页组）的密码学哈希，源端只回传差异页；远程需 SSH 且两端 PATH 中都有 `sqlite3_rsync`；"Other programs can write to ORIGIN and can read from REPLICA while this utility runs"，副本最终是源库某一时刻的完全一致快照；运行期间副本只读；3.50.0（2025-05-29）起取消了"必须 WAL、页大小一致"的限制；"a 500MB database will typically synchronize with about 20KB of network traffic"（库内容相近时）。— [sqlite3_rsync](https://sqlite.org/rsync.html)
- **VACUUM INTO**：备份 API 的替代，产出"minimal in size"的一致快照；若源库 synchronous 为 NORMAL/FULL，完成后会 fsync 输出文件；被断电中断则输出可能不完整/损坏。— [VACUUM](https://sqlite.org/lang_vacuum.html)
- **Backup API**：若源库被**另一个连接/进程**修改，增量备份 "usually restarts the backup process when sqlite3_backup_step() is next called"；只有同一连接写入时目标才会被同步更新；一步复制全部页需要在整个过程中持有源库读锁。— [Online Backup API](https://sqlite.org/backup.html)
- 事务中直接复制库文件会得到坏备份。— [howtocorrupt](https://sqlite.org/howtocorrupt.html)
- SQLite 本身为公有领域。— [SQLite Copyright](https://sqlite.org/copyright.html)

### Inferences
- infohub 是 web + worker 两进程：用 Python `Connection.backup()` 分批（pages>0）会因另一进程写入而反复重启；一次性复制（pages=-1/0）在 WAL 下不阻塞写者，但会在 6 分钟内持有读事务 → 期间 WAL 无法重置、会增长（结合 Q1 的 `journal_size_limit`）。VACUUM INTO 同为长读事务，但产物更紧凑且自动 fsync，可作为替代；两者均应在产出后对副本跑 `integrity_check`（可放到后台，不阻塞服务）。
- **推荐组合（单机 + 异地）**：
  1. 本地：定时 `VACUUM INTO`（或现有 backup API）生成快照 + CAS 增量清单（已有）。
  2. 异地：`sqlite3_rsync` 经 SSH（例如经 Tailscale 到另一台机器/NAS）把**活库**同步到远端副本，带宽极低；远端需安装 `sqlite3_rsync`（≥3.50.0）。CAS blob 由于内容寻址、不可变，可用任意"只增不改"的文件同步工具同步（未在本次研究范围内核实具体工具）。
  3. 可选：Litestream 作为 Linux 侧车容器（不是 Windows 原生服务，因而避开"Windows 非官方支持"），以 SFTP/S3 兼容存储为目标，提供分钟级以内 RPO 与 PITR。前提：SQLite 已升级到含 WAL-reset 修复的版本，且使用 v0.5.17+；应用侧不要再自行做 checkpoint（Litestream 接管）。注意每库只能一个目标。
- LiteFS 面向多节点（FUSE + 主从），对单机无收益且支持有限，不建议。
- 若采用 Litestream，它只覆盖 SQLite；"证据完整"的恢复仍需 CAS 文件同步 + 清单校验，恢复演练应同时验证两者（Litestream v0.5.10 起有恢复后完整性校验）。

### Gaps
- 未找到 Litestream 在 4–5 GB、写入频繁库上的公开实测（CPU、带宽、对 checkpoint 的影响）数据。
- 未核实 Litestream v0.5.17 实际捆绑的 SQLite 版本号（发布说明只说移除了 WAL-reset 暴露）。
- 未找到 sqlite3_rsync 在 Windows 原生环境运行的官方说明；在容器/WSL 内运行应无问题，但未验证。
- LiteFS 许可证与最新版本号未从一手来源核实。

## Q3. SQLite 上的任务队列（litequeue、huey、Django-Q、类 SKIP LOCKED 模式）与租约/fencing 实践

### Takeaway
SQLite 单写者模型下不需要也没有 `SKIP LOCKED`：在 `BEGIN IMMEDIATE` 事务内"选一条可领取任务并写入租约"就是原子领取。现成库（huey 的 SqliteHuey、litequeue）未见文档化的可见性超时/租约语义，infohub 自建"租约 + 单调 fencing token（如 attempt/lease_version）+ 完成时校验 token"的队列是合理选择。

### Cited Findings
- huey：MIT 许可，支持 Redis、Postgres、SQLite（`SqliteHuey`）、文件系统、内存存储；有重试、任务锁、限流、调度、过期、周期任务；仓库文档未说明 worker 崩溃时已出队任务是否会因租约/可见性超时而重新投递。— [coleifer/huey](https://github.com/coleifer/huey)
- litequeue 定位为基于 SQLite 的"primitive queue implementation on which to build other tools"。— [Litements: Queue](https://litements.exampl.io/queue/)（搜索摘要）
- 社区小型项目（成熟度低，仅作模式参考）：TaskRelay（SQLite 事务、过期租约、退避重试、死信、fenced acknowledgements）、LeaseQueue（幂等提交、原子领取、过期租约、死信）。— [taskrelay-python](https://github.com/alandoure005/taskrelay-python)；[leasequeue](https://github.com/nawyaunnam/leasequeue)
- Kleppmann：带超时的锁（租约）在 GC 停顿或网络延迟超过租约时不安全，"If the GC pause lasts longer than the lease expiry period … it may go ahead and make some unsafe change"；解决方法是 fencing token——每次获取锁发放单调递增编号，存储端拒绝比已接受编号更小的写入。— [Martin Kleppmann: How to do distributed locking (2016)](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)
- WAL 下 IMMEDIATE 与 EXCLUSIVE 等价；读事务升级为写事务若遇他人持有写锁会直接 SQLITE_BUSY。— [Transaction 文档](https://sqlite.org/lang_transaction.html)

### Inferences
- 领取模式：`BEGIN IMMEDIATE; UPDATE jobs SET lease_owner=?, lease_until=?, lease_token=lease_token+1 WHERE id=(SELECT id FROM jobs WHERE state='ready' AND (lease_until IS NULL OR lease_until<?) ORDER BY priority, id LIMIT 1) RETURNING id, lease_token; COMMIT;` 在单写者下即为原子领取（`RETURNING` 需 SQLite 3.35+，本次未单独核实版本号）。
- fencing：worker 写结果/提交发布时在同一事务中 `WHERE id=? AND lease_token=?` 校验；token 不符则丢弃结果——这与 infohub 的"analysis attempts / results"不可变追加表天然契合（过期 worker 的结果可以记录为一次被拒绝的 attempt，而不是覆盖）。
- 所有写事务使用 `BEGIN IMMEDIATE`，避免 DEFERRED 读→写升级时绕过 busy_timeout 直接 SQLITE_BUSY。
- 队列心跳/续租属于高频小写入，若改 `synchronous=NORMAL` 可放在单独连接上（见 Q1 推断），丢失最近一次续租最多导致任务被重新领取，靠 fencing 保证正确性。
- 不建议为队列引入 huey：其 SQLite 存储与业务库的事务无法原子组合，且租约语义不明确。

### Gaps
- 未取得 Django-Q / Django-Q2 的 ORM（SQLite）broker 租约语义的一手资料。
- 未找到 SQLite 队列在多 GB 业务库内与业务写入争用写锁的公开实测数据。
- litequeue 的许可证与维护状态未核实。

## Q4. 内容寻址证据存储与溯源标准（W3C PROV、WARC/warcio/Browsertrix、Software Heritage、IPFS 式 CAS、新闻档案存储方式）及可验证追加日志（Merkle 透明日志、sigstore/rekor、Trillian/Tessera）——change_log 上的轻量哈希链是否值得

### Takeaway
infohub 的"sha256 CAS + 不可变版本 + 触发器禁改"已与 WARC 摘要字段、Software Heritage 内在标识（intrinsic ID）同构；可借鉴的是 **标准化字段与导出格式**（WARC 记录类型/Payload-Digest/revisit 去重、PROV 的 Entity/Activity/Agent 词汇）。对 change_log，**单机、无外部审计方**时，轻量哈希链 + 定期把链头哈希写到机器外（备份清单、异地副本）性价比最高；完整 Merkle 透明日志（RFC 6962 / tlog-tiles / Tessera）只有在需要向第三方出具 O(log N) 包含/一致性证明时才值得。

### Cited Findings
- **W3C PROV**：2013-04-30 成为 W3C Recommendation 家族；序列化有 PROV-O（OWL2/RDF）、PROV-N（人读记法）、PROV-XML；PROV-LINKS 提供跨 bundle 链接，支持"provenance of provenance"。— [PROV-Overview](https://www.w3.org/TR/prov-overview/)
- **WARC 1.1（ISO 28500:2017）**：记录类型 warcinfo、response、resource、request、metadata、revisit、conversion、continuation；`WARC-Block-Digest` 针对整个块，`WARC-Payload-Digest` 针对有效载荷，格式为 `算法:值`，不强制特定算法；revisit 记录 + `WARC-Refers-To` + "identical-payload-digest" profile 用于去重；建议单文件约 1 GB、gzip 逐记录压缩（.warc.gz）；用 `WARC-Concurrent-To` 关联请求/响应。— [IIPC WARC 1.1 规范](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/)
- **warcio**：Apache-2.0；读 WARC 1.0/1.1 与旧 ARC，写 WARC；写入时自动计算 block/payload 摘要；`warcio check` 校验摘要；`capture_http` 可把 `requests` 流量直接录成 WARC；CDXJ 索引由 cdxj-indexer 提供。— [webrecorder/warcio](https://github.com/webrecorder/warcio)
- **Software Heritage SWHID**：对象类型 cnt/dir/rev/rel/snp；内容 ID 为 git 风格 SHA1（"blob" + 长度 + NUL + 内容）；构成 "a Merkle DAG"；"intrinsic identifiers can be computed from the object itself, without having to rely on any third party"；限定符 origin/visit/anchor/path/lines 提供外部上下文。— [SWHID 规范](https://docs.softwareheritage.org/devel/swh-model/persistent-identifiers.html)
- **tlog-tiles（C2SP）**：签名树头以 checkpoint 形式提供（Ed25519）；Merkle 树以静态 tile 提供，"Full tiles MUST be exactly 256 hashes wide, or 8,192 bytes"；条目以 entry bundle 提供；tile 不可变、可长期缓存；客户端并行取 tile 自行计算包含/一致性证明；规范仍有 TODO，处于演进中。— [C2SP tlog-tiles](https://c2sp.org/tlog-tiles)
- **Trillian Tessera / Rekor v2**：Tessera 是构建 tile 化透明日志的 Go 库，Trillian v1 的继任方案；Rekor v2 于 2025-10-10 GA，后端从 Trillian 换为 Tessera，以降低存储成本、免去 log server/signer 实例、读请求可 CDN 缓存；Tessera 后端包括 GCP、AWS、MySQL 与 "a POSIX filesystem"；Rekor v2 批量集成带来"a few seconds"延迟；见证（witnessing）计划集成。— [Sigstore 博客：Rekor v2 GA](https://blog.sigstore.dev/rekor-v2-ga/)；[transparency-dev/tessera](https://github.com/transparency-dev/tessera)

### Inferences
- **CAS 与 WARC 对齐**：infohub 的 raw record 可补充存储（或可导出）WARC 等价元数据：请求/响应头、抓取时间、`WARC-Payload-Digest`（sha256:…）、`WARC-Block-Digest`。需要互操作/长期保存时，用 warcio 按需把一批 raw records 导出为 .warc.gz（1 GB 一卷），并用 `warcio check` 做第三方可复核的摘要验证。重复抓取同一内容可映射为 revisit 记录（infohub 的 CAS 去重已在存储层实现同样效果）。
- **PROV 映射**（只借词汇，不必上 RDF）：raw_record / document_version / analysis_result = `prov:Entity`；采集与 analysis attempt = `prov:Activity`；爬虫/模型/提示词版本 = `prov:Agent`（SoftwareAgent）+ `prov:Plan`；关系 `used`、`wasGeneratedBy`、`wasDerivedFrom`、`wasAssociatedWith`。在导出/审计接口提供 PROV-JSON 视图即可。
- **哈希链 vs Merkle**：infohub 的威胁模型是"单机上的意外损坏/误操作/回滚"，而非"不受信任的日志运营者"。线性哈希链 `h_n = sha256(h_{n-1} || canonical(row_n))` 存入 change_log（或旁表），每次备份把 `(seq, h_seq)` 写进 SHA-256 清单并同步到异地，即可让任何对历史行的篡改/静默丢失在下次校验时暴露，且支持从上次已验证的检查点开始**增量**校验（见 Q5）。代价：每行一次 sha256，写入路径几乎无感。Merkle（tlog-tiles）的增量价值在于 O(log N) 包含/一致性证明与第三方见证；若未来要对外发布"某条新闻在某时已被采集"的可验证证明，再考虑 Tessera 的 POSIX 后端。
- 注意哈希链与 epoch 的配合：epoch 切换（如崩溃恢复导致序号回退）应写入一条显式的"链重置/分叉"记录，引用旧链头，避免链断裂被误判为篡改。

### Gaps
- 未取得 Internet Archive、GDELT 原始抓取存储方式的一手资料（IA 使用 WARC 为业内常识，但本次未取得可引用的官方页面）。
- Browsertrix（Crawler/Cloud）的许可证与当前版本未核实；Tessera、Rekor 许可证未在已抓取页面中出现（推测 Apache-2.0，未核实）。
- Russ Cox《Transparent Logs for Skeptical Clients》（research.swtch.com/tlog）抓取失败（DNS 错误），因此"哈希链证明规模 O(N) vs Merkle O(log N)"的对比未能引用一手来源。
- SWHID 是否已成为 ISO/IEC 国际标准未在抓取页面中确认。
- IPFS/CID 的多哈希（multihash）设计未纳入本次检索。

## Q5. 用增量/流式完整性校验替代"每次启动全量复核"

### Takeaway
把"启动时全量 `integrity_check` + 全账本复核（34 s，且随库增长）"拆成三层：启动只做 O(1)/O(增量) 检查（版本断言、`quick_check` 可选、从上次检查点开始校验账本哈希链）；后台定期在**备份副本**上跑完整 `integrity_check`（Tailscale 模式）；页级防护可选用官方 checksum VFS（cksumvfs）让位翻转在读取时即报 `SQLITE_IOERR_DATA`。

### Cited Findings
- `quick_check` 为 O(N)，`integrity_check` 为 O(NlogN)；`integrity_check(TABLENAME)` 可只查某表及其索引，但查不出未用区域/页重复使用类错误。— [PRAGMA](https://sqlite.org/pragma.html)
- `cell_size_check` 让 B-tree 页在首次读入时做额外检查，更早发现损坏。— [PRAGMA cell_size_check](https://sqlite.org/pragma.html)
- **cksumvfs**："a VFS shim that adds an 8-byte checksum to the end of every page"，写时添加、读时校验；需 SQLite ≥3.32.0 且 reserve bytes 恰为 8（新库在写数据前用 `SQLITE_FCNTL_RESERVE_BYTES` 设置，旧库需随后 `VACUUM`）；校验失败返回 `SQLITE_IOERR_DATA`；提供 `verify_checksum()` 与 `PRAGMA checksum_verification=OFF`；reserve bytes 只能增不能减；以可加载扩展形式提供。— [Checksum VFS](https://sqlite.org/cksumvfs.html)
- Tailscale 通过对备份 "continuously ran `PRAGMA integrity_check`" 发现损坏。— [Tailscale 博客](https://tailscale.com/blog/sqlite-wal-reset-bug)
- Litestream v0.5.10 增加 "post-restore integrity validation"。— [Litestream releases](https://github.com/benbjohnson/litestream/releases)
- WARC 摘要字段与 `warcio check` 提供对原始载荷的独立校验方式。— [IIPC WARC 1.1](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/)；[warcio](https://github.com/webrecorder/warcio)
- SWHID 内在标识可从对象本身重新计算，无需第三方。— [SWHID](https://docs.softwareheritage.org/devel/swh-model/persistent-identifiers.html)

### Inferences
- **启动路径（目标 <1–2 s）**：(1) 断言 SQLite 版本不在 WAL-reset 受影响区间；(2) 断言关键 PRAGMA（journal_mode、synchronous、触发器存在且定义未变——可对 `sqlite_schema` 中证据表触发器 SQL 取哈希比对）；(3) 账本：从持久化的 `(last_verified_seq, chain_hash)` 检查点起，只校验新增 change_log 行并推进检查点；(4) 可选：`quick_check` 仅在上次非正常退出（WAL 存在且较大/崩溃标记）时执行。
- **后台路径**：每日/每次备份后对**备份副本**跑完整 `integrity_check` + `foreign_key_check` + 全账本重算；与"恢复演练"合并，既校验备份可用又不占用主库读事务（避免 WAL 饥饿）。全库 `integrity_check` 在活库上运行时也会长期持有读事务。
- **CAS 校验**：blob 不可变且以 sha256 命名，可做"滚动抽样复核"（每天随机/按游标复核 1/30 的 blob，一个月覆盖一遍），新写入 blob 在写入时校验；比每次全量重新哈希便宜得多。
- **cksumvfs** 适合发现静默位翻转，但需要：Python `sqlite3` 能加载扩展（官方镜像是否启用 `enable_load_extension` 未核实）、一次性 VACUUM 设置 reserve bytes（4.9 GB 库需约 2 倍空间与停机时间），且只保护主库页面（文档未说明 WAL 帧）。成本/收益需评估，属"可选增强"。
- **按表分片的全量校验**：利用 `integrity_check(TABLENAME)` 轮转校验各大表，可把一次 34 s 拆成多次短检查，但须定期补一次全库检查以覆盖跨表类错误。

### Gaps
- 未找到 `integrity_check` / `quick_check` 在 5–20 GB 库上的公开耗时基准，也没有 cksumvfs 的性能开销数据（官方页面未给数字）。
- 未核实官方 Python Docker 镜像的 `sqlite3` 模块是否允许加载扩展（关系到 cksumvfs 可行性）。
- 未找到针对"SQLite 追加账本 + 哈希链增量校验"的成熟开源实现可直接复用；上面的设计为推断。
