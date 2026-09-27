# Reliable-sync reference consumer

`examples/reliable_sync_consumer.py` is a small downstream implementation of the P19
snapshot-plus-changes contract. It demonstrates how a future larger system can consume InfoHub
without opening, copying or writing InfoHub's production SQLite database.

## Boundary

The example owns a separate SQLite file containing only its materialized subscription and opaque
resume cursor. HTTP transport, bearer-token storage and retry timing remain the caller's
responsibility. The token is never stored in the consumer database. This keeps the example useful
for a Mac test, a Windows-side service or another system without putting downstream state inside
InfoHub.

Only `research` snapshots are accepted. `selected` remains fail closed because no approved,
versioned selection policy has passed the required quality gates. The example accepts the current
v1/1.0.0 response contract and rejects an unexpected protocol version or hash algorithm.

## Initial snapshot

The caller performs the network loop:

1. `POST /api/v1/sync/snapshots` with an idempotency key and the full resource set.
2. Poll `GET /api/v1/sync/snapshots/{id}` with bounded backoff until `ready` or `failed`.
3. Follow every resource's signed page cursor until `next_cursor` is null.
4. Pass the ready status and downloaded page responses to
   `ReliableSyncConsumer.import_snapshot(...)`.

Before changing visible local data, the consumer checks:

- API, schema, dataset, epoch, snapshot, scope and checksum-algorithm identity;
- every page's JCS SHA-256 and complete cursor chain;
- strict UTF-8 byte ordering and uniqueness of resource IDs;
- each record's resource type, version and payload shape;
- each manifest count and the resource-stream JCS SHA-256.

Only after every resource verifies does one SQLite transaction replace the materialized objects and
record `snapshot_id`, dataset/epoch, high-water, resource set, opaque cursor and
`consumer_received_at`. A bad or incomplete resource leaves the prior projection untouched.

## Incremental changes

The caller requests `GET /api/v1/changes` using only the cursor returned by the consumer state. One
call to `apply_changes(...)` verifies response identity, monotonically increasing seq values,
subscription membership and each payload hash, then commits object changes and `next_cursor` in the
same SQLite transaction. A crash can therefore leave either the old objects and old cursor or the
new objects and new cursor, never a mixed state.

Create, update, withdraw, merge, split and delete operations all upsert their supplied payload. The
example deliberately retains tombstones and replacement information instead of physically deleting
an object and losing the reason it disappeared. Sequence gaps are allowed because changes for
unsubscribed resources are invisible; an empty final batch still advances to the server high-water.

On HTTP 409 `epoch_changed`, HTTP 410 `cursor_expired`/`snapshot_expired`, a response epoch mismatch
or high-water regression, the caller discards the incremental attempt and creates a fresh snapshot.
It must not silently continue from the current time. Authentication failure requires credential
repair; 429 and retryable 503 responses use bounded backoff while preserving the current cursor.

## Validation status

The local integration drill runs against the actual FastAPI routes and durable snapshot worker. It
covers a two-page snapshot, an unsubscribed sequence gap, update, delete, withdraw, merge, split, an
empty scan, payload tampering, atomic rollback and epoch mismatch. This is a synthetic downstream
contract drill. A real external consumer, Windows restart and restore drill remain P23 production
acceptance work.
