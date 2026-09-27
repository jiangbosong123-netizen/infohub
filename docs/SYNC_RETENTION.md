# Reliable sync retention operations

This document describes the implemented P19e retention boundary. It applies to the private files
created by the reliable-sync snapshot worker. It does not authorize deletion of immutable database
history.

## Implemented policy

- A snapshot expires at the immutable `sync_snapshots.expires_at` value copied from its request.
- The snapshot worker currently issues research snapshots with a 24-hour lifetime.
- The daily `maintenance:prune` job removes an expired snapshot's private directory only after its
  derived directory name, path boundary and `manifest.json` SHA-256 all match the immutable ledger.
- A symlinked snapshot root or manifest, a path outside `RUNTIME_PATH/sync-snapshots`, a missing
  manifest or a hash mismatch is refused. Refusal fails the maintenance job so operators can
  investigate rather than silently discarding questionable files.
- Already-missing expired directories are idempotently reported as `missing`.
- `sync_snapshot_requests`, `sync_snapshots`, `sync_snapshot_resources` and
  `sync_snapshot_pages` are never deleted or updated by retention. They remain an audit record of
  what was generated, for whom, at which dataset epoch and high-water.

The maintenance command previews by default:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py sync-retention
INFOHUB_PROCESS_ROLE=maintenance python cli.py sync-retention --apply
```

Both forms first require a current, verified database. The JSON report separates candidates,
eligible directories, deleted directories, already-missing directories, refusals and unexpired
ledger rows. `--apply` is the only form that deletes files.

## Change history retention

Reliable-sync cursors can remain valid for at most 90 days. The current implementation retains
`change_log` indefinitely, which satisfies the minimum history window conservatively. It does not
physically prune change rows because publication ledgers reference `change_log.seq` and the formal
audit history must remain resolvable. A future compaction design must first preserve publication
foreign keys, tombstones, cursor expiry semantics and audit reconstruction; the file cleanup in
this unit must not be extended into database deletion without a separate reviewed migration.

## Failure and recovery

- A refused directory remains in place. Correct the filesystem discrepancy, then rerun the dry
  run and apply command.
- A maintenance job failure is retried by the durable worker according to its existing retry
  policy. File deletion is safe to retry because missing directories are accepted.
- Losing an expired page file does not change the immutable ledger. Clients receive `410` after
  expiry and must request a new snapshot; they cannot use the old page endpoint as an archive.
- Database backup and restore procedures continue to protect ledgers. Snapshot page files are
  short-lived transport artifacts and are regenerated through a new request rather than restored.

## Production status

This unit changes code and local validation only. Windows production and automatic deployment
remain unchanged until the wider database and synchronization build-out is accepted.
