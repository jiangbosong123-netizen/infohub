import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.analysis_runs import AnalysisRunError
from app.ingest import store_payload
from app.tone_contracts import TONE_SCHEMA_V1, TONE_SCHEMA_VERSION
from app.tone_evidence import verify_tone_quotes


class ToneEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.blobs = Path(self.temp.name) / "blobs"
        self.patch = patch.object(config, "BLOB_PATH", self.blobs)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute("""CREATE TABLE raw_records(
            id TEXT PRIMARY KEY,payload_ref TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,media_type TEXT NOT NULL,encoding TEXT)""")

    def store(self, payload, evidence_id="raw-1"):
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest, reference = store_payload(encoded)
        self.db.execute(
            "INSERT INTO raw_records VALUES(?,?,?,?,?,?)",
            (evidence_id, reference, digest, len(encoded), "application/json", "utf-8"),
        )
        return digest, self.blobs / reference

    @staticmethod
    def data(pointer="/source_record/summary", quote="偏好🙂", start=2, end=5):
        return {"assessments": [{"evidence": [{
            "evidence_id": "raw-1", "quote": quote,
            "locator": {
                "type": "json_pointer", "json_pointer": pointer,
                "start_offset": start, "end_offset": end,
                "offset_unit": "unicode_code_point",
            },
        }]}]}

    def test_unicode_quote_is_reloaded_from_hash_checked_payload(self):
        digest, _ = self.store({"source_record": {"summary": "前文偏好🙂后文"}})
        result = verify_tone_quotes(
            self.db, schema_version=TONE_SCHEMA_VERSION, data=self.data()
        )
        self.assertEqual(result["status"], "passed")
        span = result["spans"][0]
        self.assertEqual(span["payload_sha256"], digest)
        self.assertEqual(span["quote_sha256"], hashlib.sha256("偏好🙂".encode()).hexdigest())

    def test_rfc6901_object_escape_and_array_index_are_supported(self):
        self.store({"source_record": {"a/b": {"~note": ["x", "Quoted"]}}})
        result = verify_tone_quotes(
            self.db, schema_version=TONE_SCHEMA_VERSION,
            data=self.data("/source_record/a~1b/~0note/1", "Quoted", 0, 6),
        )
        self.assertEqual(result["spans"][0]["json_pointer"], "/source_record/a~1b/~0note/1")

    def test_wrong_quote_pointer_or_scalar_is_rejected(self):
        self.store({"source_record": {"summary": "Evidence", "count": 2}})
        cases = [
            (self.data(quote="Evidencx", start=0, end=8), "does not match"),
            (self.data(pointer="/source_record/missing", quote="A", start=0, end=1), "does not exist"),
            (self.data(pointer="/source_record/count", quote="2", start=0, end=1), "resolve to a string"),
            (self.data(pointer="/source_record/~2bad", quote="A", start=0, end=1), "is invalid"),
        ]
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(AnalysisRunError, message):
                verify_tone_quotes(self.db, schema_version=TONE_SCHEMA_VERSION, data=data)

    def test_corrupt_cas_or_ledger_size_is_rejected(self):
        _, path = self.store({"source_record": {"summary": "Evidence"}})
        original = path.read_bytes()
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(AnalysisRunError, "missing or corrupt"):
            verify_tone_quotes(
                self.db, schema_version=TONE_SCHEMA_VERSION,
                data=self.data(quote="Evidence", start=0, end=8),
            )
        path.write_bytes(original)
        self.db.execute("UPDATE raw_records SET size_bytes=size_bytes+1 WHERE id='raw-1'")
        with self.assertRaisesRegex(AnalysisRunError, "does not match its ledger"):
            verify_tone_quotes(
                self.db, schema_version=TONE_SCHEMA_VERSION,
                data=self.data(quote="Evidence", start=0, end=8),
            )

    def test_v1_structural_results_are_explicitly_not_verified(self):
        result = verify_tone_quotes(self.db, schema_version=TONE_SCHEMA_V1, data={})
        self.assertEqual(result, {
            "validator_version": None, "status": "not_applicable", "spans": [],
        })


if __name__ == "__main__":
    unittest.main()
