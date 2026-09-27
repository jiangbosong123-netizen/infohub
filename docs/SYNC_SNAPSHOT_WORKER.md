# Reliable-sync snapshot worker

P19b turns a schema-39 snapshot request into immutable page files and a closed database manifest.
It does not expose an HTTP route or enable production synchronization.

## Consistency boundary

The worker first validates the durable job lease, current dataset epoch, exact API key,
authorization version, frozen scopes and expiry. It then moves the request to `running` and uses
the SQLite backup API. The dataset epoch and change high-water are read from the completed backup,
so commits arriving afterward cannot leak into the snapshot.

The worker materializes the latest published `change_log` record for each requested resource ID at
or below that high-water. This is the public publication projection rather than a direct dump of
internal tables. Delete, withdraw, merge and split tombstones remain visible when they are the
latest published state.

## Canonical output

Every record has `resource_type`, `resource_id`, `version_id` and `payload`. Records are ordered by
the UTF-8 bytes of the stable resource ID. Page files contain an RFC 8785/JCS canonical data array.
Their SHA-256 covers that array exactly. A resource hash covers each canonical record followed by
one LF; the empty resource hash is SHA-256 of zero bytes.

The immutable manifest fixes:

- dataset ID, epoch and backup-derived high-water;
- an observed knowledge checkpoint with explicit `unknown` clock status;
- key authorization version, frozen scopes and projection scope;
- backup digest, source schema version, total resource/record counts and per-resource hashes;
- the 24-hour request expiry supplied by the future API layer.

Files are written and flushed in a staging directory, atomically renamed, then read back and
verified before database publication. Resource rows, page rows, checkpoint, final snapshot and the
`ready` transition commit together. A lost/expired job lease, epoch change or authorization change
fails closed before publication. Retried work returns an existing complete result or rebuilds from
scratch when no immutable rows were published.

## Current boundary

Only `research` projection is buildable. `selected` fails explicitly because no approved selection
policy and selection-reason contract exist yet; returning research data under that label would be
incorrect. P19c will create/status/download snapshots through authenticated APIs. P19d will add the
change stream and signed resume cursor. Retention cleanup and end-to-end restore drills remain
later P19 units. Windows production is unchanged.

The isolated current-data rehearsal and supported-runtime checks are recorded in
[`p19b-sync-snapshot-worker.json`](evidence/p19b-sync-snapshot-worker.json). The legacy copy has no
published `change_log` records, so its correct public-projection snapshot has nine empty resources;
the worker does not bypass publication gates by exporting legacy portal tables directly.
