import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.evaluation import validate_evaluation_dataset
from app.evaluation_admission import admit_sampling_plan
from app.evaluation_sampling import build_sampling_plan
from app.owner_label_console import (
    OwnerConsoleError,
    OwnerLabelSession,
    create_owner_label_console,
)
from app.owner_label_intake import import_owner_label_batch

MODEL_TEXT = ("MODEL SUMMARY 模型摘要", "模型翻译标题", "MODEL REASON 理由", "987654")


class OwnerLabelConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "app.db"
        db = sqlite3.connect(self.database)
        db.executescript("""
            CREATE TABLE sources(id INTEGER PRIMARY KEY,key TEXT,name TEXT,type TEXT,tier TEXT);
            CREATE TABLE items(
                id INTEGER PRIMARY KEY,source_id INTEGER,url TEXT,title TEXT,title_en TEXT,title_zh TEXT,
                summary TEXT,raw_summary TEXT,channel TEXT,event_type TEXT,official INTEGER,
                published_at TEXT,score INTEGER,tmt INTEGER,reason TEXT,ai_cat TEXT
            );
            CREATE TABLE stories(id TEXT PRIMARY KEY);
            CREATE TABLE story_items(item_id INTEGER PRIMARY KEY,story_id TEXT);
            CREATE TABLE companies(id INTEGER PRIMARY KEY,slug TEXT,market TEXT,cik TEXT);
            CREATE TABLE item_companies(item_id INTEGER,company_id INTEGER);
            CREATE TABLE item_topics(item_id INTEGER,topic_slug TEXT);
            INSERT INTO sources VALUES(1,'wire','Example Wire','rss','media'),(2,'cn','示例快讯','rss','media');
        """)
        rows = []
        for item_id in range(1, 13):
            chinese = item_id % 2 == 0
            rows.append((
                item_id, 2 if chinese else 1, f"https://news.test/{item_id}",
                f"公司发布芯片 {item_id}" if chinese else f"Chipmaker update {item_id}",
                "", "", f"source excerpt {item_id}", f"source excerpt {item_id}",
                "ai", "product", 0, f"2026-09-{10 + item_id:02d}T08:00:00Z", 70, 1, "reason", None,
            ))
        db.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        # An old row whose summary was overwritten by the model and has no raw_summary.
        db.execute(
            "UPDATE items SET summary=?,raw_summary=NULL,title_zh=?,reason=?,score=? WHERE id=3",
            (MODEL_TEXT[0], MODEL_TEXT[1], MODEL_TEXT[2], int(MODEL_TEXT[3])),
        )
        db.commit()
        db.close()
        plan = build_sampling_plan(self.database, self.root / "plan", target=12, seed="console")
        self.assertEqual(plan.selected, 12)
        self.dataset = self.root / "unlabeled"
        admit_sampling_plan(
            self.root / "plan", self.dataset, dataset_version="console-unlabeled-v1",
            database=self.database,
        )
        self.drafts = self.root / "drafts"

    def session(self, owner="owner-1") -> OwnerLabelSession:
        return OwnerLabelSession(self.dataset, self.database, self.drafts, owner_id=owner)

    def test_labels_flow_from_console_through_export_into_owner_tier(self):
        session = self.session()
        splits = [session.by_id[case_id]["split"] for case_id in session.order]
        self.assertEqual(splits, sorted(splits, key=["test", "dev", "train", "security"].index))
        chosen = {}
        case_id = session.next_case()
        while case_id is not None:
            label = ("relevant", "not_relevant", "unknown")[len(chosen) % 3]
            session.record(case_id, label, session.by_id[case_id]["content_sha256"])
            chosen[case_id] = label
            case_id = session.next_case(after=case_id)
        self.assertEqual(len(chosen), 12)
        # Changing a judgment before export appends; the latest draft label wins.
        first = session.order[0]
        session.record(first, "unknown", session.by_id[first]["content_sha256"])
        chosen[first] = "unknown"

        batch = self.root / "batch-1"
        summary = session.export(batch)
        self.assertEqual(summary["exported_labels"], 12)
        self.assertEqual(summary["progress"]["labeled"], 12)
        manifest = json.loads((batch / "manifest.json").read_text())
        self.assertEqual(
            (manifest["task"], manifest["label_definition"], manifest["blind"]),
            ("relevance", "relevance-definition-v1", True),
        )
        report = import_owner_label_batch(
            "relevance", self.dataset, batch, self.root / "owner-v1",
            dataset_version="console-owner-v1",
        )
        self.assertEqual(report.owner_labeled_cases, 12)
        self.assertFalse(report.publishable_gold)
        labeled = {
            case["case_id"]: case["annotation"]["labels"]["relevance"]
            for case in map(json.loads, (self.root / "owner-v1" / "cases.jsonl").read_text().splitlines())
        }
        self.assertEqual(labeled, chosen)
        self.assertEqual(
            validate_evaluation_dataset(self.root / "owner-v1").annotation_state_counts,
            {"owner_labeled": 12},
        )

    def test_console_shows_only_source_text_never_model_fields(self):
        session = self.session()
        client = TestClient(create_owner_label_console(session, csrf_token="token"))
        pages = [client.get(f"/case/{case_id}").text for case_id in session.order]
        for page in pages:
            for value in MODEL_TEXT:
                self.assertNotIn(value, page)
            self.assertNotIn("https://news.test", page)
        model_case = next(
            case_id for case_id in session.order
            if session.by_id[case_id]["document_ref"] == "legacy-item:3"
        )
        self.assertEqual(session.content(model_case).text, "")
        self.assertIn("没有原文摘录", client.get(f"/case/{model_case}").text)

    def test_changed_source_content_stops_labeling(self):
        session = self.session()
        case_id = session.order[0]
        item_id = int(session.by_id[case_id]["object_ref"].rsplit("/", 1)[1])
        db = sqlite3.connect(self.database)
        db.execute("UPDATE items SET raw_summary='edited later' WHERE id=?", (item_id,))
        db.commit()
        db.close()
        with self.assertRaisesRegex(OwnerConsoleError, "changed since sampling"):
            session.content(case_id)
        with self.assertRaisesRegex(OwnerConsoleError, "changed since sampling"):
            session.record(case_id, "relevant", session.by_id[case_id]["content_sha256"])
        client = TestClient(create_owner_label_console(session, csrf_token="token"))
        self.assertIn("changed since sampling", client.get(f"/case/{case_id}").text)

    def test_forms_require_csrf_matching_hash_and_loopback_host(self):
        session = self.session()
        client = TestClient(create_owner_label_console(session, csrf_token="token"))
        case_id = session.order[0]
        digest = session.by_id[case_id]["content_sha256"]
        page = client.get(f"/case/{case_id}")
        nonce = re.search(r"script-src 'nonce-([^']+)'", page.headers["content-security-policy"]).group(1)
        self.assertIn(f'nonce="{nonce}"', page.text)
        bad = client.post(f"/label/{case_id}", data={
            "csrf_token": "wrong", "content_sha256": digest, "label": "relevant"})
        self.assertEqual(bad.status_code, 403)
        stale = client.post(f"/label/{case_id}", data={
            "csrf_token": "token", "content_sha256": "0" * 64, "label": "relevant"})
        self.assertEqual(stale.status_code, 409)
        invalid = client.post(f"/label/{case_id}", data={
            "csrf_token": "token", "content_sha256": digest, "label": "maybe"})
        self.assertEqual(invalid.status_code, 409)
        self.assertEqual(session.drafts(), {})
        ok = client.post(f"/label/{case_id}", data={
            "csrf_token": "token", "content_sha256": digest, "label": "relevant"},
            follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(session.drafts()[case_id]["label"], "relevant")
        remote = TestClient(create_owner_label_console(session, csrf_token="token"),
                            base_url="http://203.0.113.5")
        self.assertEqual(remote.get("/").status_code, 400)

    def test_private_paths_owner_binding_and_immutable_export(self):
        repository = Path(__file__).parents[1]
        with self.assertRaisesRegex(OwnerConsoleError, "draft directory must be outside"):
            OwnerLabelSession(self.dataset, self.database, repository / "evaluation" / "drafts",
                              owner_id="owner-1")
        session = self.session()
        with self.assertRaisesRegex(OwnerConsoleError, "no labels to export"):
            session.export(self.root / "empty-batch")
        case_id = session.order[0]
        session.record(case_id, "relevant", session.by_id[case_id]["content_sha256"])
        session.export(self.root / "batch")
        with self.assertRaisesRegex(OwnerConsoleError, "already exists"):
            session.export(self.root / "batch")
        import_owner_label_batch(
            "relevance", self.dataset, self.root / "batch", self.root / "owner-v1",
            dataset_version="console-owner-v1",
        )
        # The next session continues on the new version but must keep the same owner.
        with self.assertRaisesRegex(OwnerConsoleError, "another owner"):
            OwnerLabelSession(self.root / "owner-v1", self.database, self.drafts, owner_id="owner-2")
        follow_up = OwnerLabelSession(self.root / "owner-v1", self.database, self.drafts,
                                      owner_id="owner-1")
        self.assertEqual(len(follow_up.order), 11)
        self.assertNotIn(case_id, follow_up.by_id)


if __name__ == "__main__":
    unittest.main()
