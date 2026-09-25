# Local topic review console

The topic review console is a maintenance-only interface for reviewing one
member of a frozen topic sample at a time. It is a separate FastAPI application;
the production portal never imports or mounts it.

Start it on the machine that has the review database:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py topic-review-console BATCH_ID
```

The command verifies the current database and the requested batch before
starting. It binds to `127.0.0.1:8011`, fixes the reviewer identity to the local
operating-system user, and creates a random in-memory CSRF token. The bind host
is not configurable. Open the printed loopback URL in a browser on that same
machine.

Each page contains at most one unresolved sample member. The reviewer must read
the title, source, assigned topic, method and available evidence, enter a reason,
then explicitly accept or reject that one assertion. The form cannot select the
reviewer, batch or assignment. There is no bulk decision endpoint.

Every submission still goes through the append-only topic assignment review
ledger. The form carries the review ID observed when the page was rendered; if
another reviewer changes that assertion first, the stale submission receives a
conflict and cannot overwrite the newer decision. Assignments outside the fixed
batch are rejected.

The local server disables API documentation and sends no-store, frame denial,
strict content type and restrictive content-security headers. Only valid HTTP(S)
source links are rendered. Loopback binding limits network exposure, while the
CSRF token prevents an unrelated browser page from silently posting a decision
to the local service.

Stopping the console does not affect the production portal or worker. Completing
the page queue also does not approve topic statistics: the operator must create
and approve the separate sample evaluation, then run the statistics admission
workflow described in [Topic statistics admission](TOPIC_STATISTICS_ADMISSION.md).

## Recovery and audit

- A browser refresh is safe; the next unresolved member is derived from the
  immutable sample and current review ledger.
- A validation or concurrency error writes no review row.
- Restarting the command creates a new CSRF token and resumes the same batch.
- Review history remains inspectable through `topic-review-preview` and the
  immutable database ledger.
- The console intentionally has no remote-access mode. A future shared reviewer
  service would require explicit identity, authentication, authorization and
  audit design rather than widening this local tool.
