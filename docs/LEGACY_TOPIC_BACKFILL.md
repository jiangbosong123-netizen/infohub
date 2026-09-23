# Legacy topic assignment backfill

Migration 26 adds a one-time, resumable bridge from mutable legacy
`item_topics` rows to append-only `document_topic_assignments`. It does not
change the portal read path and it does not promote a legacy rule match to a
reviewed fact.

## Semantics

The importer requires the P07 document backfill to be complete. At first run it
holds an immediate SQLite transaction and freezes every `item_topics` row at or
below the document backfill cutoff. Each frozen row records:

- the exact legacy evidence string and its SHA-256;
- the mapped immutable `document_version_id`;
- the stable `topic_version_id` resolved through the slug alias catalog; and
- the real capture time.

The source and resolved manifest each receive an ordered SHA-256. Missing
document mappings or topic versions abort the entire freeze, so a partial
manifest is never accepted. Later changes to the mutable legacy projection do
not rewrite the frozen evidence.

Every imported assertion uses `method=legacy_projection`,
`method_version=legacy-item-topics-v1`, `status=candidate`, and an empty
`evidence_ids_json`. The old evidence is a list of rule hits, not a raw-record
evidence ID. Treating it as a formal evidence reference or an accepted human
decision would invent provenance. `available_at` is the import time; it is not
presented as the unknown historical classification time.

## Operation and recovery

Run only from a maintenance process after a verified backup:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py legacy-topic-backfill 250
```

Each batch inserts assertions and mapping rows and advances the composite
`(item_id, topic_slug)` cursor in one transaction. A crash before commit leaves
both data and cursor unchanged. Re-running resumes the frozen manifest;
completed runs are no-ops. The snapshot, mappings, stable assertions, and
manifest identity are protected by immutability triggers.

On failure, stop the importer and inspect `legacy_topic_backfill_state`.
The existing portal remains on legacy tables. Recovery is to correct the
missing prerequisite and rerun the command. Do not delete or edit frozen rows.
The new tables can remain unused if rollout is abandoned; no old table is
rewritten or removed.

## Publication boundary

These candidate rows are sufficient for audited migration coverage, but not
for public topic counts. Human decisions are appended through the
[topic assignment review ledger](TOPIC_ASSIGNMENT_REVIEWS.md). A later
projection must resolve each assertion's latest review, define which results
contribute to a count, and publish an explicit `counted_at`. Until that
projection is reviewed, the topic API must remain unavailable rather than
report misleading zeroes.
