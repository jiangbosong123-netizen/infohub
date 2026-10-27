# 投研系统接入 Infohub 新闻（D31 M2 第一步，2026-10-18）

给 AI 半导体投研系统（`ai-research-daily`）的对接说明。按 D31，投研系统的代码由它自己的会话按它的规矩改，
Infohub 不往那个仓库写东西；这份说明就是交给那边的全部信息。看什么、影响什么的标准见
[平台设计](../spec/PLATFORM_DESIGN.md) 5.1。

## 接口

`GET https://windows-server.tail29d4dd.ts.net/api/v1/signals/news`，头 `Authorization: Bearer <密钥>`。只在 Tailscale 私网里能访问。

| 参数 | 说明 |
|---|---|
| `scope` | `company`（有 `cik` 时默认）、`industry`、`macro` |
| `cik` | 公司 CIK，可重复或逗号分隔，最多 100 个，前导零可省；只用于 `company` |
| `theme` | 主题键，可重复；只用于 `industry` 或 `macro`，不填就是该范围的全部主题 |
| `since` | 必填，ISO 8601 带时区；只返回可得时间晚于它的 |
| `as_of` | 可选，ISO 8601 带时区；给了就是严格的时点查询（见下），不给就到现在为止 |
| `min_score` | 0–100；默认公司 0、行业 50、宏观 70 |
| `limit` | 1–500，默认 200 |
| `after` | 上一页的 `next_after`，取下一页 |
| `include_routine` | `true` 时也返回例行公告（内部人申报等），默认不返回 |

时间窗口：公司最多 31 天，行业和宏观最多 7 天。错误参数返回 422；没开或数据库忙返回 503，按缺口记录、下次再取。

主题（`market-themes-v2`，规则见 `app/market_themes.py`；v2 起芯片走私算出口管制）：
- 行业：`export_controls` 出口管制与芯片限制、`ai_capex` AI 资本开支、`capacity_supply` 产能与供应链、`memory` 存储芯片、
  `equipment_materials` 半导体设备与材料、`datacenter_power` 数据中心与电力、`end_demand` 终端需求、`industry_data` 行业数据；
- 宏观：`rates` 货币政策与利率、`inflation_jobs` 通胀与就业、`markets` 大盘与指数、`bonds_fx` 国债与汇率、`trade_geopolitics` 贸易与地缘。

## 返回

```json
{"scope": "company", "since": "...", "as_of": "...", "point_in_time": false, "min_score": 0,
 "coverage": {"0000723125": {"covered_since": "2026-10-15T15:59:42+00:00", "basis": "first_feed_fetch"}},
 "collection_gaps": [{"from": "2026-10-03T16:02:18Z", "to": "2026-10-14T22:04:42Z", "reason": "..."}],
 "algorithm": "news-signals-v1", "themes_version": "market-themes-v2", "unknown_ciks": [],
 "next_after": null,
 "data": [{"id": 123, "available_at": "2026-10-18T09:12:03.1+00:00", "published_at": "...",
           "title": "...", "title_zh": "...", "url": "...", "publisher": "Reuters", "source": "Google News · 美光",
           "official": false, "independence": "report", "story_id": "...", "routine": false,
           "ciks": ["0000723125"], "headline_ciks": ["0000723125"], "themes": ["memory"], "event_type": "earnings", "channel": "stock",
           "importance": 80, "mood": -2, "judged_at": "2026-10-18T09:15:40+00:00"}]}
```

- `title_zh`：AI 译的中文标题。模型拒绝翻译，或译文有乱码（10-19 起检查，见运维手册“AI 乱码”）时为空，请用 `title`。
- `available_at`：Infohub 第一次看到这条的时间。**回测和“防未来函数”只能用它**；`published_at` 只作显示。
- `importance`：AI 重要性评分 0–100；`mood`：AI 判断的利空到利好，−2 到 2。两者都是没有人工复核的粗略判断。
  `refused` 为真表示模型的内容审查拒绝判断这一条，此时两项都为空（10-18 以前这类条目返回过 `importance: -1`）。
- `judge_prompt_version`：给出这两项的评分规则。`score-v1` 是这一版上线前的（上线时间另行通知）；`score-v2` 起，情绪按条目所挂公司（`ciks`）判断，只提问题不给结论的文章和没有指向这家公司的泛泛行业、法律、政策讨论给 0；重要性规则不变；`rule` 是例行公告的固定判断。已有的判断不会重判：上线前的条目（含带 `as_of` 补取的历史截面）永远是 `score-v1`。判断未知时为空。
- `judged_at`：AI 给出上面两项的时间（2026-10-18 以前的条目没有记录）。给了 `as_of` 时，只有 `judged_at ≤ as_of` 的判断
  才返回，否则两项为空——那一刻还不知道。
- `independence`：`official` 公司或监管方本人发布；`report` 同一件事的第一篇；`repost` 后来的转载。计数时同一个
  `story_id` 只算一次，与模块 4 的“只算原始来源和独立研究”对应。
- `headline_ciks`：`ciks` 里在标题本身被点名的那些（按公司别名、简称和代码识别）。在里面说明新闻是讲这家公司的；
  只在 `ciks` 里、不在这里，多半是正文里顺带提到（2026-10-18 应投研系统要求加）。
- `coverage`（只在 `company` 范围）：每个请求的 CIK 从什么时候开始采，`{"covered_since": UTC 时间, "basis": 依据}`。依据是
  `first_feed_fetch`（这家公司自己的新闻源第一次抓取，2026-10-18 以前的公司按此回填）或 `recorded`（加入时记下）。
  在 `covered_since` 之前没有新闻，说明的是“还没在采”，不是“没有新闻”。
- `judging_gaps`：模型一条都没判的时段 `[{"from", "to", "reason"}]`（10-27 起）。一轮评分里所有请求都失败（余额不足、密钥无效、连不上等）就开一段，下一次有回答就关上；不到 30 分钟的不列；还没恢复时 `to` 为空。这段时间进来的条目仍会进库，恢复后补判，`judged_at` 是补判时间。10-27 的那段（01:09:24Z–12:57:04Z，余额不足）按条目判断时间补记。
- `collection_gaps`：整个 Infohub 停采的时段 `[{"from", "to", "reason"}]`，任何公司在这些时段里都可能缺新闻。10-22 起自动记录：任何 30 分钟以上没有抓取（主机睡眠、断网、后台停止），恢复后的第一轮抓取就记一段，`reason` 以“自动记录”开头；之前 14 天抓取日志里能看到的也补记了。恢复后会往回补抓一部分，所以时段内不一定完全没有条目。
  2026-10-02 以前（Mac 时期）没有抓取日志，那时 Mac 偶尔睡眠造成的空档无法还原，不在列表里。
- `unknown_ciks`：Infohub 还没在采的公司。要加，在 Infohub 的 `config/coverage.yaml` 的 `ai-research` 申请里加 CIK。

## 按投研系统自己的规矩接

- 网络只走 `实现/网络.py`（`get_json`），地址和超时写在 `实现/配置.py`。
- 密钥不进仓库：放在 `~/.infohub/signals.token`（权限 600），或环境变量；按 `配置.av_key_file()` 的写法读。
- 断网不崩：取不到就记成数据缺口（GitHub Actions 的机器不在私网，一定取不到；本机 launchd 和看板能取到）。
- 时点：按 `available_at` 切片，和 8-K 的 `filingDate > as_of` 跳过同一个道理。
  按时点保存的只有 `available_at` 和 `judged_at`：公司标签（`ciks`）、是否隐藏、`title_zh` 都用查询那一刻的值。Infohub 修正标签后（如 2026-10-20 迁移 63 去掉 AMD/Apple/Meta 的误挂），带 `as_of` 补取的历史截面也是改后的标签，和当天存下的快照可能不同；修正的生效时间会通知你们记录。
- 第一阶段只进看板和风控台：每家公司“近期新闻”、重要利空和重大风险提醒。不进模块分、不改 `RULE_VERSION`；
  要进评分先按它的量化层规范预注册。

## 开通（Windows 上一行）

```powershell
cd ~\Server\infohub; git pull; .\signals.cmd
```

打开接口、签发 `ai-research` 的 `read:signals` 密钥（一年，新签发会让旧的失效）、用 Tailscale 发到 Mac 的“下载”文件夹。
Mac 上把它移到 `~/.infohub/signals.token` 并设为 600。
