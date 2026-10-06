import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, ingest
from app.crawler import fastnews, runner
from tests.test_crawl_connection_reuse import contents

SIGHTINGS = ["2026-10-06T10:00:00.000000Z", "2026-10-06T10:20:00.000000Z",
             "2026-10-06T10:40:00.000000Z", "2026-10-06T11:00:00.000000Z",
             "2026-10-06T11:20:00.000000Z"]


def feed(round_number: int) -> list[dict]:
    """Twenty items; items 0-4 change in round 3 and change back in round 4."""
    seen = SIGHTINGS[round_number - 1]
    out = []
    for n in range(20):
        revised = round_number == 3 and n < 5
        summary = f"Body {n}" + (" revised" if revised else "")
        out.append(dict(
            url=f"https://example.test/a/{n}", title=f"NVIDIA article {n}", summary=summary,
            published_at="2026-10-06T09:00:00+00:00", event_type="", official=0, companies=None,
            extra={}, source_time_values=[], observed_at=seen,
            source_record={"link": f"https://example.test/a/{n}", "summary": summary},
            payload_kind="feed_entry",
        ))
    return out


class ObservationOnChangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def crawl(self, name: str, *, repeat_unchanged: bool) -> Path:
        path = self.root / name / "app.db"
        real = ingest.observe_candidate

        def observe(*args, **kwargs):
            kwargs["repeat_unchanged"] = repeat_unchanged
            return real(*args, **kwargs)

        with contextlib.ExitStack() as stack:
            for target, attr, value in ((database, "DB_PATH", path), (config, "DB_PATH", path),
                                        (config, "BLOB_PATH", self.root / name / "blobs")):
                stack.enter_context(patch.object(target, attr, value))
            stack.enter_context(patch.object(runner, "observe_candidate", observe))
            database.init_schema()
            runner.upsert_sources()
            source = next(s for s in runner.all_sources() if s["key"] == "techcrunch")
            for round_number in range(1, 6):
                with patch.dict(runner.FETCHERS, {"rss": lambda _s, n=round_number: feed(n)}):
                    runner.run_source(source)
        return path

    def test_only_new_and_changed_content_is_observed_and_everything_else_is_unchanged(self):
        every = contents(self.crawl("every", repeat_unchanged=True))
        changes = contents(self.crawl("changes", repeat_unchanged=False))
        self.assertEqual(len(every.pop("raw_observations")), 20 * 5)
        # First sightings, the five revisions, and the five returns to the earlier text.
        self.assertEqual(len(changes.pop("raw_observations")), 20 + 0 + 5 + 5 + 0)
        self.assertEqual(changes, every)

    def test_locators_keep_the_latest_sighting_of_unchanged_content(self):
        path = self.crawl("changes", repeat_unchanged=False)
        db = sqlite3.connect(path)
        rows = db.execute("""SELECT first_observed_at,last_observed_at FROM document_locators""").fetchall()
        self.assertEqual(len(rows), 20)
        self.assertEqual(set(rows), {(SIGHTINGS[0], SIGHTINGS[-1])})
        db.close()


class ClsEngagementTests(unittest.TestCase):
    def payload(self, **changes):
        record = {"id": 7, "title": "", "content": "财联社电报 7 号。正文", "ctime": 1791300000,
                  "reading_num": 100, "comment_num": 0, "share_num": 1,
                  "subjects": [{"subject_id": 1, "subject_name": "A", "attention_num": 27439}]}
        record.update(changes)
        raw = fastnews._cls_raws([record], fastnews.datetime(2026, 10, 6, tzinfo=fastnews.timezone.utc),
                                 share_links=True)[0]
        return ingest._candidate_payload(raw)

    def test_engagement_counters_are_not_part_of_the_evidence(self):
        base = self.payload()
        self.assertEqual(self.payload(
            reading_num=250, comment_num=3, share_num=9,
            subjects=[{"subject_id": 1, "subject_name": "A", "attention_num": 27440}]), base)
        self.assertNotIn(b"reading_num", base)
        self.assertNotIn(b"attention_num", base)
        self.assertIn(b"subject_name", base)
        self.assertNotEqual(self.payload(content="财联社电报 7 号。更正后的正文"), base)
        self.assertNotEqual(self.payload(
            subjects=[{"subject_id": 2, "subject_name": "B", "attention_num": 1}]), base)


class ObserveCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, attr, value in ((database, "DB_PATH", root / "app.db"),
                                    (config, "DB_PATH", root / "app.db"),
                                    (config, "BLOB_PATH", root / "blobs")):
            item = patch.object(target, attr, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        runner.upsert_sources()
        self.source = next(s for s in runner.all_sources() if s["key"] == "techcrunch")

    def observe(self, text: str, seen: str, *, ordinal=0, run=None, repeat_unchanged=False):
        run = run or ingest.begin_ingest_run(self.source)
        candidate = dict(url="https://example.test/x", title="X", summary=text, observed_at=seen,
                         source_record={"link": "https://example.test/x", "summary": text},
                         payload_kind="feed_entry")
        return run, ingest.observe_candidate(run, candidate, ordinal=ordinal, observed_at=seen,
                                             repeat_unchanged=repeat_unchanged)

    def observations(self):
        with database.get_db() as db:
            return [tuple(r) for r in db.execute(
                """SELECT record.payload_sha256,observation.observed_at FROM raw_observations AS observation
                   JOIN raw_records AS record ON record.id=observation.raw_record_id
                   ORDER BY observation.observed_at""")]

    def test_repeats_changes_and_returns(self):
        _, first = self.observe("A", SIGHTINGS[0])
        _, repeat = self.observe("A", SIGHTINGS[1])
        self.assertEqual((repeat.observation_id, repeat.raw_record_id),
                         (first.observation_id, first.raw_record_id))
        self.assertEqual(repeat.observed_at, SIGHTINGS[1])
        _, changed = self.observe("B", SIGHTINGS[2])
        _, back = self.observe("A", SIGHTINGS[3])
        self.assertEqual(back.raw_record_id, first.raw_record_id)
        self.assertNotEqual(back.observation_id, first.observation_id)
        hashes = [h for h, _ in self.observations()]
        self.assertEqual(hashes, [first.payload_sha256, changed.payload_sha256, first.payload_sha256])

    def test_the_default_still_observes_every_repeat(self):
        self.observe("A", SIGHTINGS[0], repeat_unchanged=True)
        self.observe("A", SIGHTINGS[1], repeat_unchanged=True)
        self.assertEqual(len(self.observations()), 2)

    def test_a_retried_ordinal_in_one_run_stays_idempotent(self):
        run, first = self.observe("A", SIGHTINGS[0])
        _, again = self.observe("A", SIGHTINGS[0], run=run)
        self.assertEqual(again.observation_id, first.observation_id)
        with self.assertRaisesRegex(ingest.IngestEvidenceError, "reused for different content"):
            self.observe("B", SIGHTINGS[0], run=run)


if __name__ == "__main__":
    unittest.main()
