# Reliable news/feed crawling: projects, papers and techniques infohub can borrow

Scope note: all GitHub star counts, licenses, last-push dates and latest releases below were pulled from the GitHub REST API on 2026-10-07 (this session). "Probe" findings come from `curl` requests I ran against infohub's real feed URLs on 2026-10-07; the source link for those is the feed URL itself. Licenses matter because infohub is private: MIT/BSD/Apache code can be copied with attribution, but GPL/AGPL code (FreshRSS, RSSHub, tt-rss, GNE) should only be studied and reimplemented, not copied.

## 1. Feed polling efficiency and politeness: conditional GET, WebSub, ttl/skipHours, adaptive and learned schedules

### Takeaway
For about 60 feeds, the clear wins are (a) storing ETag/Last-Modified correctly and honouring `304`, `Retry-After`, `Cache-Control: max-age`/`Expires` and RSS `<ttl>`, and (b) a simple per-source interval based on how often new items appear, as Miniflux's `entry_frequency` scheduler and CommaFeed's "empirical" interval do. WebSub does nothing for infohub: none of the probed feeds advertise a hub, and it needs a public callback URL. The learned/RL revisit schedulers in the literature are built for millions of URLs under bandwidth limits and are overkill here. A clipped Poisson rate estimate per source gets most of the benefit.

### Cited Findings

**Probe of infohub's own feeds (2026-10-07):**
- TechCrunch sends `ETag` + `Last-Modified` + `Cache-Control: max-age=300, must-revalidate`. A conditional re-request returned **304** — [techcrunch.com/feed](https://techcrunch.com/feed/)
- 量子位 (WordPress) sends `ETag` + `Last-Modified`, and the conditional request returned **304** — [qbitai.com/feed](https://www.qbitai.com/feed)
- Google DeepMind blog sends `ETag` and `Cache-Control: public, max-age=60`, and the conditional request returned **304** — [deepmind.google/blog/rss.xml](https://deepmind.google/blog/rss.xml)
- Fed monetary-policy feed sends `ETag` + `Last-Modified`, and the conditional request returned **304** — [federalreserve.gov press_monetary.xml](https://www.federalreserve.gov/feeds/press_monetary.xml)
- IT之家 RSS sends **no validators** (`Cache-Control: private`), so conditional GET is impossible — [ithome.com/rss](https://www.ithome.com/rss/)
- Google News RSS search sends **no validators** and `Cache-Control: no-cache, no-store, max-age=0, must-revalidate`. Every poll of the 23 company feeds is a full download — [news.google.com/rss/search](https://news.google.com/rss/search?q=NVIDIA+when:2d&hl=en-US&gl=US&ceid=US:en)
- A control feed (Ars Technica) sends `Last-Modified` but answered a conditional request with **200**: the server ignores it, so the client must compare validators itself — [feeds.arstechnica.com](https://feeds.arstechnica.com/arstechnica/index)
- None of the probed feeds contained `<ttl>` or an `atom:link rel="hub"` (no WebSub). TechCrunch and 量子位 expose WordPress's `<sy:updatePeriod>` (syndication module) — same probe URLs as above.

**How mature readers implement it:**
- **Miniflux** (Go, Apache-2.0):
  - Sends `If-None-Match`/`If-Modified-Since` unless the per-feed `IgnoreHTTPCache` flag is set or the refresh is forced. — [handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/handler/handler.go)
  - `IsModified()` returns false on 304. On a 200 it also compares the returned ETag (or Last-Modified) with the stored value, which avoids re-parsing on servers that ignore conditional headers. — [response_handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/fetcher/response_handler.go)
- **Miniflux next-check delay:** `refreshDelay = max(feed TTL, Cache-Control max-age, Expires)`, then `interval = max(interval, refreshDelay)`. — [handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/handler/handler.go); [feed.go](https://github.com/miniflux/v2/blob/main/internal/model/feed.go)
- **Miniflux `entry_frequency` scheduler:**
  - `interval = 7 days / (weeklyEntryCount × SCHEDULER_ENTRY_FREQUENCY_FACTOR)`, clamped to [`SCHEDULER_ENTRY_FREQUENCY_MIN_INTERVAL` = 5 min, `..._MAX_INTERVAL` = 1440 min]. A weekly count of 0 uses the max. The default scheduler is `round_robin` (60 min–24 h).
  - Other defaults: `POLLING_LIMIT_PER_HOST` = 0 (disabled), `HTTP_CLIENT_TIMEOUT` = 20 s, `HTTP_CLIENT_MAX_BODY_SIZE` = 15 MiB, `WORKER_POOL_SIZE` = 16.
  - Sources: [Miniflux configuration docs](https://miniflux.app/docs/configuration.html); [feed.go](https://github.com/miniflux/v2/blob/main/internal/model/feed.go)
- **Miniflux on HTTP 429:** `ParseRetryDelay()` reads `Retry-After` as either seconds or an RFC1123 date, and the next check is pushed by that delay. — [response_handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/fetcher/response_handler.go); [handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/handler/handler.go)
- **FreshRSS** (PHP, AGPL-3.0) changelog:
  - 1.27.0 (2025-08-18) added `Cache-Control: max-age`/`Expires` support (#6812), plus "support for HTTP 429 Too Many Requests and 503 Service Unavailable, obey Retry-After" (#7760).
  - 1.27.1 includes the port in Retry-After scoping.
  - 1.28.0 (2025-12-24) added separate Retry-After rules for proxies and excludes local networks from domain-wide Retry-After.
  - 1.30.1 (2026-10-05) fixed domain-wide Retry-After.
  - 1.26.1 fixed a regression that broke conditional requests.
  - Source: [FreshRSS CHANGELOG](https://github.com/FreshRSS/FreshRSS/blob/edge/CHANGELOG.md)
- **reader** (lemon24, Python, BSD-3-Clause; latest release 3.26 on 2026-06-24):
  - Scheduled updates arrived in 3.13: global/per-feed `.reader.update` config, default interval "one hour", with optional jitter "such that updates don't happen exactly at the same time".
  - It honours `Retry-After` since 3.15 and `Cache-Control max-age`/`Expires` since 3.21. It uses ETag/Last-Modified.
  - It warns that failing to persist feed state means "you will repeatedly download feeds that have not changed… and the publisher may ban you".
  - Source: [reader guide](https://reader.readthedocs.io/en/latest/guide.html)
- **CommaFeed** (Java, Apache-2.0) `FeedRefreshIntervalCalculator`, with `intervalEmpirical` on by default:
  - Last publication ≥30 days ago → `maxInterval`; ≥14 days → `maxInterval/2`; ≥7 days → `maxInterval/4`.
  - Otherwise the interval is `averageEntryInterval / 2`, clamped to [`interval`, `maxInterval/4`].
  - On 304 it reuses the success path. Every result is clamped to [interval, maxInterval].
  - Source: [FeedRefreshIntervalCalculator.java](https://github.com/Athou/commafeed/blob/master/commafeed-server/src/main/java/com/commafeed/backend/feed/FeedRefreshIntervalCalculator.java); [CommaFeedConfiguration.java](https://github.com/Athou/commafeed/blob/master/commafeed-server/src/main/java/com/commafeed/CommaFeedConfiguration.java)
- **NewsBlur** (Python/Django, MIT) `get_next_scheduled_update()` sets base intervals from stories per day (`stories_last_month/30`) and weighted subscribers:
  - ≥1 story/day → 6 / 15 / 45 min depending on subscribers.
  - <1 story/day → `60 − spd×60` or `360 − spd×360` min.
  - 0 stories → 6–24 h, multiplied by months since the last story.
  - Push (WebSub) feeds get a ×12 polling penalty because push covers them. Notification-enabled feeds get ≤30 min. Caps are 4 h (premium) and 48 h (free).
  - Source: [NewsBlur rss_feeds/models.py](https://github.com/samuelclay/NewsBlur/blob/master/apps/rss_feeds/models.py)
- **newsnow** (TypeScript, MIT) uses two separate knobs per source: `interval` (Realtime = 2 min, Fast = 5 min, Default = 10 min) and a global cache `TTL` = 30 min. Within `interval` it serves the cache without fetching. The code comments explain that interval reflects how fast the content can change. — [shared/pre-sources.ts](https://github.com/newsnext/newsnow/blob/main/shared/pre-sources.ts); [shared/consts.ts](https://github.com/newsnext/newsnow/blob/main/shared/consts.ts); [server/api/s/index.ts](https://github.com/newsnext/newsnow/blob/main/server/api/s/index.ts)
- **feedparser** (Python, BSD-style "NOASSERTION" on GitHub; v6.0.14 2026-07-30): `parse(url, etag=..., modified=...)` returns `status == 304` with empty `entries` when nothing changed. — [feedparser docs: http-etag](https://github.com/kurtmckee/feedparser/blob/develop/docs/http-etag.rst)

**Specs:**
- HTTP conditional requests (`If-None-Match`, `If-Modified-Since`) and `Retry-After` are defined in [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110); caching semantics (`max-age`, `Expires`, `no-store`) are in [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111).
- RSS 2.0 defines channel `<ttl>` (minutes a channel may be cached) and `<skipHours>`/`<skipDays>` as hints that aggregators may skip — [RSS 2.0 spec](https://www.rssboard.org/rss-specification)
- **WebSub:**
  - Discovery is via `Link: rel="hub"` and `rel="self"` headers, or `<link>` elements in the feed.
  - The subscriber must expose a **publicly reachable callback**, echo `hub.challenge` for intent verification, accept `hub.lease_seconds`, and verify the `X-Hub-Signature` HMAC when it supplied a `hub.secret`.
  - The current W3C TR page shows a Recommendation dated 2 June 2026.
  - Source: [W3C WebSub](https://www.w3.org/TR/websub/)
- Feedbin implements WebSub with subscribe, unsubscribe and lease-maintenance jobs. — [feedbin app/jobs/web_sub](https://github.com/feedbin/feedbin/tree/main/app/jobs/web_sub)

**Papers (change-rate estimation and revisit scheduling):**
- **Cho & Garcia-Molina**, "Estimating frequency of change", ACM TOIT 3(3):256–290, 2003. It builds estimators for change frequency when a crawler only sees partial change history, and reports that a crawler could gain about 35% in freshness by adopting the proposed estimator. — [ACM DL](https://dl.acm.org/doi/10.1145/857166.857170); [Stanford InfoLab record](http://ilpubs.stanford.edu:8090/471/)
- **Avrachenkov, Patil, Thoppe**, "Online algorithms for estimating change rates of web pages" (Performance Evaluation; arXiv 2020):
  - The naive estimator (changes detected / time) converges to `pΔ/(Δ+p)`, not Δ, so it is **biased and inconsistent**.
  - The Cho–Garcia-Molina MLE solves `Σ I_j τ_j/(exp(Δτ_j) − 1) = Σ (1 − I_j) τ_j`. It is consistent but has no closed form and is unstable: no solution when no change was ever seen, ∞ when every visit saw a change. The suggested fix is clipping.
  - The authors propose O(1)-per-update online estimators (law-of-large-numbers and stochastic-approximation based) with accuracy comparable to the MLE.
  - Source: [arXiv 2009.08142](https://arxiv.org/abs/2009.08142)
- **Kolobov et al.** (Microsoft Research):
  - Papers: "Optimal Freshness Crawl Under Politeness Constraints" (SIGIR 2019), "Staying up to Date with Online Content Changes Using Reinforcement Learning for Scheduling" (NeurIPS 2019, LambdaCrawl), and "Online Learning for Active Cache Synchronization" (ICML 2020).
  - The released dataset covers about 18M URLs crawled by Bing over 14 weeks in 2017, including host politeness constraints.
  - Repo: MIT, 43 stars, last push 2025-01.
  - Source: [project page](https://microsoft.github.io/Optimal-Freshness-Crawl-Scheduling/); [GitHub](https://github.com/microsoft/Optimal-Freshness-Crawl-Scheduling)
- **Upadhyay, Busa-Fekete, Kotłowski, Pál, Szörényi**, "Learning to Crawl", AAAI 2020: change rates are unknown and observed only as binary "changed / not changed" signals. They use an estimator with confidence intervals and an explore-and-commit algorithm with O(√T) regret. — [arXiv 1905.12781](https://arxiv.org/abs/1905.12781)
- **Busa-Fekete et al.**, "A Scalable Crawling Algorithm Utilizing Noisy Change-Indicating Signals" (arXiv, Feb/Mar 2025): extends refresh crawling to use noisy "web pings" / change hints (with false positives and missed events) under a fixed bandwidth, without spikes, building on Azar et al. 2018's Poisson-optimal policy. — [arXiv 2502.02430](https://arxiv.org/abs/2502.02430)
- **Dang et al.**, "Look back, look around" (arXiv 2021/2022): to predict which pages (such as news hubs) will produce new outlinks, the best predictor was the **recent history of new outlinks** on the page and on content-related pages. They used NGBoost with a Poisson output. — [arXiv 2111.05062](https://arxiv.org/abs/2111.05062)

### Inferences
- **Conditional GET is a moderate bandwidth win, not a freshness win, for infohub.** It works on roughly the WordPress/CMS half of the ~35 RSS feeds (TechCrunch, 量子位, DeepMind, Fed all returned 304). It cannot help the 23 Google News feeds or IT之家, which together are probably the bulk of request volume: 23 feeds × 72 polls/day ≈ 1,650 full downloads/day.
- Borrow Miniflux's `IsModified` double check (304 *or* an unchanged validator on a 200), because some servers (Ars in the probe) ignore conditional headers.
- **Body-hash short-circuit.** For feeds with no validators, hash the response body (or the sorted list of entry IDs) and skip parsing and DB work when it is unchanged. This is my extrapolation of the Miniflux/newsnow pattern; neither project does exactly this for validator-less feeds.
- **Adaptive per-source interval is the biggest politeness and efficiency lever**, especially for Google News. Estimate λ (new items per hour) per company from infohub's own insert history, then set `interval = clamp(k / λ, 20 min, 120 min)`, Miniflux/CommaFeed style. Quiet companies drop to 1–2 h; busy ones stay at 20 min. This lowers the request rate that probably drove Yahoo's 429s.
- Always apply `max(own interval, Retry-After, Cache-Control max-age, ttl)` as Miniflux and reader do. Add ±10–20% jitter (reader) so the 23 Google News feeds do not fire in one burst.
- **Interval must also respect the feed window.** If a feed returns at most W items and the source peaks at λ_peak items per minute, then `interval < W / λ_peak` (with margin), otherwise items fall off the window. This is the real risk for a news aggregator, which cares more about not missing items than about freshness of a cached copy. infohub's flash-feed catch-up already addresses it for Sina, CLS and WallstreetCN.
- **Do not use the naive "changes/time" estimator.** When the poll interval is long relative to the change rate, it underestimates rate. For a news feed you can count *new items* per poll directly (an incomplete change history doesn't apply), so a windowed mean of new-items-per-hour with a floor/ceiling is enough. RL schedulers (LambdaCrawl and similar) solve a bandwidth-allocation problem infohub doesn't have.
- WebSub/PubSubHubbub: skip. No probed infohub feed advertises a hub, and a home Windows host would need a public HTTPS callback (tunnel) plus lease renewal. NewsBlur's ×12 polling penalty for push feeds shows that even push adopters keep polling as a fallback.
- `skipHours` and `ttl` are absent from infohub's feeds. Still worth parsing (Miniflux does), but it won't change behaviour today.

### Gaps
- I could not open the Cho & Garcia-Molina PDF (UCLA host refused the connection; the Stanford link returned HTML), so the exact "improved estimator" formula is not verified here. From memory, it is commonly cited as `r̂ = −log((n − X + 0.5)/(n + 0.5))` per interval for n regular visits with X detected changes. Check the TOIT paper before quoting it.
- I did not check whether Google News RSS search results are strictly chronological. If they are relevance-ordered, interval tuning cannot guarantee completeness.
- One blog says a Google News RSS feed holds about 100 items ([cloro.dev](https://cloro.dev/blog/google-news-rss/)); infohub's notes say up to 50. This conflict is unresolved and may depend on the query or the `when:` window.
- I found no recent (2023–2026) paper specifically on adaptive polling of RSS feeds, as opposed to web-page recrawl.

## 2. How mature feed readers and aggregators handle reliability (health, backoff, duplicates, moved feeds, malformed XML, user-agent/rate limits)

### Takeaway
The common pattern across Miniflux, FreshRSS, CommaFeed, NewsBlur, reader and Huginn is a set of per-feed counters: consecutive errors, last error message, "problem since" timestamp, last-new-item timestamp. Retries are linear or exponential after a few failures, `Retry-After` takes priority and FreshRSS scopes it to the whole domain, and permanently dead feeds (410) are muted. Deduplication uses a per-feed hash with configurable uniqueness criteria and fallbacks. Huginn's `expected_update_period_in_days` is the cleanest prior art for infohub's "silent source" flag.

### Cited Findings

**Maturity snapshot** (GitHub API, 2026-10-07; stars · license · last push · latest release):
- Miniflux — 9,770 · Apache-2.0 · 2026-10-03 · 2.3.3 (2026-07-24) — [miniflux/v2](https://github.com/miniflux/v2)
- FreshRSS — 16,246 · AGPL-3.0 · 2026-10-06 · 1.30.1 (2026-10-05) — [FreshRSS/FreshRSS](https://github.com/FreshRSS/FreshRSS)
- NewsBlur — 7,642 · MIT · 2026-10-06 · (no GitHub releases) — [samuelclay/NewsBlur](https://github.com/samuelclay/NewsBlur)
- Feedbin — 3,780 · MIT · 2026-10-06. Its separate "crawler" service was merged into the main app. — [feedbin/feedbin](https://github.com/feedbin/feedbin); [feedbin/crawler](https://github.com/feedbin/crawler)
- CommaFeed — 3,629 · Apache-2.0 · 2026-10-04 · 7.3.2 (2026-08-22) — [Athou/commafeed](https://github.com/Athou/commafeed)
- Tiny Tiny RSS — 855 · GPL-3.0 · 2026-10-02. The original author shut down tt-rss.org infrastructure on 1 Nov 2025, and development continues as the community fork tt-rss/tt-rss. — [tt-rss/tt-rss](https://github.com/tt-rss/tt-rss); [linuxiac](https://linuxiac.com/tt-rss-shuts-down-but-the-project-lives-on-under-a-new-fork/)
- RSSHub — 46,428 · AGPL-3.0 · 2026-10-06 · (rolling, no GitHub releases) — [DIYgod/RSSHub](https://github.com/DIYgod/RSSHub)
- newsnow — 21,967 · MIT · 2026-09-16 · v0.0.42 (2026-09-16). The repo moved from ourongxing/newsnow to the **newsnext/newsnow** org. — [newsnext/newsnow](https://github.com/newsnext/newsnow)
- Huginn — 50,026 · MIT · 2026-10-04 · v2026.10.04 — [huginn/huginn](https://github.com/huginn/huginn)
- reader (Python library) — 559 · BSD-3-Clause · 2026-10-06 · 3.26 (2026-06-24) — [lemon24/reader](https://github.com/lemon24/reader)
- feedparser — 2,436 · BSD-style (GitHub "NOASSERTION") · 2026-10-06 · v6.0.14 (2026-07-30) — [kurtmckee/feedparser](https://github.com/kurtmckee/feedparser)
- Others checked: yarr 4,065 MIT; Stringer 4,131 MIT; RSS-Bridge 9,266 Unlicense — [nkanaev/yarr](https://github.com/nkanaev/yarr); [stringer-rss/stringer](https://github.com/stringer-rss/stringer); [RSS-Bridge/rss-bridge](https://github.com/RSS-Bridge/rss-bridge)

**Health tracking:**
- Miniflux stores `ParsingErrorCount` and `ParsingErrorMsg` per feed, with `ResetErrorCounter()` on success. `POLLING_PARSING_ERROR_LIMIT` (default 3) is "the maximum number of parsing errors that the program will try before stopping polling a feed". — [feed.go](https://github.com/miniflux/v2/blob/main/internal/model/feed.go); [config docs](https://miniflux.app/docs/configuration.html)
- NewsBlur keeps `errors_since_good` (incremented per failed fetch, reset on success) and `has_feed_exception`, set from `count_errors_in_history()` over fetch history. — [models.py](https://github.com/samuelclay/NewsBlur/blob/master/apps/rss_feeds/models.py)
- FreshRSS 1.28.0 shows "time since when a feed has problems" (#8670). 1.27.0 added a button to delete errored feeds (#7030). 1.30.1 fixes "muting of feeds gone with HTTP 410" (#9391). — [CHANGELOG](https://github.com/FreshRSS/FreshRSS/blob/edge/CHANGELOG.md)
- reader stores the cause of the last failure in `feed.last_exception`. `update_feeds()` logs and continues; `update_feed()` raises `ParseError`. — [reader guide](https://reader.readthedocs.io/en/latest/guide.html)
- **Huginn RssAgent:** `expected_update_period_in_days` is "How often you expect this RSS feed to change. If more than this amount of time passes without an update, the Agent will mark itself as not working." `working?` = `event_created_within?(period || 10)` and no recent error logs (an error logged within 2 min of the last event counts as recent). — [rss_agent.rb](https://github.com/huginn/huginn/blob/master/app/models/agents/rss_agent.rb); [working_helpers.rb](https://github.com/huginn/huginn/blob/master/app/concerns/working_helpers.rb)

**Backoff:**
- CommaFeed retries at the normal interval for the first `retriesBeforeBackoff` = 3 errors. After that it waits `backoffInterval` (default 1 h) × (errorCount − 3 + 1), clamped to `maxInterval`, so the backoff is linear. On 429 it uses `max(Retry-After, error backoff)`. — [FeedRefreshIntervalCalculator.java](https://github.com/Athou/commafeed/blob/master/commafeed-server/src/main/java/com/commafeed/backend/feed/FeedRefreshIntervalCalculator.java); [CommaFeedConfiguration.java](https://github.com/Athou/commafeed/blob/master/commafeed-server/src/main/java/com/commafeed/CommaFeedConfiguration.java)
- FreshRSS applies `Retry-After` **domain-wide** (host + port), with separate rules when going through a proxy, and excludes local networks. — [CHANGELOG](https://github.com/FreshRSS/FreshRSS/blob/edge/CHANGELOG.md)
- newsnow falls back to cached items, returned with `status: "cache"`, when a fresh fetch fails. Users see stale data, not an error. — [server/api/s/index.ts](https://github.com/newsnext/newsnow/blob/main/server/api/s/index.ts)
- NewsBlur sends "forbidden" (blocked) feeds through a paid proxy (ScrapingBee) with long minimum intervals: 3/6/12/24 h by audience size. It also hard-codes per-domain floors (Craigslist ≥6 h, Twitter ≥2 h) and a YouTube floor to protect a shared API quota. — [models.py](https://github.com/samuelclay/NewsBlur/blob/master/apps/rss_feeds/models.py)

**Duplicates:**
- Miniflux dedups on `(feed_id, hash)` (unique index `entries_feed_id_hash_key`). It also keeps **entry tombstones**, so an entry the user deleted is not re-inserted on the next poll. — [storage/entry.go](https://github.com/miniflux/v2/blob/main/internal/storage/entry.go)
- FreshRSS has per-feed `unicityCriteria`: `id` (default), `link`, `sha1:link_published`, `sha1:link_published_title`, `sha1:title`, and others. By default it falls back to a more precise criterion when a feed produces many duplicates, unless `unicityCriteriaForced` is set. Related changes: "New unicity policies and heuristic for feeds with bad article IDs" (1.25.0), "Fallback to GUID if article link is empty", and an option to auto-mark-read new articles with an identical GUID (1.29.0). — [FreshRSS OPML doc](https://github.com/FreshRSS/FreshRSS/blob/edge/docs/en/developers/OPML.md); [CHANGELOG](https://github.com/FreshRSS/FreshRSS/blob/edge/CHANGELOG.md)
- Huginn's RssAgent remembers the last `remembered_id_count` (default 500) IDs to avoid re-emitting. — [rss_agent.rb](https://github.com/huginn/huginn/blob/master/app/models/agents/rss_agent.rb)
- reader has an `entry_dedupe` plugin for feeds whose entry IDs change. — [reader guide](https://reader.readthedocs.io/en/latest/guide.html)

**Moved feeds, redirects and malformed XML:**
- feedparser exposes `d.status` and `d.href`. For 302 you keep the original URL; for 301 the docs say a regularly polled client must update its stored URL. — [feedparser docs: http-redirect](https://github.com/kurtmckee/feedparser/blob/develop/docs/http-redirect.rst)
- Miniflux parses against `EffectiveURL()` (after redirects) and raises `error.duplicated_feed` if another subscription already has that effective URL. I saw no automatic rewrite of the stored URL on 301. — [handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/handler/handler.go)
- reader offers `change_feed_url()` to move a feed in place while keeping its history. — [reader guide](https://reader.readthedocs.io/en/latest/guide.html)
- feedparser still parses non-well-formed XML but sets `d.bozo = 1` and `d.bozo_exception` (with line number). — [feedparser docs: bozo](https://github.com/kurtmckee/feedparser/blob/develop/docs/bozo.rst)
- Miniflux distinguishes `error.unable_to_parse_feed` from `error.feed_format_not_detected` (for example, an HTML challenge page). — [handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/handler/handler.go)

**User-agent, timeouts and proxies:**
- Miniflux supports per-feed user-agent, cookie, proxy, "disable HTTP/2" and "ignore TLS errors", plus a global `HTTP_CLIENT_USER_AGENT` (empty means a default that includes the version). — [handler.go](https://github.com/miniflux/v2/blob/main/internal/reader/handler/handler.go); [config docs](https://miniflux.app/docs/configuration.html)
- RSSHub defaults to a **random Chrome-on-macOS user-agent** (`NO_RANDOM_UA` to disable), `REQUEST_RETRY` = 2 and `REQUEST_TIMEOUT` = 3000 ms. — [RSSHub config](https://docs.rsshub.app/deploy/config)

### Inferences
Borrow list for infohub, in priority order:
1. **Per-source state columns.** Store `consecutive_errors`, `last_error`, `problem_since` (FreshRSS), `last_new_item_at`, `expected_max_gap` (Huginn), `etag`, `last_modified`, `body_hash`, `retry_after_until`. Show "problem since" on the health page.
2. **Retry-After scoped per host.** FreshRSS does this; it matters because 23 Google News feeds share one host. One 429 should pause all of them, not only the one feed.
3. **Do not auto-disable sources after N errors the way Miniflux's default does.** For an unattended system, cap the backoff at 6–24 h and keep probing. Mute only on 410 or on explicit owner action.
4. **Persist 301 targets** (feedparser guidance), but only after the same 301 has been seen several times, to avoid hijack or misconfiguration flapping.
5. **Configurable per-source uniqueness keys (FreshRSS).** Google News items need a key that survives the `news.google.com/rss/articles/...` wrapper (title + source + published), while CLS/Sina flash items should use their numeric IDs.
6. **Tombstones (Miniflux)** stop deleted or blocked items from being resurrected by catch-up paging.
- A random browser UA (RSSHub default) is not polite practice. For a private single-owner system, a stable identifying UA with a contact URL fits better with publishers that whitelist. Spoofing may still be needed for some Chinese APIs (see section 5).

### Gaps
- I did not read Tiny Tiny RSS's or Feedbin's error-backoff code, so their exact algorithms aren't covered.
- I did not confirm whether FreshRSS automatically rewrites feed URLs on 301/308. The changelog search found only WebSub-related redirect entries.

## 3. Article full-text extraction (trafilatura, newspaper4k, news-please, Fundus, readability, Chinese options)

### Takeaway
For English, trafilatura is the strongest general extractor in independent and self-run benchmarks. A 2025 multilingual SIGIR study found it degrades badly on **Chinese**: F1 0.555, versus 0.672 for Readability, and Chinese was hard for every tool. On simple article pages the top tools converge around F1 ≈ 0.93 (WCXB 2026). Full text is worth fetching only when downstream steps (LLM relevance or summary) need body text that the RSS summary lacks. For the few high-value Chinese sources, per-site CSS selectors (FreshRSS's `cssFullContent` idea, or Fundus-style per-publisher parsers) beat generic extractors.

### Cited Findings
- **trafilatura** (Apache-2.0, 6,924 stars, v2.3.1 2026-10-06), maintainer's own evaluation (2026-10-02; 990 documents, 2,951 text / 2,966 boilerplate segments):

  | Extractor | F1 | Precision | Recall |
  |---|---|---|---|
  | trafilatura 2.3.0 standard | 0.926 | 0.906 | 0.946 |
  | trafilatura precision mode | 0.922 | — | — |
  | magic-html 0.1.8 | 0.889 | — | — |
  | jusText custom | 0.862 | — | — |
  | readability-lxml 0.9 | 0.853 | — | — |
  | goose3 | 0.810 | 0.936 | — |
  | newspaper4k 0.9.6 | 0.803 | — | — |

  - Speed: trafilatura runs at about 2–3× the baseline time; news-please is "by far the slowest" at about 20.9×. This is a self-reported benchmark.
  - Source: [trafilatura evaluation](https://trafilatura.readthedocs.io/en/latest/evaluation.html)
- **Bevendorff, Gupta, Kiesel, Stein**, "An Empirical Comparison of Web Content Extraction Algorithms" (SIGIR 2023): the trafilatura docs cite it as ranking trafilatura the best single tool by ROUGE-LSum mean F1. A search snippet gives a mean of 0.883, which I did not verify in the paper. — [paper PDF](https://downloads.webis.de/publications/papers/bevendorff_2023c.pdf); [ACM DL](https://dl.acm.org/doi/10.1145/3539618.3591920); [trafilatura evaluation](https://trafilatura.readthedocs.io/en/latest/evaluation.html)
- **"Multilingual Benchmarking of Main Content Extractors"** (SIGIR 2025 short paper, anonymous version; adds the DAnIEL news corpus with **400 Chinese pages**):
  - Macro F1 across 13 datasets: Readability 0.838, Boilerpipe 0.805, Trafilatura 0.770.
  - **Chinese:** Readability F1 0.672, Boilerpipe 0.611, Trafilatura 0.555 (precision only 0.501).
  - English: Trafilatura is best at 0.883.
  - **67% of Chinese pages** scored F1 ≤ 0.1 with at least one of the three extractors, versus 8% for English.
  - The authors attribute this to features like character, comma and link density not being language-agnostic.
  - Source: [paper PDF](https://maurelf.users.greyc.fr/docs/conferences/SIGIR_2025_paper_1968.pdf)
- **WCXB** (Foley, arXiv May 2026): 2,008 pages from 1,613 domains, 7 page types, 13 extractors (11 heuristic, 2 neural). "Top systems converge on articles (F1 = 0.93)" but range 0.41–0.84 on structured page types. Released as CC-BY-4.0. — [arXiv 2605.21097](https://arxiv.org/abs/2605.21097)
- **Fundus** (Dallabetta, Dobberstein, Breiding, Akbik; ACL 2024 demo; MIT; 479 stars; v0.5.7 2026-08-03):
  - Uses hand-written per-publisher parsers and claims "significantly higher quality extractions" than generic tools.
  - Crawls via publisher RSS/sitemaps or CC-NEWS.
  - Covers a predefined publisher collection organised by country (for example `PublisherCollection.us`).
  - Source: [arXiv 2403.15279](https://arxiv.org/abs/2403.15279); [GitHub README](https://github.com/flairNLP/fundus)
- **newspaper4k** (MIT, 1,149 stars, 0.9.6 2026-07-19):
  - A maintained fork of newspaper3k: "80+ languages" including Chinese via `jieba`, multithreaded `download_articles()`, and a Google News source via the `gnews` extra.
  - It warns that a wrong detected language can return no text.
  - Source: [newspaper4k README](https://github.com/AndyTheFactory/newspaper4k)
- **news-please** (Apache-2.0, 2,492 stars): an integrated crawler and extractor. The last push was 2026-04-14 and it has no GitHub releases. — [fhamborg/news-please](https://github.com/fhamborg/news-please)
- **Readability:**
  - mozilla/readability (JS, Apache-2.0, 11,481 stars) — [mozilla/readability](https://github.com/mozilla/readability)
  - The Python port readability-lxml (Apache-2.0, 2,898 stars, pushed 2026-08) — [buriy/python-readability](https://github.com/buriy/python-readability)
  - go-readability (MIT, 939) — [go-shiori/go-readability](https://github.com/go-shiori/go-readability)
  - Postlight Parser (Apache-2.0, 5,789 stars) last pushed 2024-07 — [postlight/parser](https://github.com/postlight/parser)
- **GeneralNewsExtractor (GNE)** (Python, **GPL-3.0**, 3,801 stars, last push 2026-04): a Chinese news-body extractor based on the paper "Web Content Extraction Based on Text and Symbol Density". It also extracts title, author and publish time. The README claims near-100% accuracy on major Chinese portals (Toutiao, NetEase, Sina, iFeng, Tencent) — a self-claim without an independent benchmark. — [kingname/GeneralNewsExtractor](https://github.com/kingname/GeneralNewsExtractor); [PyPI](https://pypi.org/project/GeneralNewsExtractor/)
- **magic-html** (opendatalab, Apache-2.0, 544 stars) scored second in trafilatura's benchmark (F1 0.889). — [opendatalab/magic-html](https://github.com/opendatalab/magic-html); [trafilatura evaluation](https://trafilatura.readthedocs.io/en/latest/evaluation.html)
- Adjacent libraries from the trafilatura author (both Apache-2.0):
  - **htmldate** — "Fast and robust date extraction from web pages" — [adbar/htmldate](https://github.com/adbar/htmldate)
  - **courlan** — "Clean, filter and sample URLs… Deduplication…" — [adbar/courlan](https://github.com/adbar/courlan)
- FreshRSS supports a per-feed `cssFullContent` selector to fetch and extract a specific HTML section of each article. — [FreshRSS OPML doc](https://github.com/FreshRSS/FreshRSS/blob/edge/docs/en/developers/OPML.md)

### Inferences
- **English sources** (TechCrunch, DeepMind, Fed): trafilatura is the default choice. It is Apache-2.0 and the fastest of the accurate tools. For Fed/DeepMind, the RSS body is probably enough.
- **Chinese sources** (IT之家, 量子位, flash items): do not rely on trafilatura alone. Options:
  - (a) Hand-written CSS/XPath selectors for the 3–5 Chinese sites that matter most, stored as per-source config (the FreshRSS `cssFullContent` model).
  - (b) readability-lxml as the generic fallback.
  - (c) Reimplement GNE's text/symbol-density idea from the paper rather than copying GPL code.
  - Flash items (Sina/CLS/WallstreetCN) are already complete short texts and need no extraction.
- Add an **extraction-quality guard**: store body length and the extracted/summary ratio per source, and alert when a source's median body length collapses. That is a template change, and it feeds the content-drift monitoring in section 4.
- Google News items wrap the publisher URL in an encoded `news.google.com/rss/articles/...` link, so full-text extraction or cross-source dedup first needs URL decoding. Community decoders exist and break when Google changes the format. — [huksley gist](https://gist.github.com/huksley/bc3cb046157a99cd9d1517b32f91a99e)
- courlan could replace or augment infohub's hand-rolled URL normalization: tracking-parameter stripping and dedup filters, Apache-2.0.

### Gaps
- No benchmark found specifically for newspaper4k or Fundus on Chinese news. Fundus's supported-publisher list was not checked for Chinese outlets.
- WCXB's per-tool and per-language numbers (and whether its neural extractors beat trafilatura) were not in the abstract.
- I found no independent evaluation of GNE.

## 4. Detecting broken or frozen feeds and content drift; monitoring patterns (dead-man switches, freshness SLOs)

### Takeaway
Production scraping monitors check outputs, not just HTTP success: item counts against a minimum or previous runs, field coverage, schema validation, unwanted HTTP codes, finish reasons (Zyte's Spidermon), and "expected update period" staleness (Huginn). Separately, an **external** dead-man switch catches the case where the whole worker or host is dead, which an in-app health page cannot report. RSSHub keeps its huge connector catalogue healthy mainly by running each route's examples against the live site, weekly and on every PR.

### Cited Findings
- **Spidermon** (Zyte; BSD-3-Clause; 562 stars; 1.24.0 2025-04-11) built-in monitors:
  - `ItemCountMonitor`, `ErrorCountMonitor`, `WarningCountMonitor`, `CriticalCountMonitor`, `FinishReasonMonitor` (`SPIDERMON_EXPECTED_FINISH_REASONS`).
  - `UnwantedHTTPCodesMonitor`: default codes `[400, 407, 429, 500, 502, 503, 504, 523, 540, 541]`, max count 10.
  - `DownloaderExceptionMonitor`, `RetryCountMonitor` (`SPIDERMON_MAX_RETRIES`), `SuccessfulRequestsMonitor` (`SPIDERMON_MIN_SUCCESSFUL_REQUESTS`), `TotalRequestsMonitor`.
  - `ItemValidationMonitor` (jsonschema) and `FieldCoverageMonitor` (`SPIDERMON_FIELD_COVERAGE_RULES`, with `SPIDERMON_ADD_FIELD_COVERAGE`).
  - `PeriodicExecutionTimeMonitor` and `ZyteJobsComparisonMonitor` (`SPIDERMON_JOBS_COMPARISON*`: compares item counts against previous jobs with a threshold), plus `SPIDERMON_ITEM_COUNT_INCREASE`.
  - Notification actions: email, Slack, Telegram, Discord, Sentry and others.
  - Source: [monitors.py](https://github.com/scrapinghub/spidermon/blob/master/spidermon/contrib/scrapy/monitors/monitors.py); [settings docs](https://spidermon.readthedocs.io/en/latest/settings.html); [docs home](https://spidermon.readthedocs.io/en/latest/)
- **Huginn** marks an agent "not working" if no event was created within `expected_update_period_in_days`, or if errors were logged after its last event. — [working_helpers.rb](https://github.com/huginn/huginn/blob/master/app/concerns/working_helpers.rb); [rss_agent.rb](https://github.com/huginn/huginn/blob/master/app/models/agents/rss_agent.rb)
- **RSSHub connector QA:**
  - A scheduled workflow (`cron: '0 0 * * 0'`, weekly) runs `pnpm vitest:fullroutes` against all routes with `continue-on-error`, and publishes the results to GitHub Pages assets. — [test-full-routes.yml](https://github.com/DIYgod/RSSHub/blob/master/.github/workflows/test-full-routes.yml)
  - Contributor rules (AGENTS.md): "Do not write test files for routes… Route tests only encode the author's assumptions about the target site and go stale when it changes. Routes are verified through automatic route testing on pull requests, which runs the route examples against the live site." — [AGENTS.md](https://github.com/DIYgod/RSSHub/blob/master/AGENTS.md)
  - Runtime monitoring: `SENTRY` / `HONEYBADGER_API_KEY` error tracking, and `ERROR_TRACKING_ROUTE_TIMEOUT` (default 30,000 ms) reports slow routes. — [RSSHub config](https://docs.rsshub.app/deploy/config)
- **Dead-man switch pattern:** watch for the *absence of success* rather than for errors. A job pings after each successful run, and an alert fires if the ping does not arrive by its deadline. — [jimy-r/dead-mans-switch](https://github.com/jimy-r/dead-mans-switch); [OneUptime heartbeat guide (2026)](https://oneuptime.com/blog/post/2026-02-06-heartbeat-dead-man-switch-opentelemetry-pipeline/view)
- Self-hostable tools:
  - **Healthchecks** — "Open-source cron job and background task monitoring service" (Python/Django, BSD-3-Clause, 10,386 stars, v4.4 2026-08-31) — [healthchecks/healthchecks](https://github.com/healthchecks/healthchecks)
  - **Uptime Kuma** (MIT, 92,168 stars) — [louislam/uptime-kuma](https://github.com/louislam/uptime-kuma)
  - **changedetection.io** — website change detection (Apache-2.0, 34,818 stars, 0.60.8 2026-09-28) — [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io)
- **Freshness budget/SLO framing:** define per destination how old data may be, use a heartbeat that proves liveness even when there is no new data, and alert on silence or volume drops. These are practitioner blogs (medium authority). — [pipecode.ai](https://pipecode.ai/blogs/data-freshness-sla-monitoring-budgets); [Metabase data freshness](https://www.metabase.com/metrics/data-freshness)
- FreshRSS shows how long a feed has had problems (1.28.0). — [CHANGELOG](https://github.com/FreshRSS/FreshRSS/blob/edge/CHANGELOG.md)

### Inferences
- **Silent-source detection with a Poisson baseline.** For each source, estimate the hourly new-item rate λ_h by hour-of-week; Chinese flash feeds are near-zero overnight in Beijing time, and the Fed is bursty. Flag "silent" when the probability of seeing zero new items in the elapsed window, `exp(−Σλ_h)`, drops below about 1%. A fixed Huginn-style `expected_max_gap` per source is the simpler first step.
  - This handles "fetches OK but nothing new" without false alarms at night or on weekends.
- **Content-drift checks per fetch, Spidermon-style.** For each source, check:
  - (a) item count is not 0 and not the full page of all-duplicate items for many runs;
  - (b) field coverage: % of items with title, URL, parseable `published`, non-empty body;
  - (c) JSON-schema validation of the CLS, WallstreetCN and Sina API responses (key renames are the typical breakage of "borrowed" APIs);
  - (d) the HTTP-200-but-HTML case (anti-bot or login page; Miniflux's `feed_format_not_detected`).
- **Cross-source canary.** If Google News shows coverage for a company and the direct sources are quiet, nothing is wrong. But if all three flash feeds go silent together during market hours, suspect the worker or network rather than the sources.
- **Out-of-band dead-man switch.** The worker should ping a Healthchecks instance after each scheduler cycle. Healthchecks can be self-hosted, but then it shares the same Windows host, so a free hosted plan or a second machine is better. This covers host down, Docker down, or the worker deadlocked — none of which the in-app health page can report.
- **Weekly connector self-test (RSSHub idea).** Run each connector against the live site with assertions about the shape of the output (not exact content), and store a per-connector "last verified" date.

### Gaps
- I did not verify Uptime Kuma's "push" monitor details or Healthchecks' grace-period semantics against their docs. Both are well known but not fetched here.
- No academic paper found on statistically grounded "silent feed" detection. The Poisson approach is my inference, built on the Poisson change models in section 1.

## 5. Anti-blocking good practice that stays polite and legal; how RSSHub/newsnow keep Chinese connectors working

### Takeaway
"Polite" in mature readers means:
- conditional requests;
- honouring `Retry-After` per host;
- limiting concurrency per host;
- longer intervals for blocked or low-value sources instead of more retries;
- caching anything you fetch in a loop.

RSSHub keeps Chinese connectors alive through crowdsourcing: a huge contributor base, one directory per site, and live route tests on every PR and weekly. Proxies and browser automation are reserved for the few sites that need them. newsnow re-implements the same Chinese APIs (CLS signing, WallstreetCN live) under **MIT**, so it is the legally safer reference for infohub than AGPL RSSHub.

### Cited Findings
- **Robots:** the Robots Exclusion Protocol is standardised as [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309) (not fetched in this session; standard reference).
- **Per-host concurrency:** Miniflux `POLLING_LIMIT_PER_HOST` "Limits the number of concurrent requests to the same hostname when polling feeds" (default 0 = disabled). — [Miniflux config](https://miniflux.app/docs/configuration.html)
- **Retry-After scope:** FreshRSS scopes `Retry-After` domain-wide by host and port, keeps separate proxy rules, and excludes local networks. — [FreshRSS CHANGELOG](https://github.com/FreshRSS/FreshRSS/blob/edge/CHANGELOG.md)
- **Blocked sources get longer intervals, not more retries:** NewsBlur routes "forbidden" feeds through a paid proxy but with minimum intervals of 3–24 h. — [NewsBlur models.py](https://github.com/samuelclay/NewsBlur/blob/master/apps/rss_feeds/models.py)
- **RSSHub runtime:**
  - Route-level cache `CACHE_EXPIRE` = 300 s and content cache `CACHE_CONTENT_EXPIRE` = 3600 s (memory or Redis).
  - `REQUEST_RETRY` = 2; `PROXY_URI` with `PROXY_URL_REGEX` to proxy only some hosts; random UA by default.
  - Source: [RSSHub config](https://docs.rsshub.app/deploy/config)
- **RSSHub contributor rules** — [AGENTS.md](https://github.com/DIYgod/RSSHub/blob/master/AGENTS.md):
  - "Always cache the returned results when fetching article details in a loop using `cache.tryGet()`".
  - Set `requirePuppeteer: true` only when a route actually uses browser automation.
  - Route correctness is checked by live tests on PRs.
- RSSHub's docs say a small portion of sites use strict anti-crawling or encryption, where Puppeteer may be needed, and that "Partial routes have a strict anti-crawler policy, and can be configured to use proxy". — [RSSHub route docs](https://docs.rsshub.app/joinus/new-rss/start-code); [RSSHub FAQ mirror](https://rsshub-doc.pages.dev/en/faq)
- **RSSHub CLS (财联社) connector:** lives in `lib/routes/cls/` (`depth.ts`, `hot.ts`, `subject.ts`, `telegraph.tsx`, `utils.ts`, `namespace.ts` with `lang: 'zh-CN'`). The repo is **AGPL-3.0**. — [RSSHub lib/routes/cls](https://github.com/DIYgod/RSSHub/tree/master/lib/routes/cls)
- **newsnow CLS connector (MIT):**
  - `server/sources/cls/utils.ts` builds params with `appName: "CailianpressWeb"` and appends `sign = md5(sha1(querystring))`.
  - The telegraph list comes from `https://www.cls.cn/v1/roll/get_roll_list`. The connector also covers depth and hot lists.
  - Source: [newsnow cls/utils.ts](https://github.com/newsnext/newsnow/blob/main/server/sources/cls/utils.ts); [newsnow cls/index.ts](https://github.com/newsnext/newsnow/blob/main/server/sources/cls/index.ts)
- **newsnow WallstreetCN connector (MIT):** live feed from `https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=30`; the source is configured with `interval: Time.Fast` (5 min). — [newsnow wallstreetcn.ts](https://github.com/newsnext/newsnow/blob/main/server/sources/wallstreetcn.ts); [pre-sources.ts](https://github.com/newsnext/newsnow/blob/main/shared/pre-sources.ts)
- **newsnow's catalogue** covers about 47 Chinese and global sources (for example 36kr, baidu, cls, fastbull, gelonghui, ithome, jin10, thepaper, wallstreetcn, xueqiu, zaobao, zhihu), each a single file under `server/sources/`. — [newsnow server/sources](https://github.com/newsnext/newsnow/tree/main/server/sources)
- **Google News RSS:** reports of quick 429s even through rotating proxies (developer anecdotes, low authority). Google News RSS has no official rate-limit documentation. — [cloro.dev](https://cloro.dev/blog/google-news-rss/); [NewsCatcher "missing docs"](https://www.newscatcherapi.com/blog-posts/google-news-rss-search-parameters-the-missing-documentaiton)
- **Python building blocks:**
  - hishel — "Elegant HTTP Caching for Python", for httpx (BSD-3-Clause, 413 stars, 1.4.0 2026-09-16) — [karpetrosyan/hishel](https://github.com/karpetrosyan/hishel)
  - requests-cache (BSD-2-Clause, 1,506 stars) — [requests-cache](https://github.com/requests-cache/requests-cache)
  - CacheControl (505 stars) — [psf/cachecontrol](https://github.com/psf/cachecontrol)
  - stamina — "Production-grade retries for Python" (MIT, 1,459 stars, 26.1.0) — [hynek/stamina](https://github.com/hynek/stamina)
  - tenacity (Apache-2.0, 8,799 stars) — [jd/tenacity](https://github.com/jd/tenacity)
  - PyrateLimiter — leaky-bucket rate limiter (MIT, 525 stars) — [vutran1710/PyrateLimiter](https://github.com/vutran1710/PyrateLimiter)

### Inferences
- **License hygiene.** infohub's CLS signed-API client was "borrowed from RSSHub" (AGPL-3.0). newsnow's MIT-licensed `cls/utils.ts` implements the same `md5(sha1(query))` signing. Re-base infohub's implementation on newsnow, or on a clean-room description, and add an MIT attribution notice. That removes any AGPL-derivation question even for a private project that might later be shared. This is a judgement call, not legal advice.
- **Chinese connector maintenance for a single-owner system.** Copy RSSHub's process rather than its code:
  - (a) one module per source with a declared output schema;
  - (b) a weekly live self-test (cron in the worker) that asserts the schema and a non-zero item count;
  - (c) watch upstream changes — subscribe to newsnow and RSSHub commits under `server/sources/cls*`, `wallstreetcn*` and `lib/routes/cls/`. When those break, infohub's borrowed APIs probably broke too, and the upstream fix shows the new parameters.
- **Politeness defaults for infohub:**
  - per-host concurrency = 1;
  - a per-host token bucket (PyrateLimiter) for news.google.com;
  - shared per-host `Retry-After`;
  - jittered schedules;
  - a stable UA string with contact info for Western RSS publishers;
  - browser-like headers only where an API requires them.
  - Do not add proxy rotation: for ~4,000 items/day it adds ToS risk and complexity for little gain. NewsBlur's own approach to blocked feeds is to poll less.
- **Yahoo-style 429 lesson.** Treat 429 as a capacity signal. Persist `retry_after_until` per host, lower that host's adaptive rate (multiplicative decrease), and recover slowly (additive increase). This is AIMD-style and my inference; none of the cited readers implement full AIMD.
- hishel (httpx) or requests-cache could replace hand-rolled ETag storage, but infohub already persists per-source state in SQLite. Storing the two validator strings per source is simpler and more transparent than adding a cache layer.

### Gaps
- I did not find documentation on how often RSSHub's CLS or WallstreetCN routes break, or their mean time to repair. The issue history was not mined.
- I did not determine whether the CLS/WallstreetCN/Sina endpoints' terms of service allow automated polling. That needs a manual check.
- I found no authoritative Google statement on Google News RSS rate limits. The 429 reports are anecdotal.
