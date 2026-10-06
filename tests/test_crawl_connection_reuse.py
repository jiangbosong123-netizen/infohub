import contextlib
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.crawler import googlenews, runner

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def candidates(round_number: int) -> list[dict]:
    """Round 1 is new, round 2 repeats it, round 3 changes ten summaries and adds five items."""
    count = 55 if round_number >= 3 else 50
    out = []
    for n in range(count):
        published = (NOW - timedelta(minutes=n)).isoformat()
        summary = f"Body {n}" + (" revised" if round_number >= 3 and n < 10 else "")
        out.append(dict(
            url=f"https://example.test/a/{n}", title=f"NVIDIA article {n}", summary=summary,
            published_at=published, event_type="", official=0, companies=None, extra={},
            source_time_values=[], observed_at=NOW.isoformat(),
            source_record={"title": f"NVIDIA article {n}", "link": f"https://example.test/a/{n}",
                           "summary": summary, "published": published},
            payload_kind="feed_entry",
        ))
    out.append({"_error": "one company timed out"} if round_number == 2 else
               dict(url="", title="", summary="dropped without a URL"))
    return out


GENERATED = re.compile(r"[a-z_]*[0-9a-f]{32}|\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})?")


def contents(path: Path) -> dict:
    """Every table without generated ids and clock values, as sorted rows."""
    db = sqlite3.connect(path)
    out = {}
    for (table,) in db.execute("""SELECT name FROM sqlite_master WHERE type='table'
                                  AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'"""):
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')
                   if row[1] != "id" and not row[1].endswith(("_id", "_at")) and row[1] != "ran_at"]
        if columns:
            out[table] = sorted(GENERATED.sub("<generated>", repr(row)) for row in db.execute(
                f'SELECT {",".join(chr(34) + c + chr(34) for c in columns)} FROM "{table}"'))
    db.close()
    return out


class CrawlConnectionReuseTests(unittest.TestCase):
    def scenario(self, reuse: bool) -> tuple[dict, list[int]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / "app.db"
        connections = []
        real_connect = sqlite3.connect

        def counted(*args, **kwargs):
            connections.append(1)
            return real_connect(*args, **kwargs)

        with contextlib.ExitStack() as stack:
            for target, name, value in ((database, "DB_PATH", path), (config, "DB_PATH", path),
                                        (config, "BLOB_PATH", root / "blobs")):
                stack.enter_context(patch.object(target, name, value))
            if not reuse:
                stack.enter_context(patch.object(runner, "reused_connections", contextlib.nullcontext))
            database.init_schema()
            runner.upsert_sources()
            source = next(s for s in runner.all_sources() if s["key"] == "techcrunch")
            per_run = []
            for round_number in (1, 2, 3):
                connections.clear()
                with patch.dict(runner.FETCHERS, {"rss": lambda _s, n=round_number: candidates(n)}), \
                        patch.object(database.sqlite3, "connect", counted):
                    runner.run_source(source)
                per_run.append(len(connections))
        return contents(path), per_run

    def test_reused_connections_store_exactly_the_same_rows(self):
        separate, separate_connections = self.scenario(reuse=False)
        reused, reused_connections = self.scenario(reuse=True)
        self.assertEqual(reused, separate)
        self.assertEqual(len(reused["items"]), 55)
        self.assertEqual(len(reused["raw_observations"]), 50 + 51 + 56)
        self.assertGreater(len(reused["document_versions"]), 55)  # the ten revisions
        self.assertTrue(all(n > 100 for n in separate_connections), separate_connections)
        self.assertTrue(all(n <= 6 for n in reused_connections), reused_connections)

    def test_reconcile_reuses_connections_only_while_storing(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        events = []
        with patch.object(database, "DB_PATH", root / "app.db"), \
                patch.object(config, "DB_PATH", root / "app.db"), \
                patch.object(config, "BLOB_PATH", root / "blobs"):
            database.init_schema()
            with database.get_db() as db:
                db.execute("""INSERT INTO companies(slug,name,name_zh,ticker,code,market,aliases)
                              VALUES('nvidia','NVIDIA','英伟达','NVDA','','US','["NVIDIA"]')""")
            runner.upsert_sources()

            def fetch(slug, name, aliases):
                events.append(("fetch", database._reuse.scope is not None))
                return candidates(1)[:3] if slug == "nvidia" else []

            def refresh():
                events.append(("refresh", getattr(database._reuse, "scope", None) is not None))

            with patch.object(googlenews, "fetch_company_news", fetch), \
                    patch("app.stories.refresh_derived", refresh):
                stats = googlenews.run_reconcile()
        self.assertEqual(stats["nvidia"]["inserted"], 3)
        self.assertTrue(all(inside for kind, inside in events if kind == "fetch"))
        self.assertEqual([e for e in events if e[0] == "refresh"], [("refresh", False)])


if __name__ == "__main__":
    unittest.main()
