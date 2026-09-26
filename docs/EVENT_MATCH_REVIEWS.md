# Event match reviews

Schema 35 adds a stable review queue and an append-only human decision ledger for immutable
`match_decisions`. The matcher output remains unchanged: its original decision, score,
features, reason, model version and input versions are machine provenance. A reviewer accepts
or rejects that output by appending an `event_match_reviews` row; human judgment never rewrites
the machine record.

`event_match_review_queue.sequence` is assigned once. Migration 35 backfills existing match
decisions in SQLite row order, and a database trigger queues every later decision. Queue rows
cannot be updated or deleted. This gives sampling and operations a stable cursor that does not
change when scores, timestamps or review state are queried.

Each review must cite at least one raw record that is an input to the decision's document
version. A `candidate_link` review has a stricter rule: every cited raw record must also be
attached as evidence to one of the candidate event versions frozen in the match decision.
Reviews form a contiguous immutable chain and use the previously observed review ID as an
optimistic concurrency check. The effective status is the latest human decision when one
exists, otherwise the matcher's original `review_status`.

Maintenance operators use these commands:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-match-review-queue none 50 pending
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-match-review-preview DECISION_ID
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-match-review \
  DECISION_ID accepted EXPECTED_REVIEW_ID RAW_RECORD_ID[,RAW_RECORD_ID...] \
  "The cited source supports this event link"
```

Use `none` as the expected review ID only for the first review. A stale writer fails and must
refresh the preview. `all` may replace `pending` when reading the queue for audit purposes.

Event publication admission reads the effective status. Consequently, accepting a pending
link may make an event eligible for a fresh admission review, while rejecting a link prevents
publication. Either change also changes the admission metrics hash, so an older event admission
becomes stale and fails closed until it is reviewed again.

This unit does not declare the old 25,490 pending legacy projections correct and does not bulk
accept them. Reproducible stratified sampling is defined in
[`EVENT_REVIEW_SAMPLING.md`](EVENT_REVIEW_SAMPLING.md); quality thresholds and a dataset-level
Event API release gate remain separate work. Windows and production stay unchanged.
