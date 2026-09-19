# Curation search index build (P13g)

Migration 17 creates an empty, versioned search projection. This change adds a
bounded builder; the portal still queries its previous search path. No model or
network provider is called. The projection stores each item's original title,
the title and summary currently displayed by the curation read projection, and
the active document/publication IDs used to produce them. An item without a
current publication retains its legacy display fallback. Rejected or invalid
current publications use the same clearing rules as the portal.

## Operation

After a verified backup and migration, run `INFOHUB_PROCESS_ROLE=maintenance
python cli.py curation-search-advance 500` repeatedly until the JSON report says
`status: ready` and `dirty_remaining: 0`. Each call commits at most 500 items.
`scanned` advances the initial item-ID cursor; `refreshed` replays changes that
arrived after a scan (or after readiness). An interrupted call rolls back its
index writes, queue acknowledgements, and cursor together. A repeated call is
safe; unchanged rows do not rewrite FTS entries. Once ready, continue calling
this command periodically until a worker-owned refresh loop is delivered. This
manual builder is not scheduled by the web process.

Readiness is a snapshot at the end of a transaction, not a promise that no
new item has arrived since then. Consumers should check both readiness and the
dirty queue before treating the new index as current. The separate search
cutover PR will define its fallback when the projection is stale or rebuilding.
If rebuilding must be abandoned, the entire index and state are derived and
can be reset in a reviewed maintenance procedure; do not manually edit the
cursor alone.

## Boundaries and verification

The index is keyed by legacy item ID and does not alter source items, document
versions, publications, or existing search. It contains no event/entity text
yet. FTS5 trigram limitations on terms shorter than three characters and
search query/pagination semantics remain for the search cutover PR. Verify
`indexed_count = COUNT(items)` and `dirty_remaining = 0` after a full build,
and run the FTS external-content integrity check. The Mac copy rehearsal is
recorded in `docs/evidence/p13g-search-build-rehearsal.json`; Windows production
has not been changed.
