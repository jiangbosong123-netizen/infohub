import unittest
from collections import Counter
from urllib.parse import urlsplit

from app.crawler.runner import FETCHERS
from app.crawler.sources import SOURCES
from app.provenance import PUBLISHERS, publisher


class SourceRegistryTests(unittest.TestCase):
    def test_sources_are_unique_and_well_formed(self):
        self.assertFalse([key for key, n in Counter(s["key"] for s in SOURCES).items() if n > 1])
        self.assertFalse([url for url, n in Counter(s["url"] for s in SOURCES).items() if n > 1])
        for source in SOURCES:
            with self.subTest(source=source["key"]):
                self.assertIn(source["channel"], {"ai", "robot", "stock"})
                self.assertIn(source.get("tier", "media"), {"official", "media", "info", "reconcile"})
                self.assertIn(source["type"], FETCHERS)
                self.assertEqual(urlsplit(source["url"]).scheme, "https")
                self.assertGreater(source.get("interval_minutes", 30), 0)


class PublisherAliasTests(unittest.TestCase):
    def test_aliases_match_the_casefolded_names_publisher_compares(self):
        for domain, (key, label, aliases) in PUBLISHERS.items():
            for alias in aliases:
                with self.subTest(domain=domain, alias=alias):
                    self.assertEqual(alias, " ".join(alias.split()).casefold())

    def test_no_alias_or_label_names_two_publishers(self):
        owners: dict[str, set[str]] = {}
        for key, label, aliases in PUBLISHERS.values():
            for name in (*aliases, label.casefold()):
                owners.setdefault(name, set()).add(key)
        self.assertFalse({name: keys for name, keys in owners.items() if len(keys) > 1})

    def test_a_direct_article_and_an_attributed_one_are_the_same_publisher(self):
        for domain, (key, label, aliases) in PUBLISHERS.items():
            direct = publisher({"url": f"https://www.{domain}/article"})
            for name in (label, *aliases):
                with self.subTest(domain=domain, name=name):
                    attributed = publisher({"url": "https://news.google.com/rss/articles/x",
                                            "extra": {"publisher": name}})
                    self.assertEqual((direct[0], attributed[0]), (key, key))

    def test_subdomains_of_a_publisher_resolve_to_it(self):
        for url, key in (("https://blogs.nvidia.com/blog/x/", "nvidia"),
                         ("https://nvidianews.nvidia.com/news/x", "nvidia"),
                         ("https://machinelearning.apple.com/research/x", "apple"),
                         ("https://ir.amd.com/news-events/press-releases/detail/1", "amd"),
                         ("https://newsroom.arm.com/blog/x", "arm")):
            with self.subTest(url=url):
                self.assertEqual(publisher({"url": url})[0], key)


if __name__ == "__main__":
    unittest.main()
