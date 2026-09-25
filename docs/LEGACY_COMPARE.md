# Comparing legacy rows after a rehearsal

Run `python cli.py db-legacy-compare BEFORE.db AFTER.db` after an isolated migration or backfill. Both inputs are opened read-only. The command hashes every original column of each legacy table that existed in `BEFORE.db`; columns added by newer migrations are deliberately excluded. It reports row counts and SHA-256 digests only, never titles, URLs or report content. A missing original table/column, changed row count or changed original value makes the command exit nonzero.

Use a frozen SQLite backup as `BEFORE.db`, not a live WAL file that may change between reads. If a migration deliberately changes a legacy field, this gate must be revisited with an explicit, reviewed data transformation; do not dismiss a mismatch merely because the new portal still renders.

The comparison covers the legacy tables `companies`, `sources`, `items`, `item_companies`, `item_discoveries`, `topics`, `item_topics`, `stories`, `story_items`, `daily_reports`, and `fetch_log` when present in the baseline. It does not verify newer tables, foreign keys or CAS bytes; pair it with `db-verify`, `raw-verify` and `db-coverage` on the isolated output.
