# Data coverage audit

Run `python cli.py db-coverage /path/to/app.db` to inventory legacy portal rows and their new-model projections. The command opens a read-only SQLite connection and does not migrate, backfill, rebuild an index, or change portal behavior. It accepts a live WAL database, but counts are from one SQLite read snapshot; for repeatable release evidence, run it on a verified database backup.

The channel breakdown answers whether the legacy database contains US-stock-channel material. It does **not** prove that each requested company, source, date or market event is covered. A separate source/window audit is needed for that claim.

`documents_with_current_version` counts linked legacy items with a current normalized version. `documents_with_raw_input` means that version references at least one raw record; it does not prove the CAS file exists or that it is a full original. `documents_with_legacy_excerpt_input` counts records reconstructed from old item fields, which must not be presented as publisher full text. `point_in_time_eligible_documents` is the stricter time/evidence flag stored on the current version.

`canonical_stories_with_candidate_event` counts old non-redirect stories mapped to a versioned **candidate** event. It is neither a confirmed event count nor an event-recall metric. Analysis publication counts include unreviewed output; no model-accuracy claim follows from them. Search and story-metric state/dirty counts describe projection freshness. Versioned report publications are shown separately from old daily reports because they need not be one-to-one.

This audit is a cutover input, not cutover authorization. Before enabling new portal reads or deploying the new schema to Windows, also verify the CAS backup/restore and portal smoke, run a complete and reconciled backfill on a frozen copy, check all affected read paths, evaluate NLP on the fixed review set, and rehearse release/rollback on the target environment.

## Mac legacy snapshot, 2026-09-21

The live Mac database was read without migration: schema version 0, 36,143 items (3,545 `ai`, 68 `robot`, 32,530 `stock`), 29,585 canonical stories and 10 old daily reports. Since its new-model tables do not exist, coverage is **unavailable**, not zero. These counts are time-sensitive because the local collector can continue writing. Windows production remains off and was not inspected or changed for this audit.
