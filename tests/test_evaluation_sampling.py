import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.evaluation_sampling import EvaluationSamplingError, build_sampling_plan


class EvaluationSamplingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "app.db"
        db = sqlite3.connect(self.database)
        db.executescript("""
            CREATE TABLE sources(id INTEGER PRIMARY KEY,key TEXT,name TEXT,type TEXT,tier TEXT);
            CREATE TABLE items(
                id INTEGER PRIMARY KEY,source_id INTEGER,url TEXT,title TEXT,title_en TEXT,title_zh TEXT,
                summary TEXT,raw_summary TEXT,channel TEXT,event_type TEXT,official INTEGER,
                published_at TEXT
            );
            CREATE TABLE stories(id TEXT PRIMARY KEY);
            CREATE TABLE story_items(item_id INTEGER PRIMARY KEY,story_id TEXT);
            CREATE TABLE companies(id INTEGER PRIMARY KEY,slug TEXT,market TEXT,cik TEXT);
            CREATE TABLE item_companies(item_id INTEGER,company_id INTEGER);
            CREATE TABLE item_topics(item_id INTEGER,topic_slug TEXT);
            INSERT INTO sources VALUES(1,'sec','SEC','sec','official'),(2,'news','News','rss','media');
            INSERT INTO companies VALUES(1,'example-us','US','0000012345');
            INSERT INTO stories VALUES('event-a'),('event-b');
            INSERT INTO items VALUES
              (1,1,'https://sec.gov/a?x=1','Example files 10-K','','','Filing body',NULL,'us','filing',1,'2026-01-02T10:00:00Z'),
              (2,2,'https://news.test/cn','公司发布新产品','','公司发布新产品','摘要',NULL,'cn','launch',0,'2026-04-02T10:00:00Z'),
              (3,2,'https://news.test/en','Example launches product','Example launches product','','',NULL,'us','launch',0,'2026-04-03T10:00:00Z');
            INSERT INTO story_items VALUES(1,'event-a'),(2,'event-a'),(3,'event-b');
            INSERT INTO item_companies VALUES(1,1),(3,1);
            INSERT INTO item_topics VALUES(1,'filings'),(2,'products');
        """)
        db.commit(); db.close()

    def digest(self):
        return hashlib.sha256(self.database.read_bytes()).hexdigest()

    def test_plan_is_deterministic_read_only_and_contains_no_content(self):
        before = self.digest()
        first = build_sampling_plan(self.database, self.root / "one", target=3, seed="fixed")
        second = build_sampling_plan(self.database, self.root / "two", target=3, seed="fixed")
        self.assertEqual(before, self.digest())
        self.assertEqual(first.candidates_path.read_bytes(), second.candidates_path.read_bytes())
        rows = [json.loads(line) for line in first.candidates_path.read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertFalse({"title", "summary", "raw_summary", "text", "url"}.intersection(row))
            self.assertTrue(row["content_sha256"])
        report = json.loads(first.report_path.read_text())
        self.assertEqual(report["selected"]["languages"], {"en": 2, "zh": 1})
        self.assertEqual(report["selected"]["sec_related"], 1)
        self.assertEqual(report["selected"]["us_market_linked"], 2)

    def test_target_must_be_positive(self):
        with self.assertRaisesRegex(EvaluationSamplingError, "positive"):
            build_sampling_plan(self.database, self.root / "bad", target=0)

    def test_full_plan_reserves_two_hundred_cases_per_primary_language(self):
        db = sqlite3.connect(self.database)
        rows = []
        for item_id in range(4, 404):
            chinese = item_id < 204
            rows.append((
                item_id, 2, f"https://news.test/{item_id}",
                f"公司消息 {item_id}" if chinese else f"Company update {item_id}",
                "", "", "excerpt", None, "news", "update", 0,
                "2026-07-01T00:00:00Z",
            ))
        db.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit(); db.close()
        artifacts = build_sampling_plan(self.database, self.root / "balanced", target=400)
        report = json.loads(artifacts.report_path.read_text())
        self.assertGreaterEqual(report["selected"]["languages"]["zh"], 200)
        self.assertGreaterEqual(report["selected"]["languages"]["en"], 200)
        self.assertEqual(report["target_gaps"]["zh_documents"], 0)
        self.assertEqual(report["target_gaps"]["en_documents"], 0)

    def test_missing_schema_is_rejected(self):
        empty = self.root / "empty.db"
        sqlite3.connect(empty).close()
        with self.assertRaisesRegex(EvaluationSamplingError, "required tables"):
            build_sampling_plan(empty, self.root / "bad")


if __name__ == "__main__":
    unittest.main()
