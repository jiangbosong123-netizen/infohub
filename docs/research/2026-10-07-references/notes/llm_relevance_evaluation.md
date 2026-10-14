# LLM-based news relevance filtering, importance scoring, and evaluation with few labels / one annotator

Scope note for the report writer: the research ran on 2026-10-07. Abstracts were pulled from the arXiv API, and repo licenses come from the GitHub API on that date. "infohub" means the single-owner news site: GLM-4.6 assigns relevant/irrelevant, a 0–100 importance score (≥70 = selected), a category, and a Chinese summary. It handles about 4,000 items/day. The dataset has 600 cases (120 test / 120 dev / 360 train), with silver LLM labels on all of them. The owner is the only annotator, and a delayed self-recheck with κ ≥ 0.70 stands in for a second annotator. Bullets under "Inferences" are my reasoning or my own arithmetic, not claims from the sources.

## Q1. How well do LLMs work as annotators/judges for relevance and news classification, and which failure modes matter?

### Takeaway
Strong LLMs often match or beat crowd workers, and sometimes in-house annotators, on relevance-style labeling. On the same task they also tend to be more self-consistent than humans. But results depend heavily on the task and the prompt. Fine-tuned small classifiers still beat zero-shot LLMs on most fixed classification tasks. LLM judges are more lenient than humans about relevance, and they drift with prompt wording, rating scale and model version. Every source that measured it concludes the same thing: validate against human labels task by task, and keep the human labels as the evaluation truth. That is exactly what infohub's protocol does.

### Cited Findings
**The optimistic results (2023)**
- Gilardi, Alizadeh & Kubli (arXiv v2): on 2,382 tweets, zero-shot ChatGPT accuracy beat crowd workers on 4 of 5 tasks. The tasks included **relevance**, stance, topics and frames. ChatGPT's intercoder agreement beat both crowd workers and trained annotators on all tasks. Cost was under $0.003 per annotation, about 20× cheaper than MTurk — [arXiv 2303.15056](https://arxiv.org/abs/2303.15056)
- The published PNAS version used four samples of tweets and news articles (n = 6,183). There, ChatGPT's zero-shot accuracy beat crowd workers by about 25 percentage points on average. This figure is as summarized from the PNAS version; it does not appear in the arXiv v2 abstract — [PNAS 2023, 10.1073/pnas.2305016120](https://www.pnas.org/doi/pdf/10.1073/pnas.2305016120)
- Törnberg (2023) classified the party of US politicians from 500 tweets (2020 election). ChatGPT-4 ran 5× at temperature 0.2 and 5× at 1.0, for 5,000 calls. It was compared with 10 MTurk "Master" workers per item and 2 political-science experts. GPT-4 beat every individual human and the crowd majority vote. Its Krippendorff's α was higher than the human coders', and higher still at low temperature. Its bias toward guessing "Democrat" was not significantly different from the experts'. The exact accuracy and α values appear only in the paper's figures — [arXiv 2304.06588](https://arxiv.org/abs/2304.06588)
- Thomas, Spielman, Craswell & Mitra (Microsoft Bing) deployed LLM relevance labelling at scale. They tuned the prompt against first-party gold labels from real searchers. The LLM labels were "as good as human labellers", better than third-party workers "for a fraction of the cost", and trained notably better rankers. Systematic prompt changes moved accuracy, and so did **simple paraphrases** — [arXiv 2309.10621](https://arxiv.org/abs/2309.10621)
- Zheng et al. (MT-Bench) found that GPT-4 as a judge agrees with human preferences over 80% of the time, the same level as human–human agreement. They named position, verbosity and self-enhancement biases — [arXiv 2306.05685](https://arxiv.org/abs/2306.05685)

**Critical replications and counter-evidence (2023–2026)**
- Reiss (2023) tested ChatGPT on a **news vs. not-news** classification of website texts. Consistency "can fall short of scientific thresholds for reliability". Minor prompt wording changes and even repeating identical input changed outputs. Pooling repeated runs improved reliability. Unsupervised use is "not recommended" — [arXiv 2304.11085](https://arxiv.org/abs/2304.11085)
- Pangakis, Wolken & Fasching replicated 27 annotation tasks from 11 datasets with GPT-4. Performance was "highly contingent" on the dataset and task, so they argue for validation on a task-by-task basis and released a workflow tool — [arXiv 2306.00176](https://arxiv.org/abs/2306.00176)
- Zhu et al. relabeled 5 social-computing datasets with ChatGPT. Average accuracy was 0.609; the best dataset (sentiment) reached 64.9%. Performance varied strongly by label — [arXiv 2304.10145](https://arxiv.org/abs/2304.10145)
- Kristensen-McLachlan et al. ran binary annotation tasks on tweets from US news media. ChatGPT and open-source LLMs varied significantly, and a supervised **DistilBERT generally outperformed both** — [arXiv 2311.05769](https://arxiv.org/abs/2311.05769)
- Ziems et al. tested 13 LLMs on 25 computational-social-science (CSS) benchmarks. On classification, LLMs "fail to outperform the best fine-tuned models" but reach "fair levels of agreement with humans" — [arXiv 2305.03514](https://arxiv.org/abs/2305.03514)
- Bucher & Martini (2024) found that fine-tuned smaller BERT-style models beat zero-shot GPT-3.5, GPT-4 and Claude Opus "in all cases". The tasks were sentiment, stance, emotions and party positions; the texts included **news** — [arXiv 2406.08660](https://arxiv.org/abs/2406.08660)
- Edwards & Camacho-Collados compared approaches on 16 classification datasets. Fine-tuning smaller LMs generally beat zero- and few-shot LLMs — [arXiv 2403.17661](https://arxiv.org/abs/2403.17661)
- Alizadeh et al. tested stance, topic and **relevance** classification on news articles and tweets. Fine-tuned open-source LLMs matched or surpassed zero-shot GPT-3.5/GPT-4, but lagged fine-tuned GPT-3.5. Fine-tuning beat few-shot prompting "with a relatively modest quantity of annotated text" — [arXiv 2307.02179](https://arxiv.org/abs/2307.02179)
- Baumann et al. (2025), "LLM hacking", replicated 37 annotation tasks from 21 studies, covering 13M labels from 18 LLMs and 2,361 hypotheses. **About 31% of hypotheses reached incorrect conclusions with state-of-the-art LLMs, and about 50% with small models.** A handful of prompt paraphrases can make "virtually anything" significant. Of 21 mitigations, human annotations gave crucial protection against false positives — [arXiv 2509.08825](https://arxiv.org/abs/2509.08825)
- Törnberg's 2024 best-practice paper calls for prompt stability analysis, structured prompting and rigorous validation — [arXiv 2402.05129](https://arxiv.org/abs/2402.05129)

**Relevance judgment in IR: the closest analogue to "is this item relevant to my interests?"**
- UMBRELA is an open-source reproduction of the Bing assessor using GPT-4o. Its judgments correlate highly with system rankings across TREC Deep Learning 2019–2023 — [arXiv 2406.06519](https://arxiv.org/abs/2406.06519)
- TREC 2024 RAG Track (77 runs from 19 teams): UMBRELA-induced system rankings correlated highly with fully manual judgments. LLM *assistance* to humans did not raise that correlation. **"Human assessors appear to be stricter than UMBRELA"**, meaning the LLM is more lenient about relevance — [arXiv 2411.08275](https://arxiv.org/abs/2411.08275)
- Clarke & Dietz rebutted this. A system deliberately built to exploit the LLM metric got inflated scores. If systems also use the judge as a re-ranker, circularity distorts rankings (Kendall's τ). LLM "narcissism" is a further risk — [arXiv 2412.17156](https://arxiv.org/abs/2412.17156)
- Soboroff (keynote at the SIGIR 2024 LLM4Eval workshop): "don't use LLMs to create relevance judgments for TREC-style evaluations" — [arXiv 2409.15133](https://arxiv.org/abs/2409.15133)
- Faggioli et al. describe a human–machine collaboration spectrum for relevance judgment, from fully manual to fully automatic — [arXiv 2304.09161](https://arxiv.org/abs/2304.09161)

**Judge failure modes**
- Position bias: by reordering two answers, Vicuna-13B could "beat" ChatGPT on 66 of 80 queries when ChatGPT was the judge. Proposed fixes are Multiple Evidence Calibration, Balanced Position Calibration (score both orders) and human-in-the-loop on high-entropy cases — [arXiv 2305.17926](https://arxiv.org/abs/2305.17926); [code](https://github.com/i-Eval/FairEval)
- Thakur et al. tested 13 judge models. Only the largest aligned reasonably well with humans, and scores could still differ by up to 5 points. Judges are lenient and sensitive to prompt length and complexity. **High percent agreement can hide very different scores, so use chance-corrected metrics** — [arXiv 2406.12624](https://arxiv.org/abs/2406.12624)
- JUDGE-BENCH (20 datasets, 11 LLMs) found "substantial variance across models and datasets". LLMs "should be carefully validated against human judgments" — [arXiv 2406.18403](https://arxiv.org/abs/2406.18403)
- Stureborg et al. found familiarity bias (preference for low-perplexity text), "skewed and biased distributions of ratings", anchoring across multi-attribute judgments, and low inter-sample agreement — [arXiv 2405.01724](https://arxiv.org/abs/2405.01724)
- Vaugrante & Hagendorff (Oct 2026) compared 10 reasoning models. Changing **the rating scale alone shifted measured bias by up to 0.93 points**. Detailed prompts dropped judge leniency by 28.9 percentage points, and switching models changed it by up to 56.1 percentage points. **Model identity was the dominant source of variance**, while average binary accuracy was 96.5% — [arXiv 2610.05094](https://arxiv.org/abs/2610.05094)
- Version drift: GPT-4 fell from 84% to 51% accuracy on prime identification between the March and June 2023 versions, with "substantial" behavior changes in the "same" service — [arXiv 2307.09009](https://arxiv.org/abs/2307.09009)
- Criteria drift: in EvalGen, users needed criteria to grade outputs, but grading outputs changed their criteria. Some criteria depend on the outputs seen — [arXiv 2404.12272](https://arxiv.org/abs/2404.12272)
- Surveys: Gu et al., "A Survey on LLM-as-a-Judge" [arXiv 2411.15594](https://arxiv.org/abs/2411.15594); Tan et al., "LLMs for Data Annotation and Synthesis" [arXiv 2402.13446](https://arxiv.org/abs/2402.13446)

### Inferences
- infohub's rule that silver labels never serve as evaluation truth matches the consensus of Pangakis, Baumann, Bavaresco and Soboroff. Gilardi-style headline numbers do not transfer to a new task without validation.
- TREC 2024 found LLMs more lenient than humans. Expect GLM-4.6 to over-label items as "relevant" relative to the owner. The first metric to watch on the owner-labeled test split is the false-positive rate, i.e. precision of "relevant" and of "selected ≥70".
- Treat the labeler as a versioned artifact (infohub already versions the silver labeler). Pin model id, prompt hash, temperature and scale. Re-run a fixed canary set (for example the 120 dev items) whenever GLM, the prompt or the scale changes, and diff the label distribution. Chen et al. and Vaugrante & Hagendorff show model identity and scale choice can move outputs a lot.
- Set temperature low and measure self-consistency. Run the labeler 2–3× on the dev set and report LLM–LLM κ alongside human–LLM κ, as in Törnberg and Reiss.
- Thakur et al. warn about percent agreement. Report Cohen's κ (or Gwet's AC1) and per-class precision/recall, not just raw agreement, because "irrelevant" likely dominates the class mix.

### Gaps
- No study found on GLM-4.x or Chinese-language news specifically as an annotator. All the agreement numbers above are for GPT-4-class models on English data.
- Törnberg's exact accuracy and α values appear only in figures, so they were not extracted.
- No replication found that measures LLM relevance labels against a *single owner's* personal interest profile. That is closer to recommendation than to TREC relevance.

## Q2. Cost reduction: cascades, distillation into small classifiers, batching and caching, embeddings + logistic regression

### Takeaway
The most-supported pattern is a learned cascade with a distilled student. A cheap model trained on the LLM's own labels (logistic regression on embeddings, SetFit, or a fine-tuned encoder) handles confident items, and only uncertain items go to the LLM. Reported savings are 50–98% at matched accuracy. Fine-tuned small models often beat zero-shot LLMs on fixed label sets anyway. Batch APIs (−50%), input caching (cached input about 1/5 of the price on GLM-4.6) and multi-item prompts cut the remaining LLM cost further.

### Cited Findings
- FrugalGPT (Chen, Zaharia & Zou) learns which combination of LLMs to call per query. It matches the best single LLM (GPT-4) "with up to 98% cost reduction", or gains 4% accuracy at the same cost. It groups strategies into prompt adaptation, LLM approximation (caching / model fine-tuning) and LLM cascades — [arXiv 2305.05176](https://arxiv.org/abs/2305.05176)
- **Online cascade learning (streams)** starts with low-capacity models such as logistic regression and ends with an LLM. A learned deferral policy decides which model handles each item. The small models are updated online by imitating LLM outputs (a no-regret imitation-learning setup). Across 4 benchmarks it "parallels LLMs in accuracy while cutting down inference costs by as much as 90%", and is robust to distribution shift — [arXiv 2402.04513](https://arxiv.org/abs/2402.04513)
- **Cache & Distil / "neural caching"** (ACL Findings 2024) continuously trains a student on the LLM's responses for classification tasks. A policy decides which requests the student answers alone. Margin Sampling and Query-by-Committee gave "consistent benefits across tasks and budgets" — [arXiv 2310.13561](https://arxiv.org/abs/2310.13561)
- Cascade deferral rules (Gupta et al., Google): for classification, deferral based on predicted-class uncertainty is favored theoretically and practically. For generative outputs, naive sequence uncertainty has length bias. Learned post-hoc deferral using token-level uncertainty and small-model embeddings does better — [arXiv 2404.10136](https://arxiv.org/abs/2404.10136)
- Wang et al. (2021): to reach the same downstream-model performance, GPT-3 labels cost **50–96% less** than human labels. Mixing GPT-3 pseudo-labels with a few human labels does better still — [arXiv 2108.13487](https://arxiv.org/abs/2108.13487)
- Distilling step-by-step uses LLM rationales as extra supervision. A fine-tuned 770M T5 beat few-shot 540B PaLM using 80% of the data — [arXiv 2305.02301](https://arxiv.org/abs/2305.02301)
- RankGPT: GPT-4 ranking can be distilled ("permutation distillation") into a 440M model that beats a 3B supervised model on BEIR — [arXiv 2304.09542](https://arxiv.org/abs/2304.09542)
- DIRAS fine-tunes open 8B LLMs to annotate nuanced, domain-specific relevance with **calibrated relevance scores**. It reaches "GPT-4-level performance" on unseen (query, document) pairs, and its motivating example is "Tell me good news in the stock market today." Code: github.com/EdisonNi-hku/DIRAS — [arXiv 2406.14162](https://arxiv.org/abs/2406.14162)
- LLMaAA puts the LLM inside an active-learning loop as the annotator. Task models trained on LLM labels "can outperform the teacher within only hundreds of annotated examples" (tested on NER and relation extraction) — [arXiv 2310.19596](https://arxiv.org/abs/2310.19596)
- SetFit (contrastive fine-tuning of a sentence transformer, then a classification head):
  - With 8 labeled examples per class on Customer Reviews, it is "competitive with fine-tuning RoBERTa Large on the full training set of 3k examples".
  - On RAFT it scored 71.3% (355M params), versus GPT-3 at 62.7% (175B), T-Few at 75.8% (11B) and the human baseline at 73.5%.
  - Training took 30 s on a V100 (about $0.025), versus about 11 min and $0.7 for T-Few 3B.
  - It was tested multilingually, including Mandarin.
  - Sources: [HF blog](https://huggingface.co/blog/setfit); [arXiv 2209.11055](https://arxiv.org/abs/2209.11055); Apache-2.0 per the [GitHub API](https://github.com/huggingface/setfit)
- Fine-tuned small models beat zero-shot LLMs on fixed tasks: Bucher & Martini [arXiv 2406.08660](https://arxiv.org/abs/2406.08660); Edwards & Camacho-Collados [arXiv 2403.17661](https://arxiv.org/abs/2403.17661); Kristensen-McLachlan et al. (DistilBERT) [arXiv 2311.05769](https://arxiv.org/abs/2311.05769)
- Batch prompting puts several samples in one prompt. It gave up to 5× token and time savings with 6 samples per batch at "better or comparable" accuracy. Gains depend on batch size and task complexity — [arXiv 2301.08721](https://arxiv.org/abs/2301.08721)
- BatchPrompt found that naive batching in long contexts "will inevitably lead to worse performance", and that results depend on item position and order. Batch Permutation and Ensembling (BPE) recovers accuracy. At batch size 32 it used 9–16% of the LLM calls and 18.6–30.8% of the tokens:
  - BoolQ: 90.6 → 90.9 accuracy
  - QQP: 87.2 → 88.4
  - RTE: 91.5 → 91.1
  - Source: [arXiv 2309.00384](https://arxiv.org/abs/2309.00384)
- GLM pricing on Z.ai (international), per 1M tokens:
  - GLM-4.6: input $0.6, cached input $0.11 (cache storage free for a limited time), output $2.2.
  - GLM-4.5-Air: $0.2 / $0.03 / $1.1.
  - GLM-4.5-Flash: free.
  - No batch discount is listed on this page.
  - Source: [Z.ai pricing docs](https://docs.z.ai/guides/overview/pricing)
- Zhipu BigModel (China) Batch API: "价格是标准 API 的 50%" (half the standard API price). Jobs are expected to finish within 24 h and are cancelled after 7 days. Each file holds at most 50,000 requests and 100 MB. The page lists GLM-4-Air, GLM-4-FlashX, GLM-4-Plus, GLM-4-0520 and GLM-4, and does **not** explicitly list GLM-4.6 — [BigModel batch docs](https://docs.bigmodel.cn/cn/guide/tools/batch)

### Inferences
- **Cascade for infohub's relevance filter.** Stage 0: multilingual embeddings plus logistic regression (or SetFit), trained on silver labels for the 360 train cases plus a large pool of GLM-labeled production items. Silver labels in train are allowed by the protocol. Auto-decide only the confident tail, e.g. "clearly irrelevant" at p < 0.05. Send the uncertain middle and every candidate for "selected" to GLM-4.6. Online cascade learning and neural caching both suggest retraining the student continuously on GLM outputs and using margin sampling to pick which items GLM sees.
- **Gate the expensive work on relevance.** Translation and summarization (output tokens at $2.2/M) are likely the dominant cost. Generate the Chinese summary only after the item passes the relevance gate, or only for items scoring ≥ some threshold. A cheap first pass can be a short classification-only prompt with a cached system prompt.
- **Illustrative arithmetic (my assumptions, not measured).**
  - Assume 4,000 items/day, about 800 input and about 300 output tokens per item, at Z.ai list prices: input ≈ 3.2M × $0.6 = $1.92 and output ≈ 1.2M × $2.2 = $2.64, so about $4.6/day.
  - Caching 500 of the 800 input tokens (a shared system prompt and rubric) cuts input to about $0.94.
  - Cutting 50% of items before summary generation roughly halves output cost.
  - Batch API at 50% (if it supports GLM-4.6) halves whatever remains, at the cost of up to 24 h latency. It fits backfills and re-labeling runs, not live triage.
- **Multi-item prompts.** For the cheap first pass, try 5–10 headlines per prompt. Use a randomized order, or score each item in two different orders (BatchPrompt's permutation-and-ensemble), and check on the dev set that per-item decisions don't move.
- **Watch the student on distribution shift.** News topics shift daily, so track the student's deferral rate and its agreement with GLM on a small random audit stream. Online cascade learning reports robustness to shift, but only on its benchmarks.

### Gaps
- Not confirmed whether BigModel's Batch API supports GLM-4.6. The docs page lists only GLM-4 series models. Domestic CNY prices for GLM-4.6 were not verified: a search snippet claimed ¥5/¥5 per M tokens, but I could not confirm it on the official page, so treat it as unverified.
- No published cascade study specifically on multilingual (zh/en) news relevance with cost numbers.
- Prompt-cache hit rules for GLM (prefix length, TTL) were not verified.

## Q3. Importance/newsworthiness scoring: prediction, LLM ranking (pairwise vs pointwise), calibrating 0–100 scores

### Takeaway
Little published work measures "news importance" directly with LLMs. The closest are newsworthiness prediction (68% F1 against expert journalists) and important-news detection datasets, which report high annotator disagreement. Ranking research consistently finds pointwise scoring the cheapest but the weakest at ordering, and pairwise or setwise prompting more accurate but costlier. Pairwise judgments are also more manipulable by distractors. Recent work finds coarse scales (0–5) align best with humans, and that changing the scale shifts outcomes. A raw 0–100 LLM score is therefore poorly calibrated by default. The ≥70 threshold should be fitted against owner labels, not taken at face value.

### Cited Findings
- Spangher et al. define "newsworthiness prediction": will a local policy item get covered by the SF Chronicle? With expert journalists judging, the systems identified newsworthy policies at **68% F1**, and coverage recommendations had an 84% win-rate — [arXiv 2311.09734](https://arxiv.org/abs/2311.09734); earlier lead-generation work [arXiv 2104.09653](https://arxiv.org/abs/2104.09653)
- KhabarChin (Persian) has 7,869 news articles annotated for "important news", defined as news significant for a considerable portion of society and capable of influencing their mindset or decision-making. It reported "high disagreement and imbalance between classes" as the main challenges — [arXiv 2312.03361](https://arxiv.org/abs/2312.03361)
- Market-relevance proxy for equities: on post-cutoff headlines, GPT-4 scores achieved "approximately 90% portfolio-day hit rates" for the initial market reaction and predicted subsequent drift, especially for small stocks and negative news — [arXiv 2304.07619](https://arxiv.org/abs/2304.07619)
- Pairwise Ranking Prompting (PRP) on TREC-DL 2019/2020: Flan-UL2 (20B) performed on par with the GPT-4-based previous best. On BEIR, PRP beat pointwise LLM methods by more than 10% average NDCG@10. Linear-complexity variants remain competitive — [arXiv 2306.17563](https://arxiv.org/abs/2306.17563)
- Setwise ranking (comparative study): pointwise approaches "score high on efficiency, [but] suffer from poor effectiveness". Pairwise approaches are more effective but incur "high computational overhead". Setwise reduces LLM calls and tokens while keeping effectiveness. Code: github.com/ielab/llm-rankers — [arXiv 2310.09497](https://arxiv.org/abs/2310.09497)
- PairS ranks texts globally from local LLM pairwise comparisons, with uncertainty-guided search. It showed "significant improvements over direct scoring"; debiased pairwise calibration helps further — [arXiv 2403.16950](https://arxiv.org/abs/2403.16950)
- Counterpoint (Tripathi et al. 2025): pairwise protocols are more vulnerable to distractor features. Preferences flipped in about **35% of cases for pairwise vs 9% for absolute scores** — [arXiv 2504.14716](https://arxiv.org/abs/2504.14716)
- Scale granularity (Li et al., Jan 2026): rated with humans and LLMs on 3 scales across 6 benchmarks, measured by intraclass correlation (ICC). **0–5 gave the strongest human–LLM alignment overall.** Scale choice "substantially shifts human-LLM agreement", and the best scale can depend on the benchmark. The scales compared were 0–5, 0–10 and 0–100. That 0–10 was weakest comes from a secondary summary of the paper — [arXiv 2601.03444](https://arxiv.org/abs/2601.03444); [code](https://github.com/Harvard-AI-and-Robotics-Lab/LLM-Judge)
- Changing the rating scale alone shifted measured bias by up to 0.93 points, and model identity dominated variance — [arXiv 2610.05094](https://arxiv.org/abs/2610.05094)
- LLM evaluators produce "skewed and biased distributions of ratings" and show anchoring across multi-attribute judgments. Asking for importance *and* category *and* relevance in one prompt is a multi-attribute setup — [arXiv 2405.01724](https://arxiv.org/abs/2405.01724)
- G-Eval combines chain-of-thought with a form-filling rubric. It weights the candidate scores by the probabilities of the model's output tokens, and reached Spearman 0.514 with humans on summarization. The paper flags a bias toward LLM-generated text — [arXiv 2303.16634](https://arxiv.org/abs/2303.16634)
- For RLHF models, verbalized confidence was typically better calibrated than token probabilities, often cutting expected calibration error by about 50% relative (TriviaQA, SciQ, TruthfulQA) — [arXiv 2305.14975](https://arxiv.org/abs/2305.14975)
- Prediction-powered ranking builds rank-sets from a small set of human pairwise comparisons plus a large set of LLM ones, with coverage guarantees. Rank-sets built from LLM comparisons alone were often inconsistent with human preferences — [arXiv 2402.17826](https://arxiv.org/abs/2402.17826)

### Inferences
- **Pointwise first, pairwise only near the threshold.** At 4,000 items/day, full pairwise ranking is too expensive. Keep the cheap pointwise score for everything. Then run setwise or pairwise comparisons only inside a daily candidate band, e.g. scores 55–85, to order the top-N and decide borderline "selected" items. Pairwise flips under distractors (Tripathi), so use both orders (Balanced Position Calibration) for pairwise calls.
- **Replace the 0–100 output with a coarse rubric.** Ask GLM for a 0–5 (or 1–7) importance level with explicit anchors: 5 = market-moving for a covered ticker or a major AI/robotics release; 0 = no impact. If the API exposes logprobs, compute an expected score from token probabilities as G-Eval does. Otherwise run 2–3 samples and average. Then map to 0–100 for display. This targets the skew and anchoring findings.
- **Calibrate the threshold on owner labels.** Fit isotonic or logistic (Platt) regression from LLM score to P(owner says "selected") on the train/dev labels the owner produces, then set the cut by target precision. Isotonic regression needs more data; with only about 100–200 owner labels, a 1-parameter logistic fit is safer. Re-fit when GLM, the prompt or the scale changes.
- **Separate the prompts.** Asking for relevance, category and importance in separate calls (or a small structured schema with relevance first) may reduce anchoring. This should be tested on dev, not assumed.
- **Treat importance as personal.** KhabarChin's high disagreement suggests importance is subjective. Infohub's "truth" is the owner's ranking, so the owner's own labels or pairwise picks are the only valid target. Gathering a few owner pairwise choices per day (e.g. "which of these two matters more?") may give more reliable signal than asking the owner for 0–100 numbers.

### Gaps
- No paper found that evaluates LLM 0–100 importance scores for news against human editors with calibration numbers. Nor a public benchmark of "news importance" for tech/finance in Chinese and English.
- The scikit-learn-style recipe (isotonic vs Platt on small samples) is standard ML practice, not verified against a news-specific study in this research.
- Whether GLM-4.6 exposes token logprobs for G-Eval-style expected scores was not checked.

## Q4. Evaluation with few labels and one annotator: sample sizes and CIs, stratified/active selection, prediction-powered inference

### Takeaway
With 120 owner-labeled test items, classical 95% confidence intervals are about ±6 points on overall accuracy, and ±10–14 points on precision of a minority class with 30–60 positives. Self-recheck κ is also unstable when "relevant" is rare. Three families of methods make each owner label count more, all with valid intervals:
- Stratified or importance sampling of what the owner labels: StratPPI, OASIS, active testing.
- Combining owner labels with the 600 to 4,000/day silver/LLM labels: PPI, PPI++, AutoEval, confidence-driven inference.
- Active label selection: active statistical inference.

Gains are real but bounded: about 25–50% effective-sample savings in practice, and at most 2× when the judge is not better than the system it evaluates. In the very-few-label regime, PPI++ can even lose to the classical interval.

### Cited Findings
**Prediction-powered inference (PPI) family**
- PPI gives provably valid confidence intervals for means, quantiles and regression coefficients by combining a small gold-labeled set with many ML predictions. It makes no assumptions about the predictor, and better predictions give tighter intervals — [arXiv 2301.09633](https://arxiv.org/abs/2301.09633) (Science 2023)
- PPI++ adapts automatically to prediction quality, and its intervals "always improve on classical intervals using only the labeled data" (asymptotically) — [arXiv 2311.01453](https://arxiv.org/abs/2311.01453)
- Eyre & Madras: **"when labelled data is scarce, the PPI++ method can perform even worse than classical inference."** They propose robust-regression PPI variants for the few-label regime — [arXiv 2411.12665](https://arxiv.org/abs/2411.12665)
- Cross-PPI lets the same small labeled set both train the predictor and do inference via cross-fitting. It is more powerful than splitting the data and gives more stable intervals — [arXiv 2309.16598](https://arxiv.org/abs/2309.16598)
- Stratified PPI (Google): stratification plus sample allocation gives "substantially tighter" intervals when autorater quality varies across strata — [arXiv 2406.04291](https://arxiv.org/abs/2406.04291)
- AutoEval Done Right uses PPI-style estimators on GPT-4-labeled data to increase the effective human-labeled sample size "by up to 50%" while staying unbiased — [arXiv 2403.07008](https://arxiv.org/abs/2403.07008)
- Active statistical inference uses model uncertainty to choose which points get human labels, and relies on predictions where the model is confident. It gives valid CIs with far fewer labels than non-adaptive collection — [arXiv 2403.03208](https://arxiv.org/abs/2403.03208)
- Confidence-Driven Inference (Gligorić et al.) uses LLM annotations plus LLM confidence to select which human annotations to collect. CIs are valid and never worse than human-only; it cut needed human annotations by **over 25%** on politeness, stance and bias tasks — [arXiv 2408.15204](https://arxiv.org/abs/2408.15204)
- Design-based supervised learning (DSL): using surrogate labels directly "leads to substantial bias and invalid confidence intervals, even with high surrogate accuracy of 80–90%". DSL is a doubly-robust correction that requires a *known* probability of sampling each document for gold labeling — [arXiv 2306.04746](https://arxiv.org/abs/2306.04746)
- **Upper bound (Dorner, Nastl & Hardt):** "when the judge is no more accurate than the evaluated model, no debiasing method can decrease the required amount of ground truth labels by more than half". Practical savings were "even more modest" — [arXiv 2410.13341](https://arxiv.org/abs/2410.13341)
- Baumann et al. found that human annotations are the key protection against false-positive conclusions, and that regression-correction estimators trade Type I against Type II error — [arXiv 2509.08825](https://arxiv.org/abs/2509.08825)

**Active / label-efficient testing**
- Active testing (Kossen et al., ICML 2021) selects test points to label using acquisition functions tailored to evaluation rather than to training. Active selection biases the estimate, and they give a weighted estimator that removes the bias while reducing variance — [arXiv 2103.05331](https://arxiv.org/abs/2103.05331)
- Active Surrogate Estimators interpolate errors on unlabeled points with a surrogate model, using the XWED acquisition. They are more label-efficient than active testing — [arXiv 2202.06881](https://arxiv.org/abs/2202.06881)
- OASIS uses adaptive importance sampling to estimate precision, recall and F under extreme class imbalance, with consistency guarantees. It cut labels by **up to 83%** without loss of estimate accuracy (entity resolution) — [arXiv 1703.00617](https://arxiv.org/abs/1703.00617)
- tinyBenchmarks: 100 curated examples sufficed to estimate MMLU (14K items) performance accurately. This shows the value of choosing *which* items to label — [arXiv 2402.14992](https://arxiv.org/abs/2402.14992)

**Agreement and label quality with one annotator**
- Sim & Wright (2005) say kappa depends on prevalence and bias. Sample size for kappa studies depends on the null κ and the proportion of positive ratings. Larger samples are needed when positives are scarce or abundant, and they give sample-size tables — [Physical Therapy 85(3):257–268, via Univ. Birmingham](https://research.birmingham.ac.uk/en/publications/the-kappa-statistic-in-reliability-studies-use-interpretation-and/)
- Kappa paradoxes: kappa can be low despite high observed agreement when expected chance agreement is high, i.e. skewed prevalence (Feinstein & Cicchetti 1990; Cicchetti & Feinstein 1990). Reporting separate positive and negative agreement indexes avoids it — [J Clin Epidemiol, Part II](https://www.jclinepi.com/article/0895-4356(90)90159-M/abstract); [formal proof, Warrens 2010](https://link.springer.com/article/10.1007/s00357-010-9060-x)
- Label errors are common even in curated test sets: on average at least 3.3% across 10 benchmarks, and 51% of confident-learning-flagged candidates were confirmed as errors. Lower-capacity models can rank higher once labels are corrected — [arXiv 2103.14749](https://arxiv.org/abs/2103.14749)
- Criteria drift in EvalGen: grading outputs changes the grader's criteria — [arXiv 2404.12272](https://arxiv.org/abs/2404.12272)
- Small-n binomial intervals: Brown, Cai & DasGupta (2001) recommend Wilson or Jeffreys intervals over the standard Wald interval, which behaves erratically for small n and extreme p — [Statistical Science 16(2)](https://projecteuclid.org/journals/statistical-science/volume-16/issue-2/Interval-Estimation-for-a-Binomial-Proportion/10.1214/ss/1009213286.full)

### Inferences
- **What 120 test labels buy (my Wilson-interval arithmetic).**

  | Labels | Observed rate | 95% CI | Half-width |
  |---|---|---|---|
  | n = 120 | accuracy 0.85 | 0.775–0.903 | ±6.4 pp |
  | n = 120 | accuracy 0.90 | 0.833–0.942 | ±5.4 pp |
  | 30 predicted positives | precision 0.80 | 0.627–0.905 | ±14 pp |
  | 40 predicted positives | precision 0.80 | 0.652–0.895 | ±12 pp |
  | 60 predicted positives | precision 0.80 | 0.682–0.882 | ±10 pp |
  | n = 360 | 0.85 | — | ±3.7 pp |
  | n = 600 | 0.85 | — | ±2.9 pp |

  The 120-item test split is enough to detect gross failure, but not to separate two labeler versions that differ by a few points.
- **What the self-recheck κ ≥ 0.70 target means (my simulation).** Assume the owner re-labels with 94% raw self-agreement:
  - At 30% "relevant" prevalence: mean κ ≈ 0.86, 95% range 0.76–0.95 for n = 120, 0.66–1.0 for n = 40.
  - At 15% prevalence, the same 94% agreement gives mean κ ≈ 0.78, with a 95% range of 0.62–0.92 at n = 120 and 0.44–1.0 at n = 40.

  So re-check at least about 60–120 items, report raw positive and negative agreement alongside κ (kappa paradox), and pre-register that the κ CI, not just the point estimate, is compared to 0.70.
- **Stratify the human sample by LLM output.** Example strata: LLM-irrelevant; LLM-relevant with score < 70; score 70–85; score > 85; LLM-uncertain. Allocate owner labels with known inclusion probabilities and reweight. Oversample near the 70 threshold and the uncertain stratum. This is what StratPPI, OASIS and DSL need, and it gives valid precision/recall for "selected" despite the class imbalance. If infohub's existing 600-case sample was drawn with known strata, record those inclusion probabilities now.
- **Use PPI on top of the 600 silver labels and the production stream.**
  - Precision of "selected" can be estimated as a PPI mean of 1[owner agrees] over items GLM selected.
  - Recall needs a ratio: P(LLM selects ∧ owner positive) / P(owner positive), with PPI on both terms and the delta method or bootstrap.
  - ppi_py has `ppi_mean_ci` and Cross-PPI, plus a power-analysis notebook to choose labeled vs unlabeled sizes under a budget.
  - With about 120 labels, also compute the classical Wilson interval. Eyre & Madras show PPI++ can be worse in the very-few-label regime, so report both and prefer the narrower *valid* one by a pre-registered rule.
- **Be realistic about gains.** GLM-4.6 is both the production labeler and (presumably) the silver labeler's family. Dorner et al. imply PPI-type debiasing can at most halve the labels needed. AutoEval reports about 50% and confidence-driven inference about 25%. Plan for roughly 1.3–2× effective-sample gain, not 10×.
- **Active labeling for train/dev, not the blind test.** Keep the 120-item test split a fixed random or stratified sample, so it stays blind and unbiased. Use uncertainty or margin sampling (Cache & Distil, active statistical inference, confidence-driven inference) only to choose *additional* owner labels for train/dev or for a separate PPI/active-inference audit set with known selection probabilities.
- **Guard against criteria drift and memorization in the self-recheck.**
  - Freeze a written relevance and importance rubric before labeling the test split.
  - Re-label a random subset after a delay, shuffled, with silver labels and earlier answers hidden.
  - Log the timestamp and rubric version with each label.

  EvalGen's criteria-drift result suggests the first ~20 labels are where the rubric is still forming. Consider labeling a warm-up batch from train/dev first.
- **Run label-error detection on silver and owner labels.** Use cleanlab-style confident learning: out-of-fold probabilities from the cheap student against the given labels. Route flagged items back to the owner for adjudication. Northcutt et al. found about half of flags are real errors, so this is a cheap way to clean the 360 train items.

### Gaps
- No published guidance found on a minimum delay between the original labeling and the self-recheck for text relevance labeling, or on memory effects. That would need clinical test–retest literature, which was not searched.
- No paper found applying PPI or active testing specifically to news-feed relevance precision/recall with a single annotator. The methods are general, but there is no domain-specific evidence.
- Exact sample-size tables from Sim & Wright were not extracted (paywalled PDF).

## Q5. Tools: Argilla, Label Studio, cleanlab, ppi_py, and others — licenses and fit for a one-person workflow

### Takeaway
All the main open-source tools are permissively licensed as of 2026-10-07: Argilla, Label Studio, SetFit, UMBRELA and cleanlab are Apache-2.0; ppi_py and doccano are MIT. cleanlab moved from AGPL-3.0 to Apache-2.0 in December 2025. For a single owner with a few hundred labels, the statistical libraries (ppi_py, cleanlab, SetFit) give the most value. A full annotation server is optional, since infohub already has its own labeling launcher.

### Cited Findings
- Argilla ("collaboration tool for AI engineers and domain experts to build high-quality datasets"): Apache-2.0, about 5.1k stars, not archived, last push 2026-10-06 — [GitHub](https://github.com/argilla-io/argilla) (GitHub API, checked 2026-10-07)
- Label Studio (multi-type labeling tool with a standardized output format): Apache-2.0, about 28.4k stars, active (push 2026-10-06) — [GitHub](https://github.com/HumanSignal/label-studio)
- cleanlab ("data-centric AI package for data quality and ML with messy, real-world data and labels"):
  - Currently Apache-2.0, about 11.7k stars, last push 2026-01-13.
  - The LICENSE history shows "Change LICENSE to AGPL-3" on 2021-06-06, then "license (#1263)" on 2025-12-16, which is the current Apache-2.0.
  - Older pinned versions may still carry AGPL-3.0.
  - Source: [GitHub](https://github.com/cleanlab/cleanlab); [LICENSE history](https://github.com/cleanlab/cleanlab/commits/master/LICENSE)
  - Method basis: confident learning, [arXiv 1911.00068](https://arxiv.org/abs/1911.00068); the label-error study, [arXiv 2103.14749](https://arxiv.org/abs/2103.14749)
- cleanlab authors' BSDetector gives black-box LLM confidence scores by sampling multiple responses. It identifies wrong LLM answers better than alternative uncertainty methods and improves "reliable evaluation" in human-in-the-loop and automated settings — [arXiv 2308.16175](https://arxiv.org/abs/2308.16175)
- ppi_py:
  - MIT license, about 300 stars, last push 2026-04-10; `pip install ppi-python`.
  - API: `ppi_[ESTIMAND]_ci(...)` / `_pointestimate`, e.g. `ppi_mean_ci(Y, Yhat, Yhat_unlabeled, alpha)`; also quantiles, OLS and logistic regression, and mean p-values.
  - `cross_ppi.py` implements Cross-PPI, and a power-analysis notebook chooses labeled and unlabeled sizes under a budget.
  - Source: [GitHub](https://github.com/aangelopoulos/ppi_py)
- SetFit: Apache-2.0, about 2.8k stars, active — [GitHub](https://github.com/huggingface/setfit)
- doccano: MIT, about 10.8k stars, last push 2026-04-14 — [GitHub](https://github.com/doccano/doccano)
- UMBRELA (LLM relevance assessor, TREC 2024 RAG): Apache-2.0 — [GitHub](https://github.com/castorini/umbrela)
- Other code mentioned by the papers, with licenses not checked:
  - FairEval (position-bias calibration): [github.com/i-Eval/FairEval](https://github.com/i-Eval/FairEval)
  - llm-rankers (pointwise/pairwise/listwise/setwise): [github.com/ielab/llm-rankers](https://github.com/ielab/llm-rankers)
  - DIRAS: [github.com/EdisonNi-hku/DIRAS](https://github.com/EdisonNi-hku/DIRAS)
  - RankGPT: [github.com/sunnweiwei/RankGPT](https://github.com/sunnweiwei/RankGPT)
  - LLM-Judge grading-scale study: [github.com/Harvard-AI-and-Robotics-Lab/LLM-Judge](https://github.com/Harvard-AI-and-Robotics-Lab/LLM-Judge)
  - Bucher & Martini's fine-tuning toolkit: [arXiv 2406.08660](https://arxiv.org/abs/2406.08660)
  - Pangakis et al.'s validation software: [arXiv 2306.00176](https://arxiv.org/abs/2306.00176)

### Inferences
- **Recommended minimal stack for infohub:**
  - Keep the existing label.sh / worktree labeling flow for the blind test split.
  - Add **ppi_py** (MIT) for precision/recall/selection-rate CIs that combine owner labels with silver and production labels.
  - Add **cleanlab** (now Apache-2.0; pin a release at or after the Dec-2025 relicense) to flag likely label errors in silver train labels and in owner labels.
  - Add **SetFit** or sentence-embedding + logistic regression as the cascade student.
  - All are permissive, so there are no copyleft concerns even if infohub is later published.
- **Argilla and Label Studio fit if the owner wants a nicer UI.** Argilla's dataset and suggestion model fits "show the LLM suggestion and confirm or correct" for train/dev. But showing silver labels as pre-filled suggestions would break blinding, so the test split must be configured *without* suggestions. Both run as servers (Docker or Python), which is extra operational weight for one person. doccano (MIT) is a lighter alternative.
- **UMBRELA's prompt is a reusable template** for a graded (0–3) relevance prompt, if infohub moves from binary to graded relevance.

### Gaps
- Licenses of the paper code repos (FairEval, llm-rankers, DIRAS, RankGPT, LLM-Judge) were not checked.
- The reason for cleanlab's December 2025 relicense was not investigated, nor whether every cleanlab sub-package (e.g. its hosted TLM / "Trustworthy Language Model" API) is covered. The TLM is a commercial service and outside the OSS license.
- No head-to-head study found of annotation tools for a solo annotator (time per label, fatigue). Fit judgments above are inference.
