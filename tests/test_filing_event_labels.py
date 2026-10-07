import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.crawler import hkex_source, sec_source

CATEGORIES = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "hkex-headline-categories-20261007.json"


class SecEightKItemTests(unittest.TestCase):
    def test_item_combinations_seen_in_the_rehearsal_data(self):
        cases = {
            "2.02,9.01": ("8-K · 业绩披露", "earnings"),
            "2.02,8.01,9.01": ("8-K · 业绩披露", "earnings"),
            "5.02,7.01,9.01": ("8-K · 高管/董事变动", "personnel"),
            # Credit agreements and note indentures: financing, not acquisitions.
            "1.01,2.03,9.01": ("8-K · 新增直接债务", "offering"),
            "1.01,1.02,2.03,9.01": ("8-K · 新增直接债务", "offering"),
            "1.01,2.03,7.01": ("8-K · 新增直接债务", "offering"),
            "1.01,8.01,9.01": ("8-K · 签订重大协议", "other"),
            "1.01,3.03,5.03,9.01": ("8-K · 签订重大协议", "other"),
            "3.02": ("8-K · 未注册股权发行", "offering"),
            "5.07": ("8-K · 股东大会表决结果", "other"),
            "7.01,9.01": ("8-K · 投资者披露", "other"),
            "8.01,9.01": ("8-K · 其他事件", "other"),
        }
        for items, expected in cases.items():
            with self.subTest(items=items):
                self.assertEqual(sec_source._classify("8-K", items), expected)

    def test_the_most_telling_item_decides(self):
        self.assertEqual(sec_source._classify("8-K", "1.01,2.01,9.01"), ("8-K · 收购或处置完成", "ma"))
        self.assertEqual(sec_source._classify("8-K", "1.01,5.02"), ("8-K · 高管/董事变动", "personnel"))
        self.assertEqual(sec_source._classify("8-K", "1.05,8.01"), ("8-K · 重大网络安全事件", "other"))
        self.assertEqual(sec_source._classify("8-K/A", "2.02"), ("8-K · 业绩披露修订", "earnings"))
        # Exhibits alone never name the filing.
        self.assertEqual(sec_source._classify("8-K", "9.01"), ("8-K · 重大事件", "other"))

    def test_items_are_whole_codes(self):
        self.assertEqual(sec_source._item_codes(" 2.02, 9.01 "), ["2.02", "9.01"])
        self.assertEqual(sec_source._classify("8-K", "12.02"), ("8-K · 重大事件", "other"))
        self.assertEqual(sec_source._items_text("2.02,9.01"), "2.02 业绩披露、9.01 财务报表与附件")

    def test_forms(self):
        self.assertEqual(sec_source._classify("S-4", ""), ("合并或置换证券注册", "other"))
        self.assertEqual(sec_source._classify("S-8", ""), ("员工股权计划注册", "other"))
        self.assertEqual(sec_source._classify("SCHEDULE 13G/A", ""), ("大股东持股披露修订", "insider"))
        self.assertEqual(sec_source._classify("3", ""), ("内部人首次持股报告", "insider"))
        self.assertEqual(sec_source._classify("424B2", ""), ("证券发行", "offering"))
        self.assertEqual(sec_source._classify("N-PX", ""), ("提交文件", ""))  # left to the AI


class HkexHeadlineCategoryTests(unittest.TestCase):
    def test_long_text_splits_into_tier_one_and_tier_two(self):
        cases = {
            "翌日披露報表 - [其他 &#x2f; 股份購回]<br/>": ("翌日披露報表", ["其他", "股份購回"]),
            "財務報表&#x2f;環境、社會及管治資料 - [中期&#x2f;半年度報告]": (
                "財務報表/環境、社會及管治資料", ["中期/半年度報告"]),
            "公告及通告 - [內幕消息 &#x2f; 其他-業務發展最新情況 &#x2f; 季度業績]": (
                "公告及通告", ["內幕消息", "其他-業務發展最新情況", "季度業績"]),
            "債券及結構性產品 - [上市通告 － 債務證券]": ("債券及結構性產品", ["上市通告 － 債務證券"]),
            "月報表<br/>": ("月報表", []),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hkex_source._headline_category({"LONG_TEXT": text}), expected)

    def test_categories_seen_for_the_watched_companies(self):
        cases = [
            ("公告及通告", ["內幕消息", "其他-業務發展最新情況", "季度業績"], "earnings"),
            ("財務報表/環境、社會及管治資料", ["年報", "環境、社會及管治資料/報告"], "earnings"),
            ("公告及通告", ["海外監管公告-營運業績最新情況"], "earnings"),
            ("公告及通告", ["須予披露的交易"], "ma"),
            ("公告及通告", ["分拆"], "ma"),
            ("公告及通告", ["根據一般性授權發行股份", "海外監管公告-證券發行及相關事宜"], "offering"),
            ("債券及結構性產品", ["上市通告 － 債務證券"], "offering"),
            ("債券及結構性產品", ["贖回或購回 － 債務證券"], "other"),
            ("翌日披露報表", ["股份購回"], "buyback"),
            ("翌日披露報表", ["其他"], "other"),
            ("公告及通告", ["股東周年大會的結果", "更換董事或重要行政職能或職責的變更"], "personnel"),
            # Routine AGM circulars re-elect directors; that is not a management change.
            ("通函", ["在股東批准的情況下重選或委任董事", "一般性授權", "回購股份的說明函件"], "other"),
            ("公告及通告", ["董事會召開日期"], "other"),
            ("委任代表表格", [], "other"),
            ("月報表", [], ""),
        ]
        for group, categories, expected in cases:
            with self.subTest(categories=categories or group):
                self.assertEqual(hkex_source._classify("任意標題 回購 業績", group, categories), expected)

    def test_uncategorised_records_fall_back_to_the_title(self):
        self.assertEqual(hkex_source._classify("業績公告"), "earnings")
        self.assertEqual(hkex_source._classify("月報表"), "")
        self.assertEqual(hkex_source._classify("一般資料"), "other")

    def test_every_mapped_category_is_an_official_hkex_name(self):
        reference = json.loads(CATEGORIES.read_text(encoding="utf-8"))
        official = {name for groups in reference["tier_two"].values()
                    for names in groups.values() for name in names}
        mapped = set().union(*(names for _, names in hkex_source._CATEGORY_EVENTS))
        self.assertEqual(sorted(mapped - official), [])
        groups = set(hkex_source._GROUP_EVENTS) | hkex_source._SKIPPED_GROUPS | hkex_source._TIER_ONE
        self.assertEqual(sorted(groups - set(reference["tier_one"])), [])

    def test_items_carry_the_announcement_title_and_its_categories(self):
        records = [
            {"TITLE": "截至二零二六年六月三十日止三個月及六個月業績公佈",
             "LONG_TEXT": "公告及通告 - [中期業績]", "FILE_LINK": "/a.pdf", "DATE_TIME": "13/08/2026 16:30"},
            {"TITLE": "翌日披露報表", "LONG_TEXT": "翌日披露報表 - [股份購回]<br/>",
             "FILE_LINK": "/b.pdf", "DATE_TIME": "14/08/2026 17:30"},
            {"TITLE": "截至二零二六年七月三十一日止之股份發行人的證券變動月報表", "LONG_TEXT": "月報表",
             "FILE_LINK": "/c.pdf", "DATE_TIME": "04/08/2026 16:38"},
        ]
        company = {"slug": "tencent", "name": "Tencent", "name_zh": "腾讯控股", "code": "0700",
                   "hkex_stock_id": "7609"}
        response = SimpleNamespace(text=json.dumps({"result": json.dumps(records, ensure_ascii=False)}))
        with patch.object(hkex_source.http, "fetch", return_value=response):
            rows = hkex_source._fetch_company(company, "20260801", "20260815")
        self.assertEqual([row["title"] for row in rows], [
            "腾讯控股 · 港交所公告：截至二零二六年六月三十日止三個月及六個月業績公佈",
            "腾讯控股 · 港交所公告：翌日披露報表（股份購回）",
        ])
        self.assertEqual([row["event_type"] for row in rows], ["earnings", "buyback"])
        self.assertEqual(rows[0]["extra"], {"code": "0700", "hkex_category": "公告及通告",
                                            "hkex_subcategories": ["中期業績"]})
        self.assertIn("分类：公告及通告 - 中期業績", rows[0]["summary"])
        self.assertNotIn("分类", rows[1]["summary"])  # already in the title


if __name__ == "__main__":
    unittest.main()
