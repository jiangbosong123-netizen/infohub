import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crawler import googlenews

# Titles from the legacy Google News items, one or more per pattern.
QUOTE_PAGES = [
    "Li Auto Inc. (LI) stock price, news, quote and history",
    "Tencent Holdings Limited (0700.HK) Stock Price, News, Quote & History",
    "Broadcom Inc (AVGO) Stock Price, Quote, News & History",
    "2330: TSMC - Stock Price, Quote and News | TWSE",
    "Oracle (ORCL) Stock Price, Quote & Analysis",
    "TSM Stock Quote Price and Forecast",
    "Baidu, Inc. (9888.HK) Interactive Stock Chart",
    "Baidu, Inc. (BIDU) Options Chain",
    "TSLA 260821 320.00C (TSLA260821C320000) Stock Community & Discussion",
    "AMD Sep 2026 637.500 put (AMD260925P00637500) stock historical prices and data",
    "Baidu Inc (BIDU) Stock Price Today: $90.14",
    "Li Auto rStock Price Today: Live RLI to USD Price Chart & Market Data",
    "ORCL: Oracle Corp. Latest Stock Price, Analysis, News and Trading Ideas",
    "Alibaba Stock Price: BABA Stock Chart, Market Cap & News Today",
    "TCENTX to IDR: Tencent xStock Price in Indonesian Rupiah",
    "AMAT,NVDA,META,CSCO,EXPE,LYFT,MRVL,CART | Stock Prices | Quote Comparison",
    "Broadcom Inc Share Price - AVGO, RNS News, Articles, Quotes, & Charts (NASDAQ:AVGO)",
    "Broadcom (AVGO) Yield Shares Purpose ETF (YAVG) Stock Price | Quotes & News",
    "ADBE vs AVGO: US Stock Price & Performance Comparison 2026 | MEXC",
]
ARTICLES = [
    "Goldman Sachs sets Amazon stock price for 12 months",
    "UBS raises Palantir stock price target on strong AI demand momentum",
    "Palantir stock price ended at $167.23 on Friday, after gaining 0.83%",
    "Prediction: This Will Be Palantir's Stock Price by the End of 2028",
    "Oracle (NYSE:ORCL) Stock Price Up 3.3% on Insider Buying Activity",
    "QQQ is up 1.7% today, on AMD stock price movement",
    "Why is Arm stock up 7.1% today?",
    "OpenAI, Microsoft executives' quotes on AI training threaten copyright defense, news outlets argue",
    "Tesla Stock Price Prediction: Can TSLA Stock See a New Rally as Next-Gen Roadster Nears Launch?",
]


class QuotePageTests(unittest.TestCase):
    def test_quote_chart_and_option_pages_are_recognised(self):
        for title in QUOTE_PAGES:
            with self.subTest(title=title):
                self.assertTrue(googlenews._is_quote_page(title))

    def test_articles_about_a_stock_price_are_kept(self):
        for title in ARTICLES:
            with self.subTest(title=title):
                self.assertFalse(googlenews._is_quote_page(title))

    def test_the_feed_skips_quote_pages(self):
        entries = "".join(
            f"<item><title>{title} - Yahoo Finance</title><link>https://news.google.com/rss/articles/{n}</link>"
            f"<pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate></item>"
            for n, title in enumerate(["Tencent Holdings Limited (TCEHY) Stock Price, News, Quote & History",
                                       "Tencent shares rise after earnings beat"]))
        feed = f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{entries}</channel></rss>'
        with patch.object(googlenews.http, "fetch", return_value=SimpleNamespace(content=feed.encode())), \
                patch.object(googlenews, "match_companies", return_value=["tencent"]):
            rows = googlenews.fetch_company_news("tencent", "Tencent", ["Tencent"])
        self.assertEqual([row["title"] for row in rows], ["Tencent shares rise after earnings beat"])



# (title, publisher) pairs from the legacy Google News items.
COMMUNITY_POSTS = [
    ("$Oracle (ORCL.US)$ Next one, come here.", "Moomoo"),
    ("$Palantir (PLTR.US)$", "Moomoo"),
    ("$Tesla (TSLA.US)$ come up 5%+", "moomoo.com"),
    ("$DBS (D05.SG)$ $DBS (D05.SG)$ $Oracle (ORCL.US)$ $UOB (U11.SG)$", "Moomoo"),
    ("day 18.. hopefully $Tesla (TSLA.US)$ is back soon", "Moomoo"),
    ("Day 26 Check-In: Microsoft ($MSFT) as the Enterprise Operating System", "Moomoo"),
    ("Day 11 - Login to Check In For Trading Record (AVGO) Lets Go", "moomoo.com"),
]
NOT_COMMUNITY_POSTS = [
    ("Market Chatter: Microsoft Plans to More Than Triple Data Center Capacity", "moomoo.com"),
    ("Wedbush Maintains Meta Platforms(META.US) With Hold Rating, Maintains Target Price $650", "Moomoo"),
    ("$10,000 in Tesla When It Joined the S&P 500 Would Be About $15,700 Today", "fool.com"),
    ("$PLTR stock fell 5% this week. Here's what we see in our data.", "Quiver Quantitative"),
    ("$200 ChatGPT Pro Subscriptions Paused After 'Unprecedented' Astra Demand", "PCMag"),
    ("Day 14: Oak Apple Galls and Antique Apples", "The Trek"),
]


class CommunityPostTests(unittest.TestCase):
    def test_moomoo_community_posts_are_recognised(self):
        for title, publisher in COMMUNITY_POSTS:
            with self.subTest(title=title):
                self.assertTrue(googlenews._is_community_post(title, publisher))

    def test_news_reposts_and_other_dollar_titles_are_kept(self):
        for title, publisher in NOT_COMMUNITY_POSTS:
            with self.subTest(title=title):
                self.assertFalse(googlenews._is_community_post(title, publisher))

    def test_the_feed_skips_community_posts(self):
        entries = "".join(
            f'<item><title>{title} - Moomoo</title><link>https://news.google.com/rss/articles/{n}</link>'
            f'<pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate><source url="https://www.moomoo.com">Moomoo</source></item>'
            for n, title in enumerate(["$Oracle (ORCL.US)$ Next one, come here.", "Day 6: ORCL",
                                       "Market Chatter: Oracle Wins Pentagon Cloud Deal"]))
        feed = f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{entries}</channel></rss>'
        with patch.object(googlenews.http, "fetch", return_value=SimpleNamespace(content=feed.encode())), \
                patch.object(googlenews, "match_companies", return_value=["oracle"]):
            rows = googlenews.fetch_company_news("oracle", "Oracle", ["Oracle"])
        self.assertEqual([row["title"] for row in rows], ["Market Chatter: Oracle Wins Pentagon Cloud Deal"])

if __name__ == "__main__":
    unittest.main()
