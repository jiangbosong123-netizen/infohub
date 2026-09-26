# Event match review sampling

Schema 36 adds immutable, reproducible, stratified sampling for pending event match
decisions. It provides a bounded human quality-review workload before legacy candidate links
can support an Event API release decision. It does not bulk accept any match.

Each batch freezes two monotonically assigned cutoffs:

- the highest event match decision queue sequence;
- the highest append-only human review sequence.

The candidate population is every decision at or before the decision cutoff whose effective
status at the review cutoff is `pending`. Candidates are stratified by machine decision,
matcher version and score band (`missing`, `<0.50`, `0.50–0.779999`, `0.78–0.899999`,
or `>=0.90`). Within each stratum, SHA-256 ranking over the dataset identity, caller-supplied
seed, decision identity, stratum and queue sequence selects at most the configured limit.
The ordered member manifest and its digest are stored immutably.

Operators create and inspect a batch with the maintenance role:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-review-sample-create 50 event-quality-v1
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-review-sample-report BATCH_ID
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-review-sample-queue BATCH_ID none 50 pending
```

Review one returned decision with `event-match-review-preview` followed by
`event-match-review`. The report exposes pending, accepted and rejected counts, decided basis
points and acceptance basis points overall and per stratum. `all` may replace `pending` to
audit completed members. Reports can also be reproduced at an earlier review cutoff.

The database verifier recomputes every frozen population, selection rank and manifest hash.
It also verifies that every event match review has exactly one immutable global order row.
A later decision or review does not change an existing batch.

This unit records sample observations but does not define pass thresholds. The next unit is an
append-only event sample evaluation gate. A subsequent dataset release decision must bind the
approved sample before the Event API can be enabled. Windows and production remain unchanged.
