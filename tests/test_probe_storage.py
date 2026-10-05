import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

# The probe starts its writers with "spawn", so it must be importable by name in the children.
PROBE_DIRECTORY = str(Path(__file__).resolve().parents[1] / "deploy" / "windows")


def load_probe():
    if PROBE_DIRECTORY not in sys.path:
        sys.path.insert(0, PROBE_DIRECTORY)
    return importlib.import_module("probe_storage")


class ProbeStorageTests(unittest.TestCase):
    def test_quick_probe_reports_and_leaves_nothing_behind(self):
        probe = load_probe()
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = probe.main([folder, "--quick"])
            self.assertEqual(list(Path(folder).iterdir()), [])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(report["concurrent_wal_writers"]["safe"])
        self.assertEqual(report["concurrent_wal_writers"]["final_value"], 100)
        self.assertEqual(report["verdict"], "concurrency check passed")
        self.assertTrue(output.getvalue().isascii())

    def test_lost_updates_corruption_or_crashes_are_unsafe(self):
        probe = load_probe()
        healthy = {"increments": 1000, "final_value": 1000, "integrity": "ok", "exit_codes": [0, 0]}
        self.assertTrue(probe.wal_writers_safe(healthy))
        for change in ({"final_value": 997}, {"integrity": "database disk image is malformed"},
                       {"exit_codes": [0, 1]}):
            with self.subTest(change=change):
                self.assertFalse(probe.wal_writers_safe({**healthy, **change}))


if __name__ == "__main__":
    unittest.main()
