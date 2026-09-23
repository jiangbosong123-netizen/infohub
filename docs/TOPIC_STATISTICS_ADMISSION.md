# Topic statistics publication admission

Migration 29 separates a technically complete statistics build from a
publication approved for external use. Every admission decision is an
append-only human review of one immutable publication. Corrections append a new
version; database triggers reject update, delete, skipped versions, and stale
predecessors.

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

Use the exact current publication and optimistic-concurrency predecessor:

```text
python cli.py topic-admission-preview PUBLICATION_ID
python cli.py topic-admission-review PUBLICATION_ID rejected none 9000 false "candidate backlog is too large"
python cli.py topic-admission-review PUBLICATION_ID approved PREVIOUS_REVIEW_ID 9500 false "coverage accepted"
```

`MIN_BPS=9500` means 95% of assignments must have an effective accepted or
rejected decision. It does not claim model accuracy. Human review coverage is
reported separately, because an assignment originally emitted as accepted or
rejected can be decided without a human review.

Creating the table, previewing a publication, or recording a rejection does
not enable the API or portal. The next change makes the API require the latest
admission decision to be approved. Production must not approve the current
legacy-derived zero publication: its candidate backlog remains effectively
undecided.
