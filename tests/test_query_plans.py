"""Plans of statements the code actually runs, so hot lookups cannot silently scan whole tables."""

import re
import sys
import unittest
from contextlib import contextmanager

from app import database
from tests import test_api_analyses as api_fixtures
from tests import test_evidence_backup as backup_fixtures


@contextmanager
def traced_statements():
    """Expanded SQL of every statement run through any app module's get_db()."""
    statements = []
    original = database.get_db

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    modules = [module for name, module in list(sys.modules.items())
               if name.startswith("app") and getattr(module, "get_db", None) is original]
    for module in modules:
        module.get_db = traced
    try:
        yield statements
    finally:
        for module in modules:
            module.get_db = original


def plan(sql: str) -> list[str]:
    with database.get_db() as db:
        return [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)]


def with_page_of_ids(sql: str) -> str:
    """Fixtures hold one item; real pages pass dozens of ids. Without a pinned join order the
    planner switches to scanning every publication pointer from five ids on."""
    return re.sub(r"legacy_item_id IN \([^)]*\)",
                  "legacy_item_id IN (" + ",".join(str(n) for n in range(1, 31)) + ")", sql)


class QueryPlanTests(unittest.TestCase):
    def test_analysis_detail_finds_publications_by_result_through_an_index(self):
        fixture = api_fixtures.ApiAnalysisTests("test_openapi_declares_analysis_scope")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with traced_statements() as statements:
            response = fixture.client.get(
                f"/api/v1/analyses/{fixture.current_id}", headers=fixture.headers())
        self.assertEqual(response.status_code, 200)
        detail = [sql for sql in statements if "FROM analysis_results AS result" in sql]
        self.assertTrue(detail)
        lines = [line for sql in detail for line in plan(sql)]
        self.assertTrue(any("idx_analysis_publication_versions_result" in line for line in lines), lines)
        self.assertFalse([line for line in lines if line.startswith("SCAN")], lines)

    def test_report_input_pointers_start_from_the_reports_documents(self):
        fixture = backup_fixtures.EvidenceBackupTests(
            "test_bundle_contains_exact_referenced_objects_and_verifies")
        with traced_statements() as statements:
            fixture.setUp()  # freezes a calendar report input
        self.addCleanup(fixture.doCleanups)
        pointers = [sql for sql in statements
                    if "SELECT d.legacy_item_id,p.task_type,p.current_publication_id" in sql]
        self.assertEqual(len(pointers), 1)
        lines = plan(with_page_of_ids(pointers[0]))
        self.assertIn("legacy_item_id=?", lines[0])
        self.assertFalse([line for line in lines if line.startswith("SCAN")], lines)

    def test_portal_publication_reads_start_from_the_pages_documents(self):
        from app.curation_projection import published_curation
        fixture = backup_fixtures.EvidenceBackupTests(
            "test_bundle_contains_exact_referenced_objects_and_verifies")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with traced_statements() as statements, database.get_db() as db:
            published_curation(db, [1])
        reads = [sql for sql in statements if "FROM documents d" in sql]
        self.assertEqual(len(reads), 1)
        lines = plan(with_page_of_ids(reads[0]))
        self.assertIn("legacy_item_id=?", lines[0])
        self.assertFalse([line for line in lines if line.startswith("SCAN")], lines)


if __name__ == "__main__":
    unittest.main()
