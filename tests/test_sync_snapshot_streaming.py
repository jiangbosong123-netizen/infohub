from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rfc8785

from app import config, database, sync_snapshots
from app.sync_snapshots import RESOURCE_TYPES, SyncSnapshotError


# Ids whose UTF-8 byte order differs from UTF-16 code-unit order ("￿" sorts after an
# astral character in UTF-16) and from naive ASCII assumptions.
RESOURCE_IDS = ["b", "a", "é", "￿", "😀", "a-2", "Z", "ä", "a/1"]


def _reference_write(db, root: Path, reference_root: Path, resources, page_size, **identity):
    """The writer before streaming: every record of a resource in memory, sorted in Python."""
    manifests, total = [], 0
    for resource in resources:
        rows = db.execute(
            """SELECT change.* FROM change_log AS change
               JOIN (SELECT resource_id,MAX(seq) AS seq FROM change_log
                     WHERE dataset_id=? AND epoch=? AND resource_type=? AND seq<=?
                     GROUP BY resource_id) AS latest ON latest.seq=change.seq""",
            (identity["dataset_id"], identity["epoch"], RESOURCE_TYPES[resource],
             identity["high_water"]),
        ).fetchall()
        records = sorted(
            ({"resource_type": RESOURCE_TYPES[resource], "resource_id": row["resource_id"],
              "version_id": row["version_id"], "payload": json.loads(row["payload_json"])}
             for row in rows),
            key=lambda value: value["resource_id"].encode("utf-8"),
        )
        stream = hashlib.sha256()
        for record in records:
            stream.update(rfc8785.dumps(record) + b"\n")
        pages = []
        for offset in range(0, len(records), page_size):
            page_records = records[offset:offset + page_size]
            payload = rfc8785.dumps(page_records)
            path = root / "pages" / resource / f"{len(pages) + 1:08d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            pages.append({
                "page_number": len(pages) + 1,
                "first_resource_id": page_records[0]["resource_id"],
                "last_resource_id": page_records[-1]["resource_id"],
                "record_count": len(page_records),
                "payload_ref": (reference_root / path.relative_to(root))
                .relative_to(Path(config.RUNTIME_PATH)).as_posix(),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            })
        manifests.append({
            "resource": resource, "record_count": len(records), "page_count": len(pages),
            "content_sha256": stream.hexdigest(), "pages": pages,
        })
        total += len(records)
    return manifests, total


class SnapshotStreamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "app.db"
        for item in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "RUNTIME_PATH", self.root / "runtime"),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
        self.dataset_id, self.epoch = state["dataset_id"], state["current_epoch"]
        self.counter = 0

    def _change(self, resource_type: str, resource_id: str, version: int) -> None:
        self.counter += 1
        payload = {"id": resource_id, "version": version, "text": f"«{resource_id}» ✓",
                   "score": version / 3, "tags": [resource_id, None, True]}
        encoded = rfc8785.dumps(payload)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO change_log(
                       dataset_id,epoch,idempotency_key,resource_type,resource_id,version_id,
                       operation,available_at,payload_json,payload_sha256,hash_algorithm
                   ) VALUES(?,?,?,?,?,?,'update','2026-09-27T10:00:00+00:00',?,?,
                            'jcs-sha256-v1')""",
                (self.dataset_id, self.epoch, f"fixture:{self.counter}", resource_type,
                 resource_id, f"{resource_id}-v{version}", encoded.decode("utf-8"),
                 hashlib.sha256(encoded).hexdigest()),
            )

    def _fixture(self) -> int:
        for version in (1, 2):
            for resource_id in RESOURCE_IDS:
                self._change("item", resource_id, version)
        for resource_id in RESOURCE_IDS[:4]:
            self._change("analysis", resource_id, 1)
        high_water = self.counter
        # Written after the high-water mark, so no snapshot may include it.
        self._change("item", "a", 3)
        return high_water

    def _write(self, writer, name: str, page_size: int, high_water: int):
        # Each writer stages separately; both publish to the same final location.
        root = Path(config.RUNTIME_PATH) / f"staging-{name}"
        reference_root = Path(config.RUNTIME_PATH) / "final"
        with database.get_db() as db:
            manifests, total = writer(
                db, root, reference_root, resources=list(RESOURCE_TYPES), page_size=page_size,
                dataset_id=self.dataset_id, epoch=self.epoch, high_water=high_water,
            )
        files = {path.relative_to(root).as_posix(): path.read_bytes()
                 for path in sorted(root.rglob("*")) if path.is_file()}
        return manifests, total, files

    def test_streamed_pages_are_byte_identical_to_whole_resource_pages(self):
        high_water = self._fixture()

        def streamed(db, root, reference_root, **options):
            return sync_snapshots._write_resources(
                db, root, reference_root, snapshot_id="snapshot", **options)

        for page_size in (1, 2, 3, 4, 9, 100):
            with self.subTest(page_size=page_size):
                expected = self._write(_reference_write, f"reference-{page_size}", page_size,
                                       high_water)
                actual = self._write(streamed, f"streamed-{page_size}", page_size, high_water)
                self.assertEqual(actual, expected)

        # The last comparison (one page per resource) also pins what the reference produced.
        manifests, total, files = expected
        self.assertEqual(total, len(RESOURCE_IDS) + 4)
        self.assertEqual(
            {manifest["resource"]: manifest["record_count"] for manifest in manifests
             if manifest["record_count"]},
            {"items": len(RESOURCE_IDS), "analyses": 4},
        )
        items = json.loads(files["pages/items/00000001.json"])
        self.assertEqual(
            [record["resource_id"] for record in items],
            sorted(RESOURCE_IDS, key=lambda value: value.encode("utf-8")),
        )
        self.assertEqual({record["payload"]["version"] for record in items}, {2})

    def test_pages_are_written_while_records_are_still_being_read(self):
        high_water = self._fixture()
        read = 0
        read_at_write = []
        original = sync_snapshots._resource_records

        def counted(*args, **kwargs):
            nonlocal read
            for value in original(*args, **kwargs):
                read += 1
                yield value

        def durable(path, payload):
            read_at_write.append(read)

        with patch.object(sync_snapshots, "_resource_records", counted), \
                patch.object(sync_snapshots, "_write_durable", durable), \
                database.get_db() as db:
            sync_snapshots._write_resources(
                db, self.root / "staging", Path(config.RUNTIME_PATH) / "final",
                snapshot_id="snapshot", dataset_id=self.dataset_id, epoch=self.epoch,
                high_water=high_water, resources=["items"], page_size=2,
            )
        # Nine items in pages of two: each page leaves as soon as it fills.
        self.assertEqual(read_at_write, [2, 4, 6, 8, 9])

    def test_records_out_of_utf8_byte_order_are_rejected(self):
        # A UTF-16 database orders BINARY text by UTF-16 code units, which is not the snapshot
        # order; streaming must fail rather than publish pages in the wrong order.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA encoding='UTF-16le'")
        db.execute(
            """CREATE TABLE change_log(seq INTEGER PRIMARY KEY, dataset_id TEXT, epoch TEXT,
                   resource_type TEXT, resource_id TEXT, version_id TEXT, payload_json TEXT,
                   payload_sha256 TEXT, hash_algorithm TEXT)"""
        )
        payload = rfc8785.dumps({"id": 1})
        for resource_id in ("a", "Ā"):
            db.execute(
                "INSERT INTO change_log VALUES(NULL,'d','e','item',?,'v',?,?,'jcs-sha256-v1')",
                (resource_id, payload.decode(), hashlib.sha256(payload).hexdigest()),
            )
        records = sync_snapshots._resource_records(
            db, dataset_id="d", epoch="e", high_water=10, resource="items")
        with self.assertRaisesRegex(SyncSnapshotError, "byte order"):
            list(records)

    def test_backup_hash_streams_the_copied_database(self):
        self._fixture()
        target = self.root / "backup" / "source.db"
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            digest = sync_snapshots._backup_database(target)
        with target.open("rb") as handle:
            self.assertEqual(digest, hashlib.sha256(handle.read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
