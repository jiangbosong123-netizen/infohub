# 近重复检测与新闻事件/故事聚类：可供 infohub 借鉴的方法、项目与论文

> 研究范围：(a) 短文本近重复检测（MinHash/LSH、SimHash、text-dedup、embedding 去重）；(b) 在线/流式新闻事件（story）聚类（TDT、Miranda 2018、embedding 方法、LLM 辅助）；(c) 中英跨语言同一事件匹配；(d) 生产级开源实现与成本控制；(e) 少标注评估与 story ID 稳定性。
> 检索日期：2026-10-07。所有“事实”均附来源；“Inferences”是对 infohub 的借鉴推断，不是来源事实。论文 PDF 数字均为本地抽取 PDF 文本后核对（Miranda 2018、Saravanakumar 2021、Santos 2022、Škvorc 2025、SemEval-2022 Task 8 综述、NDD-MAC 2025）。仅来自搜索摘要或二手博客的条目已标注“可靠性较低/未核实”。
> infohub 现状（来自任务说明）：约 4,000 条/天（标题+短摘要，常无正文），约 60% 中文、40% 英文；现有精确 URL 去重 + 规则匹配器 titles-v3（标题字符三元组重叠 + 时间窗 + 公司/实体提示，每条最多打分约 120 个候选 story）；热度按不同发布方计。已知问题：大幅改写的报道、中英同一事件不合并；三家中文快讯措辞略不同。

---

## 1. 短文本近重复检测：MinHash/LSH、SimHash、text-dedup、embedding 去重（SemDeDup 等）；中文短标题用字符 n-gram 还是 jieba；常见阈值

### Takeaway
在标准新闻去重基准上，词面哈希（MinHash）依然是很强的基线：现成的多语 embedding 不微调时只和它打平。明显的提升来自“对比学习微调的 bi-encoder + cross-encoder 重排”（NEWS-COPY 上 ARI 0.937，而 MinHash 是 0.74–0.78）。SimHash 加汉明距离 ≤3 的经典配置是为长网页设计的，用在短文本上召回差。对 infohub 来说：每天 4k 条，精确计算 Jaccard 或包含度（containment）的开销可以忽略，不需要靠 LSH 来提速。真正要做的是把阈值放到自己的标注对上校准。

中文要点：短标题去重别用 SimHash(≤3)。用“字符 2–3 元组 + Jaccard/包含度”做同语种快讯去重，再用 embedding 兜住改写稿。阈值必须用 infohub 自己的标注来定。

### Cited Findings
**库/工具（成熟度、许可证）**
- **datasketch**：MIT 许可证。提供 MinHash、Weighted MinHash、HyperLogLog(++)，索引有 MinHash LSH、LSHBloom、LSH Forest、LSH Ensemble（containment 查询）和 HNSW；可选 Redis/Cassandra 存储后端。v2.0.0 默认改用 “affine32” 置换方案，修复了大集合上相似度高估的偏差，sketch 内存减半，update 约快 4 倍；旧版持久化的 sketch 要重建，或者指定 `scheme="legacy"`。 — [datasketch GitHub](https://github.com/ekzhu/datasketch)
- datasketch 约 2,970 stars，最近一次 push 在 2026-08，仍在活跃维护（GitHub API 查询） — [datasketch GitHub](https://github.com/ekzhu/datasketch)
- **text-dedup**（ChenghaoMou）：Apache-2.0 许可证，需要 Python 3.12+。实现了 MinHash+LSH、64/128 位 SimHash、Suffix Array 子串去重和 Bloom Filter 精确去重。默认参数：MinHash 用 ngram=5、num_perm=240、threshold=0.7；SimHash 用 ngram=3、bit_diff=3。约 762 stars，最近 push 在 2026-03。 — [text-dedup GitHub](https://github.com/ChenghaoMou/text-dedup)
- text-dedup 在 CORE 数据集上的基准：MinHash macro-F1 0.9518，耗时 11.09 s；SimHash macro-F1 0.8515，耗时 626 s，重复类召回只有 0.73（MinHash 为 0.94）。同表引用的 LaBSE 为 0.933，Multilingual E5-Base 为 0.924，RETSim Partial-Dup 为 0.945。 — [text-dedup README](https://github.com/ChenghaoMou/text-dedup)
- **NEWS-COPY**（Silcock、Dell 等，ICLR 2023）：27,210 篇文章，含 122,876 个正例重复对，取自 1920–1977 年间 973 家报纸。神经方法明显优于哈希和 n-gram 重叠；bi-encoder 用单张 GPU 几小时可去重 1,000 万篇。 — [arXiv 2210.04261](https://arxiv.org/abs/2210.04261)；[NEWS-COPY GitHub](https://github.com/dell-research-harvard/NEWS-COPY)（仓库未声明 license，GitHub API 显示为 none）
- NEWS-COPY 上的 ARI 汇总：n-gram 0.440；MinHash 0.737（Silcock 原文）/ 0.783（RETSim 论文调参后）；SimHash 0.695；S-BERT 0.700；Multilingual USE 0.730；Multilingual E5-Base 0.742；RETSim Partial-Dup 0.831；Silcock 对比学习 bi-encoder 0.915；bi-encoder + cross-encoder 重排 0.937。 — [text-dedup README 汇总表](https://github.com/ChenghaoMou/text-dedup)；[RETSim arXiv 2311.17264](https://arxiv.org/html/2311.17264)
- **RETSim**（Google，2023）：536k 参数，256 维，字符级（RETVec 编码器），按 512 字符分块，在 mC4 的 41 种语言上训练。吞吐量：AMD 7950 CPU 上 270 条/秒，同一 CPU 上 MinHash 为 12,544 条/秒；RTX 4090 GPU 上 4,062 条/秒。随 google/unisim 以 MIT 许可证开源。 — [RETSim arXiv](https://arxiv.org/html/2311.17264)。注意：unisim 仓库现已 archived（GitHub API） — [google/unisim](https://github.com/google/unisim)
- **SemDeDup**（Abbas 等，ICML 2023）：用预训练模型的 embedding 做 k-means，在每个簇内删除相似度超过阈值的样本。在 LAION 子集上删掉 50% 数据，性能损失很小；阈值按目标数据量人工选定，保留 60%–80% 效果最好。 — [ICML 2023](https://icml.cc/virtual/2023/26932)；[后续工作 arXiv 2401.04578](https://arxiv.org/html/2401.04578v2)。代码为 CC BY-NC 4.0（非商用），仓库已 archived — [facebookresearch/SemDeDup](https://github.com/facebookresearch/SemDeDup)
- **NDD-MAC**（TCS Research，NAACL 2025 Industry Track）：专门处理付费墙下“只有标题加短摘要”的新闻近重复。流程是：all-mpnet-base-v2 embedding 余弦相似度建图；用抽取出的元数据（事件类型、参与实体、realis 语气、领域、数量等）的 Jaccard 重新加权边；再用 Louvain/Leiden 社区发现切分。数据集：厂商数据 34,801 篇，来自 1,254 个源，平均 83.8 词；NDD-NS 1,205 篇 AGNews 摘要，平均 42.9 词。基线是 MinHash（k=1600 shingles、p=20 个置换，取自 Rodier & Carter 2020）和 SimHash（f=64、k=3，取自 Manku 2007）。阈值 >0.8 时，NDD-MAC 在两个数据集上都优于基线。在改写较多的 NDD-NS 上，最佳设置是阈值 0.6、窗口 4 天、滑动步长 2；在真实厂商新闻流上，作者建议阈值取 0.85–0.9。SimHash 比 MinHash-LSH 更快、更省内存。按滑动窗口处理比整库处理效果更好，计算也省得多。 — [NAACL 2025 Industry PDF](https://aclanthology.org/2025.naacl-industry.73.pdf)
- NDD-NS 基准论文（Kumar 等，WWW'25 Companion）在商业新闻摘要上对比了 MinHash-LSH、SimHash 和 sentence-transformer 方法。全文无法访问（HTTP 403），没有拿到具体数字。 — [ACM DL](https://dl.acm.org/doi/10.1145/3701716.3715303)

**SimHash 阈值与中文短文本**
- 经典 SimHash 配置是 64 位指纹、汉明距离 ≤3 判为近重复（Google / Manku 2007），分词后可用 TF-IDF 加权。 — [行者AI @ 博客园](https://www.cnblogs.com/xingzheai/p/14139921.html)；NAACL 2025 论文同样采用 f=64、k=3 作为 SimHash 基线 — [NAACL 2025 Industry PDF](https://aclanthology.org/2025.naacl-industry.73.pdf)
- 中文从业者文章：SimHash 在长文本上效果好，在短文本上准确率低；相似短文本之间的汉明距离通常大于 3。特征不加权时 MinHash 更好，特征加权时 SimHash 更合适。 — [知乎《海量短文本场景下去重》](https://zhuanlan.zhihu.com/p/51464268)（仅为搜索摘要级信息，属从业者博客，可靠性较低）
- 一项基于字符 n-gram 的中文抄袭检测研究，对象是“不同作者写同一新闻事件”，比较了 2≤n≤10。结论是 n=4、5 时精确率显著提高，召回不下降；这一点与英文不同。 — [ResearchGate: A copy detection method for Chinese text by character based N-gram](https://www.researchgate.net/publication/290968065_A_copy_detection_method_for_chinese_text_by_character_based_N-gram)（只看到摘要片段，未读全文）

### Inferences
- **规模判断**：72 小时窗口内大约 12k 条。对字符 shingle 建倒排索引后精确计算 Jaccard/containment 就够快，MinHash LSH 最多算一个方便的候选召回结构，不是性能必需品。SimHash(≤3) 不适合 15–40 字的中文快讯标题（与上面的中文从业者结论一致）。
- **快讯“三家同发”去重（建议在 titles-v3 前加一层）**：(1) 规范化后做精确哈希：去掉“财联社X月X日电”“【】”、来源前缀和空白，统一全/半角和数字格式；(2) 同语种、24–72h 窗口内，算字符 2–3 元组的 Jaccard，并同时算包含度 |A∩B|/min(|A|,|B|)，用来处理“短快讯 vs 稍长改写”的情况；(3) 未命中的再交给 embedding。text-dedup 默认的 5-gram / Jaccard 0.7 是为长文档设定的。中文快讯标题很短，5-gram 太稀疏，应从 2–3-gram 起步，再在标注对上扫描阈值。
- **字符 n-gram vs jieba**：字符 n-gram 不受 jieba 新词（新产品名、人名）切分不一致的影响，和 infohub 现有的 trigram 做法一致。jieba 词元更适合用来做 TF-IDF 加权或实体提示。目前没有找到针对“中文财经快讯标题”的直接对比数据，建议在 infohub 标注对上做 A/B。
- **embedding 不能单独解决近重复**：NEWS-COPY 上不微调的 mE5-base（0.742）和 MinHash 大致持平，真正拉开差距的是对比微调和 cross-encoder 重排。infohub 不训练模型的话，合理的方案是把词面特征和 embedding 余弦组合成一个特征打分器（见第 2 节）。
- **RETSim** 体积极小、字符级、多语、能在 CPU 上跑（按 270 条/秒估算，4k 条约 15 秒），但仓库已 archived、依赖 TensorFlow 系，长期维护有风险，只适合作为候选。
- **SemDeDup** 是为训练数据剪枝设计的，用途不同，许可证又是非商用，只借鉴它“先粗聚类，再在簇内两两比较”的思路即可。

### Gaps
- 没有找到公开的“中文财经快讯/短标题”近重复基准，也没有对应的阈值数据。上面各类阈值都来自长文档或英文新闻摘要场景，不能直接搬用。
- NDD-NS / NDD-MAC 的具体 F1 只在图里，正文没有给出数值。
- 没有找到 jieba 分词与字符 n-gram 在中文短新闻去重上的定量对比（只有上面那条摘要级证据）。

---

## 2. 在线/流式新闻事件聚类与故事检测：TDT、Miranda 2018、Event Registry、embedding 方法、LLM 辅助；基准与数字

### Takeaway
在“具体事件”这一粒度上，有标注的流式基准一再显示：TF-IDF（词、词元、实体）加时间特征，再配一个学习出来的打分器，比单独用稠密 embedding 更强（英文 B-cubed F1 约 92–95，而单独的 BERT 类 embedding 只有约 69–83）。稠密 embedding 的价值在于跨语言和改写稿。在不少研究里，最可靠的增益来自三件事：学习“是否新建 story”的决策（而不是手调阈值）、时间衰减特征、事后的簇合并。LLM 目前主要用来提供成对约束、做标注或复核，没有看到纯 LLM 的流式新闻聚类在标准基准上胜出的证据。

中文要点：保留 titles-v3 的词面+实体主干，加三样东西：时间衰减特征、embedding 余弦特征、一个学出来的“并入还是新建”分类器；再加一个定时的 story 合并步骤。

### Cited Findings
**Miranda et al. 2018（EMNLP，Priberam；多语流式聚类的事实标准基线）**
- 方法：完全在线。每个语言内部先形成单语簇，文档表示由多组 TF-IDF 子向量组成（词、词元、命名实体，分别对标题、正文、两者合并计算）。另加 3 个高斯时间特征：文档时间与簇内最新、平均、最旧文档时间之差，μ=0，σ 在开发集上调得 72 小时。各特征权重用 SVM-rank 学习。“是否新建簇”由一个单独的二分类 SVM（SVM-merge）决定。跨语言簇是若干单语簇的组合，用 TF-IDF 加权的跨语言词向量计算相似度，并以英文作为枢轴语言。 — [Miranda et al. 2018 PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)；[ACL Anthology D18-1483](https://aclanthology.org/D18-1483/)
- 数据集：由 Rupnik et al. (2016) 的数据构建。测试集英文 8,726 篇 / 222 簇，德文 2,101 / 118，西文 2,177 / 149；训练集英文 12,233 / 593。IDF 用 Deutsche Welle 每种语言 7.7 万–13.4 万篇文章计算。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)
- 结果（成对 F1）：英文 94.1（P 98.2 / R 90.3），对比 CluStream 79.0；德文 97.1；西文 94.2。只用时间特征时 F1 61.1，P 44.5，R 97.6。用阈值 τ 网格搜索决定是否新建簇，F1 为 82.8；改用 SVM-merge 后为 94.1。把跨语言 embedding 直接用于单语聚类，英文只有 74.8，低于词袋基线。跨语言 F1 为 84.0（英文枢轴），用全局 τ 时只有 72.7。在线 LSA 效果明显更差，速度也更慢。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)
- 代码：Priberam/news-clustering，BSD-3-Clause，40 stars，最近 push 在 2022 年，基本不再维护。 — [GitHub](https://github.com/Priberam/news-clustering)
- Miranda 一文把 TDT 计划（Allan et al. 1998）列为新闻聚类研究的起点。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)

**同一英文测试集（222 个真实簇）上的后续 B-cubed 排行**
- newsLens（Laban & Hearst 2017）89.76；Miranda 2018 92.36（产生 326 簇）；Linger & Hajaiej 2020（批处理）93.86；Staykovski et al. 2019 94.41（产生 484 簇）；Saravanakumar et al. 2021 94.76（产生 276 簇）。 — [Saravanakumar et al., arXiv 2101.11059](https://arxiv.org/abs/2101.11059)
- Saravanakumar 2021 的方法：非参数流式 K-means 变体；文档用稀疏 TF-IDF 加实体感知 BERT（在事件相似度上用 triplet loss 微调）表示；簇创建用学习出的分类器，不用固定阈值。消融结果：只用 TF-IDF 为 86.36，加时间后 91.72；只用 E-S-BERT 稠密向量为 68.98，加时间后 82.73。作者的解释是稠密表示会把“主题相关但不是同一事件”的文章并到一起，例如两场不同的足球赛。 — [arXiv 2101.11059](https://arxiv.org/abs/2101.11059)

**Santos, Mendes, Miranda 2022（Priberam，语言无关的简化版）**
- 不再先建单语簇。文档只用 distiluse-base-multilingual-cased-v2 的稠密向量（正文+标题、首段、首段+标题三种）加按天的时间戳，配线性 SVM 的排序、接受和簇合并三个模型。跨语言结果：B-cubed F1 90.10，Linger 为 82.06；标准 F1 97.21，Miranda 为 84.0。单语英文 B-cubed 92.43，与 Miranda 的 92.36 持平。其他语言属零样本（训练数据里只有中文是例外）：中文 B-cubed F1 96.18（P 100 / R 92.65），但只有 28 个簇，样本很小。 — [arXiv 2204.13418](https://arxiv.org/abs/2204.13418)
- 代码 Priberam/projected-news-clustering：仅限研究用途（BSD-3），商用需要另行向 Priberam 取得许可。 — [GitHub](https://github.com/Priberam/projected-news-clustering)

**Škvorc et al. 2025（卢布尔雅那大学 + 媒体监测公司 Kliping，实时故事识别）**
- 背景：系统存档超过 8,500 万篇文章。原系统对每次查询的结果集临时做聚类，结果在时间上不稳定、受结果集大小限制，因此作者改做全库增量的 story 识别。 — [arXiv 2508.08272](https://arxiv.org/abs/2508.08272)
- 用 k-means 对照人工 story 标签比较 embedding，AMI 分别为：TF-IDF 0.603、distiluse 0.777、MiniLM-L6 0.626、DistilBERT 0.724、BGE-M3 0.838（最好）。只用命名实体时，离群（单篇 story）检测准确率更高（0.762，纯文本为 0.720），但聚类 AMI 下降。作者因此建议先用实体做离群检测，再用全文聚类。先用 LLM 生成摘要再做 embedding，效果反而下降（BGE-M3 为 0.780），而且更慢。 — [arXiv 2508.08272](https://arxiv.org/abs/2508.08272)
- 在线算法明显不如离线：AMI 分别为 DBSTREAM 0.162、DBSTREAM+BERTopic 0.398、TextClust 0.172、TextClust+BERTopic 0.355。作者的补救是“聚类后优化”：反复把簇中心或主题词 embedding 距离小于阈值 d 的簇合并。 — [arXiv 2508.08272](https://arxiv.org/abs/2508.08272)
- 与前面结论的冲突：Škvorc 的结果是 BGE-M3 远胜 TF-IDF（离线 k-means、斯洛文尼亚语），而 Saravanakumar 和 Miranda 的结果是 TF-IDF 加时间胜过 BERT（在线、英文、2019–2021 年的 BERT）。两者在模型代际、评价指标（AMI vs B-cubed）和设定（离线 vs 在线）上都不同，不能直接比较。

**层次与多粒度**
- Matryoshka 多语 embedding（2025）：微调的 mE5-base，在 SemEval-2022 Task 8 上 Pearson 0.817，此前最好为 0.801（GateNLP-UShef）。用基于 Reciprocal Nearest Neighbor 的层次凝聚聚类：前 d/4 维对应主题（theme），d/2 维对应话题（topic），全部维度对应具体 story。在 A6000 GPU 上训练，公开了权重。 — [arXiv 2506.00277](https://arxiv.org/html/2506.00277)

**Telegram Data Clustering 竞赛（CPU 约束下的新闻线程聚类实战）**
- 赛题限制：只支持英文和俄文；8 核 CPU、16 GB 内存，不提供 GPU；每 1,000 篇必须在 60 秒内处理完；服务模式要支持带 TTL 的 PUT 增量索引和 DELETE；任务包括语言识别、新闻检测、分类和线程（同事件）分组排序。 — [contest.com 文档](https://contest.com/docs/data_clustering2)
- 第 2 轮冠军 “Mindful Squirrel”（IlyaGusev/tgcontest，C++，Apache-2.0）：fastText 词向量取 avg/max/min 拼接，再乘一个用 triplet loss 训练的矩阵作为文档向量；聚类用 SLINK 单链接，数据按时间分批、批与批有重叠，聚完后再合并；线程排序用媒体 PageRank 加时间因子。 — [IlyaGusev/tgcontest](https://github.com/IlyaGusev/tgcontest)

**在线主题建模（粒度比 story 粗）**
- BERTopic 在线模式：`partial_fit` 要求聚类器本身支持 partial_fit，默认的 HDBSCAN 不支持；可以换成 River 的 DBSTREAM，但训练会明显变慢；另一条路是 `merge_models`，即分批训练再合并，可以保留 UMAP+HDBSCAN。 — [BERTopic 在线建模文档](https://maartengr.github.io/BERTopic/getting_started/online/online.html)。BERTopic 为 MIT 许可证，约 7.9k stars，2026-10 仍有更新（GitHub API） — [MaartenGr/BERTopic](https://github.com/MaartenGr/BERTopic)

**LLM 辅助聚类（2023–2026）**
- Viswanathan et al.（TACL 2024）：把 LLM 当作“有噪声的人类标注者”，用三种方式介入聚类：扩展关键短语、生成成对约束（must-link / cannot-link）、修正聚类结果。 — [ACL Anthology 2024.tacl-1.18](https://aclanthology.org/2024.tacl-1.18/)
- Cequel（2025）：在固定查询预算下，用 EdgeLLM（成对）和 TriangleLLM（三元组）贪心挑选信息量最大的文本对去问 LLM，再做加权约束聚类；同等预算下优于已有方法。 — [arXiv 2504.15640](https://arxiv.org/abs/2504.15640)。搜索摘要称它用 GPT-4o-mini 在 Tweet 数据集上总花费 0.0185 美元，准确率 73.10%（未在原文核实）。
- Tarekegn 2024：在 GDELT 上用 LLM embedding、关键词抽取和事后摘要/打标来增强事件检测，并提出 CSAI 簇稳定性指标；摘要里没有可与上面基准对比的数字。 — [arXiv 2406.10552](https://arxiv.org/abs/2406.10552)

### Inferences
- **对 titles-v3 的直接改造建议（按证据强度排序）**：
  1. 加时间衰减特征：仿照 Miranda，对 story 的最新、平均、最早时间各算一个高斯核，σ 从 72h 起调（快讯类可以更短）。在 Miranda 的实验里，时间特征是让召回大幅上升的关键。
  2. 用学出来的打分器代替手调权重：特征包括字符 trigram 重叠、实体重合、embedding 余弦和时间核，模型用逻辑回归或小型 GBM，训练数据是 infohub 的标注对。
  3. 单独训练“并入还是新建”的分类器，输入是各特征对候选集的最大值。Miranda 的 SVM-merge 比阈值网格搜索高 11.3 个 F1 点。
  4. 加一个定时的 story 合并步骤，例如每小时对近 48–72h 的 story 按中心向量和共享实体做合并，正面解决“大幅改写的稿件没有被合并”的问题（参见 Škvorc 的聚类后优化和 Santos 的簇合并模型）。
- **两层结构**：先在中文、英文内部各自形成 story，再在 story 层面做中英链接。story 数比条目少一个数量级，跨语言比较的次数和 LLM 复核的成本都会随之下降。这正是 Miranda 的设计。
- **Embedding 的定位**：做附加特征和跨语言桥梁，不要替换掉词面和实体主干。原因是在同语种、事件粒度上，词面加实体加时间的证据更强；Škvorc 的反例是离线 k-means 设定，不能直接套用到在线场景。
- 实体用于离群检测（Škvorc）对 infohub 也有意义：有独特实体的条目更可能是单条新闻，可以少拿它去和大簇比较。
- LLM（GLM）只放在打分的灰区做 yes/no 复核，或者用来离线生成训练用的成对标签（Viswanathan / Cequel 的思路），不要让 LLM 直接对每一条做聚类。

### Gaps
- Event Registry（JSI，Rupnik et al. 2016 的跨语言事件追踪）目前的生产做法没有深入调研；只确认 Miranda 的数据集由 Rupnik 2016 的数据构建（[Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)）。
- 没有找到在 Miranda 数据集或类似基准上评测的“纯 LLM 流式聚类”结果（2024–2026）。
- Miranda 数据集本身的许可证没有确认（代码是 BSD-3，数据来自 Rupnik 2016）。

---

## 3. 中英跨语言同一事件匹配：CPU 上能跑的开源 embedding 模型及其在 SemEval-2022 Task 8 等任务上的表现

### Takeaway
SemEval-2022 Task 8 的评测集包含中文（769 对）和中英（213 对），但它们是只在评测阶段出现的“惊喜语言对”，训练集里没有。最好成绩 Pearson 0.818，靠的是微调 XLM-R 并结合机器翻译和集成；不微调、直接用预训练 embedding 也能到 0.759；只用实体重合加 embedding 余弦的简单回归是 0.677。在 CPU 上，multilingual-e5-small（118M，MIT）作为默认选项最划算；想要更好的质量可以用 bge-m3（MIT）或 Qwen3-Embedding-0.6B（Apache-2.0）。规避 jina-embeddings-v3（CC BY-NC）。以 infohub 的量（4k 条短标题/天），几个模型的 CPU 推理时间都可以忽略。

中文要点：跨语言合并的推荐做法是“共享的规范化实体（公司 ID）+ 多语 embedding 余弦 + 时间窗”，在 story 层面完成。公开基准里没有“中文快讯标题 ↔ 英文标题”的现成数字，需要自己标约 200 对来定阈值。

### Cited Findings
**SemEval-2022 Task 8（多语新闻相似度）**
- 约 1 万对新闻，覆盖 10 种语言、18 种语言组合，每对在 7 个维度上标注（GEO、ENT、TIME、NAR、OVERALL、STYLE、TONE），采用 4 级量表。评测集中有 zh 769 对、zh–en 213 对，两者的 OVERALL 均值分别为 2.22 和 3.07（数值越高越不相似），都没有训练数据。各维度中，NAR 和 ENT 与 OVERALL 的相关最高，分别为 0.88 和 0.79。 — [SemEval-2022 Task 8 综述](https://aclanthology.org/2022.semeval-1.155.pdf)
- 多语排行：HFL 0.818（XLM-RoBERTa，数据增强、多标签损失、R-Drop、正文首尾拼接，同时学习 7 个维度）；GateNLP-UShef 0.801（LaBSE bi-encoder 加实体特征）；cyk1337 0.792；ITNLP2022 0.784；EMBEDDIA 0.784。所有队伍的分数在 0.35–0.82 之间，中位数 0.72。最好的人类标注者为 0.88。 — [SemEval 综述](https://aclanthology.org/2022.semeval-1.155.pdf)；[HFL 论文](https://arxiv.org/pdf/2204.04844)；[GateNLP-UShef 论文](https://arxiv.org/pdf/2205.15812)
- 简单方法也很有竞争力：不微调、直接用预训练 embedding 为 0.759（Wangsadirdja et al. 2022）；只用“共享命名实体 + 文章 embedding 余弦”两个特征的回归为 0.677（Sandeep et al. 2022）。表现最好的系统都把多语 embedding 和机器翻译到英文结合起来；bi-encoder 和 cross-encoder 谁更好，各队的证据互相矛盾。 — [SemEval 综述](https://aclanthology.org/2022.semeval-1.155.pdf)
- 后续工作：微调的 Matryoshka mE5-base 达到 0.817（第 2 节已述）。 — [arXiv 2506.00277](https://arxiv.org/html/2506.00277)

**多语 embedding 模型（许可证和参数量来自 Hugging Face API 2026-10-07 查询）**

| 模型 | 许可证 | 参数量 | 备注 | 来源 |
|---|---|---|---|---|
| intfloat/multilingual-e5-small | MIT | 117.7M | 由多语 MiniLM 初始化；输入需要加 "query: "/"passage: " 前缀 | [HF](https://huggingface.co/intfloat/multilingual-e5-small)；[mE5 技术报告](https://arxiv.org/html/2402.05672v1) |
| intfloat/multilingual-e5-base | MIT | 278M | XLM-R base；NEWS-COPY 上 ARI 0.742 | [HF](https://huggingface.co/intfloat/multilingual-e5-base)；[RETSim](https://arxiv.org/html/2311.17264) |
| BAAI/bge-m3 | MIT | 基于 XLM-RoBERTa（Large 级别） | 1024 维、8192 token；同时支持稠密、稀疏和多向量检索；100+ 语言；有 ONNX 版本；在 Škvorc 的 story 实验中表现最好 | [HF](https://huggingface.co/BAAI/bge-m3)；[arXiv 2508.08272](https://arxiv.org/abs/2508.08272) |
| sentence-transformers/LaBSE | Apache-2.0 | 471M | 专为双语句对挖掘设计；GateNLP 用它在 SemEval 拿到 0.801 | [HF](https://huggingface.co/sentence-transformers/LaBSE) |
| paraphrase-multilingual-MiniLM-L12-v2 | Apache-2.0 | 117.7M | 老一代但很轻；每月下载量很高 | [HF](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) |
| Alibaba-NLP/gte-multilingual-base | Apache-2.0 | 305M | — | [HF](https://huggingface.co/Alibaba-NLP/gte-multilingual-base) |
| Qwen/Qwen3-Embedding-0.6B | Apache-2.0 | 596M | 1024 维、32K 上下文、100+ 语言；搜索摘要称 MMTEB 均分 64.33、聚类 52.33、STS 76.17（未在模型卡原文核实） | [HF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)；[Qwen3 Embedding 论文](https://arxiv.org/pdf/2506.05176) |
| google/embeddinggemma-300m | Gemma 许可（非 OSI） | 303M | — | [HF](https://huggingface.co/google/embeddinggemma-300m) |
| jinaai/jina-embeddings-v3 | CC BY-NC 4.0（非商用） | 572M | 许可证不适合商用 | [HF](https://huggingface.co/jinaai/jina-embeddings-v3) |
| BAAI/bge-small-zh-v1.5 | MIT | 24M | 只支持中文，可用于中文内部的改写检测 | [HF](https://huggingface.co/BAAI/bge-small-zh-v1.5) |

- mE5 技术报告：small/base/large 分别由多语 MiniLM、XLM-R base、XLM-R large 初始化。双语句对挖掘上，mE5-large-instruct 在 BUCC 2018（4 个语言对）得 99.0，LaBSE 为 98.8；Tatoeba（112 种语言）上为 83.8 对 81.1。小模型推理更快、存储更省，但效果有折损。 — [arXiv 2402.05672](https://arxiv.org/html/2402.05672v1)
- CPU 延迟只找到二手博客，硬件不明，而且数字互相冲突：一组是 e5-small 约 16 ms、BGE-M3 约 45 ms、LaBSE 约 50 ms；另一组是 Apple M 系列 CPU 上 E5-large 和 BGE-M3 都约 30.9 ms、LaBSE 16.6 ms；还有 mE5-small 在 CPU 上 226 docs/s 的说法。 — [AIMultiple 博客](https://aimultiple.com/multilingual-embedding-models)（可靠性较低）
- GDELT Global Similarity Graph：用 Universal Sentence Encoder v4（DAN 架构）为 65 种语言的全球在线新闻生成 512 维文档向量，每篇文档的向量固定不变。GDELT 的实验认为，在新闻相似度上 DAN 版 USE 与 Transformer 版不相上下，甚至更好。2020 年以来已积累约 2.5 亿个文档向量（连同电视新闻句子向量共约 4.4 亿）。 — [GDELT 博客：GSG 文档嵌入](https://blog.gdeltproject.org/announcing-the-global-similarity-graph-document-embeddings-using-the-universal-sentence-encoder/)；[GDELT 博客：4.4 亿向量](https://blog.gdeltproject.org/global-similarity-graph-440-million-512-dimension-embeddings-now-available-covering-online-television-news/)
- Miranda 2018 的经验：跨语言相似度在“每种语言 ↔ 英文”之间最可靠，以英文为枢轴时跨语言 F1 为 84.0，用全局阈值只有 72.7；不同语言对适合的阈值并不相同。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)

### Inferences
- **推荐的中英合并判据（story 层面）**：两个 story 至少共享一个规范化实体（infohub 已有公司身份表或 ticker），代表标题的 embedding 余弦 ≥ t_xl（t_xl 单独设定，不要沿用单语阈值，参考 Miranda 关于全局阈值的发现），且时间窗重叠，例如最早时间相差在 24–48h 内。中间灰区交给 GLM 做 yes/no 复核。
- **算力估算**：每天 4k 条，每条约 30–80 token。即使 CPU 上只有 20 条/秒，每天也只需约 200 秒；bge-m3 慢 3–5 倍也只要十几分钟。72h 窗口约 1.2 万个向量，384 或 1024 维，用 numpy 暴力算余弦就行，不需要 ANN 索引。以上是推算，建议在那台常开的笔记本上实测。
- **模型选择**：默认用 multilingual-e5-small（MIT、轻量、BUCC/Tatoeba 跨语言对齐好）；如果标注集上召回不够，再试 bge-m3。bge-m3 的稀疏输出还可以顺带当词面特征。需要注意，单独的 embedding 在“同主题不同事件”上容易误合并（见第 2 节），所以实体约束是必须的。
- **翻译路线**：SemEval 最好的系统都结合了机器翻译。对 infohub 而言，逐条翻译太贵；可以只让 LLM 翻译或改写每个 story 的代表标题，翻译量就降到每天几百次。

### Gaps
- 没有找到这些模型在 SemEval-2022 Task 8 zh–en 子集上的单独成绩（综述只给了总体和部分语言的中位数）。
- 没有找到“中文财经快讯标题 ↔ 英文新闻标题”的跨语言事件匹配基准。
- 没有可靠的 CPU 吞吐一手数据（Apple Silicon / ONNX int8），需要本地测量。
- Qwen3-Embedding-0.6B 的 C-MTEB 细项数字只看到搜索摘要，没有核实。

---

## 4. 生产级开源实现与成本控制（候选生成/blocking + 重排）

### Takeaway
能在生产里跑的系统，控制成本的方式都差不多：用时间窗、语言和词面或实体倒排做 blocking 来限制候选；新文档只和簇的代表（中心向量）比，不和簇里每一篇比；贵的模型（cross-encoder 或 LLM）只用在少量灰区样本上；再定期合并碎片簇。小规模开源项目（个人或爱好者级）大多是“SimHash 或哈希去重 + MiniLM embedding + 余弦阈值 + 时间窗”，成熟度有限。

中文要点：infohub 可以照着这条流水线做：规范化哈希 → 字符 shingle 倒排召回候选 → 和 story 中心向量算多特征打分 → 高分直接合并、低分新建、中间分交给 GLM 判 → 每小时跑一次 story 合并。

### Cited Findings
- **Priberam（媒体监测公司）的做法**：新文档只和簇的中心向量比（φ 是文档与簇中心的余弦）。跨语言比较放在簇层面进行，簇的数量远小于文档数，所以比较量小得多。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)
- **Telegram 竞赛的做法**：CPU 限时（每 1,000 篇 ≤60 秒，8 核/16 GB）。冠军方案用轻量的 fastText 文档向量，按时间分批、批间重叠，用 SLINK 聚类后再合并。 — [contest.com 文档](https://contest.com/docs/data_clustering2)；[IlyaGusev/tgcontest](https://github.com/IlyaGusev/tgcontest)
- **NDD-MAC**：先按 embedding 余弦阈值形成初始簇，再在簇内做元数据加权的社区发现；数据按 3–4 天的滑动窗口处理，比整库处理效果更好、成本更低。 — [NAACL 2025 Industry PDF](https://aclanthology.org/2025.naacl-industry.73.pdf)
- **Kliping 原系统的教训**：在每个查询的结果集上临时聚类，结果在时间上不稳定；于是改为全库增量聚类，再定期做聚类后合并。 — [arXiv 2508.08272](https://arxiv.org/abs/2508.08272)
- **GDELT GSG**：每篇文档算一次、固定不变的 512 维向量，在 BigQuery 里用余弦做 “more like this” 检索。 — [GDELT 博客](https://blog.gdeltproject.org/announcing-the-global-similarity-graph-document-embeddings-using-the-universal-sentence-encoder/)
- **小型开源项目（成熟度低，信息来自 GitHub 搜索摘要，未逐一审读代码）**：PJC2010/ai-news-aggregator 用 SHA-256 + SimHash 去重，CPU 上跑 MiniLM（384 维）embedding，在 72 小时窗口内按余弦 0.85 做事件聚类，存储用 PostgreSQL/pgvector — [GitHub](https://github.com/PJC2010/ai-news-aggregator)；OnixFireOne/news-radar 先用 LLM 流水线做语义去重，再用 HDBSCAN 检测趋势，面向 57+ 个 Telegram 加密货币频道 — [GitHub](https://github.com/OnixFireOne/news-radar)
- **控制 LLM 成本的研究**：Cequel 在固定预算下只查询信息量最大的成对或三元组约束 — [arXiv 2504.15640](https://arxiv.org/abs/2504.15640)；Viswanathan 等把 LLM 生成的成对约束用作半监督聚类的输入 — [TACL 2024](https://aclanthology.org/2024.tacl-1.18/)
- **重排的价值**：NEWS-COPY 上，bi-encoder 召回加 cross-encoder 重排（0.937）高于单独的 bi-encoder（0.915）。 — [text-dedup README 汇总](https://github.com/ChenghaoMou/text-dedup)；[arXiv 2210.04261](https://arxiv.org/abs/2210.04261)

### Inferences
- **infohub 流水线草案**（在现有 titles-v3 和约 120 个候选的结构上增量改造）：
  1. 规范化后做精确哈希（处理快讯来源前缀等）→ 直接判为重复。
  2. Blocking：按语言，再按时间窗（快讯 24h、一般新闻 72h）圈定范围，用字符 shingle 倒排和实体 ID 倒排取出候选 story，再加上 embedding 余弦 top-k 补召回改写稿，合并后约 120 个候选。
  3. 打分：对每个“条目 → story”对，用逻辑回归或小 GBM 综合以下特征：trigram Jaccard/包含度、实体重合、embedding 余弦（对 story 中心）、高斯时间核、是否同一发布方。
  4. 决策分三带：高分直接并入，低分新建 story，中间灰区调 GLM 做 yes/no。按条目对的哈希缓存结果，并设每日调用上限。
  5. 跨语言：在 story 层面做中英链接（见第 3 节）。
  6. 每小时对最近 72h 的 story 跑一次合并。
- **LLM 成本粗估（推算）**：如果灰区占 5%，每天约 200 次短 prompt（两个标题、时间和实体，约 200–300 token）；story 层面的跨语言复核再加约 100–300 次。
- 热度按不同发布方计数的做法不变，但应在 story 的 canonical ID 上统计（见第 5 节），并对“三家快讯同发”先去重，避免同一条快讯被重复计入热度。

### Gaps
- Google News 的内部聚类方法没有公开资料（只看到一项名为 “System for incrementally clustering news stories” 的美国专利 US 8,832,105，PDF 无法解析，受让人和机制都没有核实 — [USPTO](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/8832105)）。
- 上面两个小型开源项目的实际效果和规模没有核实。
- 没有找到公开的、规模和 infohub 相近（每天数千条、中英双语）的生产系统及其指标。

---

## 5. 少标注下的聚类质量评估，以及 cluster/story ID 的长期稳定

### Takeaway
B-cubed P/R/F1 是唯一满足四条形式约束（同质性、完整性、rag bag、簇大小 vs 簇数量）的外部指标，是新闻 story 聚类的标准评估方式；配合成对 P/R 和“预测簇数 / 真实簇数”比值，就能看出碎片化问题（B-cubed 本身对碎片化不敏感）。标注少时，按模型打分分层抽样，人工标几百个成对样本，外加少量完整标注的 story，就足以校准阈值和比较版本。story ID 的稳定性：增量分配的算法天然稳定；合并时保留旧 ID 作为别名；只有批量重聚类时才需要用最大重叠（Hungarian/Jaccard）把新簇映射回旧 ID。

中文要点：评估用“成对 P/R + B-cubed + 簇数比”三件套；标注时按分数分层抽 300–500 对，再完整标 10–20 个 story。story ID 创建后不改，合并时记 merged_into 别名，热度按 canonical ID 汇总。

### Cited Findings
- Amigó, Gonzalo, Artiles & Verdejo（2009，Information Retrieval 12:461–486）：给外部聚类评估指标定义了四条形式约束，即同质性、完整性、rag bag（对单例被错放的鲁棒性）、簇大小 vs 簇数量。只有 B-cubed 四条全部满足，基于熵的指标（互信息、V-measure）和 Rand 指数都不满足。文中还给出了适用于重叠聚类的扩展 B-cubed。 — [Springer](https://link.springer.com/article/10.1007/s10791-008-9066-8)；[Semantic Scholar](https://www.semanticscholar.org/paper/A-comparison-of-extrinsic-clustering-evaluation-on-Amig%C3%B3-Gonzalo/08451d088401125eb6a1f423d9f79bf8b6c4c404)
- 现成实现：python-bcubed（Apache-2.0，支持扩展 B-cubed）。 — [hhromic/python-bcubed](https://github.com/hhromic/python-bcubed)
- 成对 P/R/F1 的定义（Miranda）：tp 是被正确放到一起的文档对，fp 是被错误放到一起的对，fn 是本该在一起却被分开的对。跨语言评估按同样方式，在“簇的簇”层面计数。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)
- B-cubed 公式：一篇文档的 P 是它所在预测簇中同 gold 标签文档的比例，R 是它所在 gold 簇中被分到同一预测簇的比例，再对所有文档取平均。 — [Santos et al. 2022](https://arxiv.org/abs/2204.13418)
- B-cubed 对碎片化不敏感：Saravanakumar 的模型产生 276 个簇（真实为 222），之前的 SOTA 产生 484 个，但 B-cubed 只差 0.35。作者建议同时报告簇数和多个指标。与 Miranda 2018 相比，B-cubed 只高 2.40（94.76 vs 92.36），CEAF-e 却高 7.36（76.93 vs 69.57），MUC、BLANC、V-measure、ARI、AMI、FMI 的差距则在 0.4–3 之间。 — [arXiv 2101.11059](https://arxiv.org/abs/2101.11059)
- SemEval-2022 Task 8 用分级的成对相似度标注，用 Pearson 评估。标注者间一致性 Krippendorff α 为 0.77，最好的单个标注者与 gold 的相关为 0.88，可以当作人类上限参考。 — [SemEval 综述](https://aclanthology.org/2022.semeval-1.155.pdf)
- LLM 可以充当有噪声的标注者，为聚类生成成对约束，减少人工标注量。 — [Viswanathan et al., TACL 2024](https://aclanthology.org/2024.tacl-1.18/)
- 稳定 ID 的方法：用匈牙利算法匹配前后两次聚类结果，让重叠记录数最大，以便把旧 ID 继承下来 — [R-bloggers: Matching clustering solutions using the Hungarian method](https://www.r-bloggers.com/2012/11/matching-clustering-solutions-using-the-hungarian-method/)；也可以用簇间 Jaccard 指数衡量稳定性，在重采样后对每个簇取 Jaccard 的均值或中位数 — [Bioinformatics 2021](https://academic.oup.com/bioinformatics/article/37/15/2212/5962080)
- 反面教训：Kliping 原来对每个查询临时聚类，story 在时间上不稳定；改为全库增量、新文章到达时持续扩展的方式后才稳定下来。 — [arXiv 2508.08272](https://arxiv.org/abs/2508.08272)
- 在线分配类算法（Miranda、Telegram 冠军方案）每篇文档到达时只做一次决定：并入已有簇，或者新建簇。tgcontest 的批处理版本是在批间重叠的部分把簇合并起来。 — [Miranda PDF](https://homepages.inf.ed.ac.uk/scohen/emnlp18cluster.pdf)；[IlyaGusev/tgcontest](https://github.com/IlyaGusev/tgcontest)

### Inferences
- **少标注评估方案**：
  1. 成对样本 300–500 对：按当前打分分成高、灰、低三层分层抽样，单独留出一份跨语言对（约 100–200 对）。用它们计算成对 P/R，并扫阈值。
  2. 完整标注 10–20 个 story：在某个时间窗内把属于这些 story 的所有条目都找出来，用来计算 B-cubed 和“预测簇数 / 真实簇数”比，专门监控碎片化，也就是“改写稿没合并”的问题。
  3. 可以先让 GLM 预标注，再由人工裁定，以减少人工工作量（参照 Viswanathan 的思路）。
  4. 每次改动匹配器，都在同一份冻结的标注集上回放，比较新旧版本。
- **ID 策略**：story_id 一旦创建就不再改变；合并时保留较早或较大的 story 作为 canonical，另一个记 `merged_into`，并在查询时用并查集解析；拆分很少发生，拆出的部分给新 ID。如果某次需要批量重聚类，比如换了 embedding 模型，就用最大重叠（Jaccard ≥0.5 或匈牙利匹配）把新簇映射回旧 ID。热度、发布方计数和对外链接一律以 canonical ID 为准。

### Gaps
- 没有找到专门讨论“新闻 story ID 在合并/拆分下如何保持稳定”的论文或开源实现，上面的策略来自通用聚类匹配方法加推断。
- 没有找到在新闻 story 聚类上量化“用 LLM 预标注能省多少人工”的研究。
