# Topic assignment review ledger

Migration 27 adds append-only human decisions for immutable
`document_topic_assignments`. The assignment remains the original model or
legacy assertion. A review records whether that assertion is currently
accepted or rejected, who decided, why, when, and any raw evidence IDs used.

Reviews form a contiguous version chain per assignment. A correction appends a
new review pointing to the previous review; update and delete triggers prevent
history rewriting. Readers resolve the effective status from the latest review,
falling back to the assignment's original status only when no review exists.

The maintenance workflow is deliberately optimistic-concurrency safe:

```text
python cli.py topic-review-preview ASSIGNMENT_ID
python cli.py topic-review-coverage
python cli.py topic-review-queue none 50 all
python cli.py topic-review-queue LAST_SEQUENCE 50 TOPIC_ID
python cli.py topic-review ASSIGNMENT_ID accepted none "source checked"
python cli.py topic-review ASSIGNMENT_ID rejected topic_review_PREVIOUS "correction received"
```

Use the exact `current_review_id` returned by the preview as
`EXPECTED_PREVIOUS`; use `none` only when the preview has no review. If another
reviewer records a decision first, the stale command fails and requires a new
preview. The command runs only in the maintenance role and derives the reviewer
identity from the local OS account.

The coverage command reports accepted, rejected, candidate, superseded and
human-reviewed counts per current topic, with separate decided and human-review
basis points. The queue returns only assignments whose latest effective state
is still `candidate`, in a database-assigned monotonic queue sequence. `AFTER`
makes the read
resumable and `TOPIC_ID` narrows a review session without changing data. Queue
rows include the frozen document/topic versions, canonical URL, method,
evidence IDs and legacy evidence text. Reading the queue never creates a review
and there is intentionally no bulk-accept command.
Migration 30 backfills one immutable queue sequence per existing assignment and
assigns a higher sequence to each future assignment, so late inserts cannot
appear behind a saved cursor.

For measured evaluation rather than an unbounded queue walk, use the
[immutable sampling workflow](TOPIC_REVIEW_SAMPLING.md). It selects a fixed
number of unresolved candidates per topic and reports the human decisions
without changing this review ledger.

This ledger is the prerequisite for topic statistics. Public counts must use
the effective decision and must not count an unreviewed `candidate`. The
initial legacy import therefore remains excluded until explicitly reviewed.
Turning off later statistics/API readers leaves this ledger intact; review
history must never be rolled back by deletion.
