from __future__ import annotations

"""源注册表：所有信息源在这里定义。想加源就在 SOURCES 里加一条。

type:  rss=RSS订阅  html=网页抓取(解析器在 html_source.py)  sec=SEC EDGAR  hkex=港交所披露易  googlenews=对账兜底
tier:  official=官方一手  media=财经媒体  info=科技资讯  reconcile=每日对账
"""
from ..config import BASE_DIR

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions"
HKEX_BASE = "https://www1.hkexnews.hk"

SOURCES: list[dict] = [
    # ---------- AI 资讯 ----------
    dict(key="qbitai", name="量子位", channel="ai", tier="info", type="rss",
         url="https://www.qbitai.com/feed", interval_minutes=30),
    dict(key="ithome", name="IT之家", channel="ai", tier="info", type="rss",
         url="https://www.ithome.com/rss/", interval_minutes=30),
    dict(key="hnrss", name="Hacker News 热门", channel="ai", tier="info", type="rss",
         url="https://hnrss.org/frontpage", interval_minutes=30),
    dict(key="techcrunch", name="TechCrunch", channel="ai", tier="info", type="rss",
         url="https://techcrunch.com/feed/", interval_minutes=30),
    dict(key="theverge", name="The Verge", channel="ai", tier="info", type="rss",
         url="https://www.theverge.com/rss/index.xml", interval_minutes=30),
    dict(key="huggingface", name="Hugging Face Blog", channel="ai", tier="info", type="rss",
         url="https://huggingface.co/blog/feed.xml", interval_minutes=60),
    dict(key="ifanr", name="爱范儿", channel="ai", tier="info", type="rss",
         url="https://www.ifanr.com/feed", interval_minutes=30),

    # ---------- 机器人 ----------
    dict(key="ieee-robotics", name="IEEE Spectrum 机器人", channel="robot", tier="info", type="rss",
         url="https://spectrum.ieee.org/feeds/topic/robotics.rss", interval_minutes=60),
    dict(key="robot-report", name="The Robot Report", channel="robot", tier="info", type="rss",
         url="https://www.therobotreport.com/feed/", interval_minutes=60),

    # ---------- 股市：官方一手 ----------
    dict(key="sec-edgar", name="SEC EDGAR 文件", channel="stock", tier="official", type="sec",
         url=SEC_SUBMISSIONS_URL, interval_minutes=10),
    dict(key="hkex", name="港交所披露易", channel="stock", tier="official", type="hkex",
         url=HKEX_BASE, interval_minutes=10),
    dict(key="openai-news", name="OpenAI 官网动态", channel="ai", tier="official", type="rss",
         url="https://openai.com/news/rss.xml", interval_minutes=60),

    # ---------- 股市：媒体 ----------
    dict(key="wallstreetcn", name="华尔街见闻", channel="stock", tier="media", type="rss",
         url="https://dedicated.wallstreetcn.com/rss.xml", interval_minutes=20),
    dict(key="cnbc-tech", name="CNBC 科技", channel="stock", tier="media", type="rss",
         url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
         interval_minutes=30),
    dict(key="sina-7x24", name="新浪 7x24 快讯", channel="stock", tier="media", type="sina",
         url="https://zhibo.sina.com.cn/api/zhibo/feed", interval_minutes=10),
    dict(key="cls-telegraph", name="财联社电报", channel="stock", tier="media", type="cls",
         url="https://www.cls.cn/telegraph", interval_minutes=10),
    dict(key="wallstreetcn-live", name="华尔街见闻快讯", channel="stock", tier="media", type="wscn_live",
         url="https://api-one-wscn.awtmt.com/apiv1/content/lives", interval_minutes=10),
    dict(key="techmeme", name="Techmeme", channel="ai", tier="media", type="rss",
         url="https://www.techmeme.com/feed.xml", interval_minutes=30),

    # ---------- 股市：每日对账兜底（不走常规轮询，单独任务）----------
    dict(key="google-news", name="Google News 对账", channel="stock", tier="reconcile", type="googlenews",
         url="https://news.google.com/rss/search", interval_minutes=1440),
]


def google_news_source_for(slug: str, name: str, name_zh: str) -> dict:
    """为每家关注公司生成 Google News 聚合源（抓取时按公司别名过滤，防相关性漂移）。

    20 分钟一轮兼顾新鲜度与对 Google 的礼貌性；另有 tier=reconcile 的每日对账任务兜底补漏。
    """
    label = name_zh or name
    return dict(key=f"googlenews-{slug}", name=f"Google News · {label}", channel="stock",
                tier="media", type="googlenews", company_slug=slug,
                url="https://news.google.com/rss/search", interval_minutes=20)


def all_sources() -> list[dict]:
    """常规源 + 依据 watchlist 动态生成的每公司 Google News 源。"""
    from ..database import get_db
    extra: list[dict] = []
    try:
        with get_db() as db:
            rows = db.execute("SELECT slug, name, name_zh FROM companies").fetchall()
        extra = [google_news_source_for(r["slug"], r["name"], r["name_zh"]) for r in rows]
    except Exception:
        pass  # init-db 之前 companies 表还没数据
    return SOURCES + extra
