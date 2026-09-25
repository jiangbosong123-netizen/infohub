# Topic statistics publication admission

Migration 29 separates a technically complete statistics build from a
publication approved for external use. Every admission decision is an
append-only human review of one immutable publication. Corrections append a new
version; database triggers reject update, delete, skipped versions, and stale
predecessors. Migration 33 retires coverage-only approval for serving and makes
new approvals use policy `sample-gated-v2`.

The preview freezes and hashes these release metrics:

- total assignments and their latest effective accepted, rejected, candidate,
  and superseded states;
- assignments with a human review, decided-assignment basis points, and human
  review basis points;
- catalog topics, topics with an accepted document, and stable public events;
- the exact published topic, document-member, and event-member totals;
- pending dirty topics.

An approval records the minimum decided-assignment coverage used by the
reviewer. The write fails when actual coverage is lower or dirty topics exist.
A publication containing no document or event member also fails unless the
reviewer explicitly sets `allow_zero_members=true`; this exception is stored in
the immutable decision instead of being inferred later.

Under `sample-gated-v2`, approval must also reference the current approved
topic-review sample evaluation. The sample must belong to the same dataset,
cover the current assignment queue high-water mark, include the current review
high-water mark, and retain the same metrics digest. A new assignment, a review
correction, a replaced sample evaluation, or changed statistics metrics makes
the admission fail closed.

Use the exact current publication and optimistic-concurrency predecessor:

```text
python cli.py topic-admission-preview PUBLICATION_ID
python cli.py topic-admission-review PUBLICATION_ID rejected none 9000 false none "candidate backlog is too large"
python cli.py topic-admission-review PUBLICATION_ID approved PREVIOUS_REVIEW_ID 9500 false SAMPLE_EVALUATION_ID "coverage and sampled quality accepted"
```

`MIN_BPS=9500` means 95% of assignments must have an effective accepted or
rejected decision. It does not claim model accuracy. Human review coverage is
reported separately, because an assignment originally emitted as accepted or
rejected can be decided without a human review.

Rows written before migration 33 keep `policy_version=coverage-v1` for accurate
history. They are never rewritten, but they no longer satisfy the API or portal
read gate. Operators must append a new admission after completing and approving
a current fixed sample.

Creating the table, previewing a publication, or recording a rejection does
not enable the portal. The typed topic API requires the latest decision to be
approved and its current metrics hash to equal the frozen review hash. It
returns `503 not_ready` after a rejection, publication replacement, or input
change. Production must not approve the current legacy-derived zero
publication: its candidate backlog remains effectively undecided.
