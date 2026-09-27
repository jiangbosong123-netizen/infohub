# Reliable-sync snapshot foundation

Schema 39 establishes the durable ownership and immutable-manifest boundary required by P19. It
does not expose a snapshot route or build snapshot payloads yet.

## Lifecycle

`sync_snapshot_requests` binds one request to the dataset/epoch, consumer, exact API key,
authorization version, idempotency key, selected resource allowlist, complete scope allowlist and
projection scope. The only state transitions are `pending -> running -> ready|failed` and
`pending -> failed`. Expiry is evaluated from the recorded `expires_at`; it is not a destructive
state transition.

The resource and scope manifests must be nonempty, unique allowlists. Reusing an idempotency key
under the same key and authorization version cannot create another request. Authorization changes
therefore create a new namespace and cannot inherit an older snapshot silently.

## Immutable result

While a request is running, a worker may append resource and page manifests. A resource fixes its
record count, page count and RFC 8785/JCS stream hash. A page fixes the first and last resource ID,
record count, private payload reference, payload SHA-256 and byte size. Rows cannot be updated or
deleted.

The final `sync_snapshots` row can be inserted only after all declared counts match all page rows,
and only when its dataset, owner, authorization, expiry and checkpoint agree with the running
request. Its checkpoint must have the same epoch and high-water. A request can become `ready` only
after that complete immutable result exists. Partial work can never be advertised as ready.

The schema stores private page references for the future worker; public APIs must return signed
page cursors and verified payloads, never these internal references. Snapshot payload generation,
the 24-hour cleanup policy, request quotas and `/changes` retention are separate P19 units.

## Operations

Migration 39 only creates empty tables, indexes and triggers. Existing `change_log`, dataset
identity, API consumers and knowledge checkpoints are unchanged. No route or worker is enabled,
and Windows production remains unchanged until the full sync path passes consumer and restore
drills.
