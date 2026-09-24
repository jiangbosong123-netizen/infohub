# Topic review sample evaluation gate

Migration 32 adds an append-only human evaluation ledger for immutable topic
review samples. Sampling defines what is measured; this gate records whether
the completed evidence meets an explicit policy. The decision freezes the
sample manifest, aggregate counts, per-topic counts, review completion and
candidate acceptance rates in a hashed metrics document. The evaluation also
freezes the monotonic review sequence used for those metrics, so strict database
verification can reconstruct a historical decision after later reviews arrive.

An approval must satisfy all four stored thresholds:

- overall review completion;
- review completion for every sampled topic;
- overall acceptance among decided candidates; and
- acceptance among decided candidates for every sampled topic.

Each threshold is 1–10,000 basis points. A topic with no human decision has no
acceptance rate and therefore cannot pass. This prevents a strong large topic
from hiding an unreviewed or weak small topic. A rejection can be recorded
before completion to preserve an explicit operational decision.

Use optimistic concurrency from the maintenance role:

```text
python cli.py topic-sample-gate-preview BATCH_ID
python cli.py topic-sample-gate-review BATCH_ID rejected none 10000 10000 9000 8500 "review incomplete"
python cli.py topic-sample-gate-review BATCH_ID approved PREVIOUS_ID 10000 10000 9000 8500 "fixed sample meets policy"
```

Corrections append another evaluation and must name the current evaluation ID.
Updates and deletes are blocked. If a reviewer later corrects any assignment
decision, the current metrics digest changes and the old approval becomes
stale; it cannot be used as current proof.

Acceptance is the precision estimate of the sampled candidate assertions. This
gate does not measure recall, does not approve a statistics publication, and
does not activate the API or portal. Wiring an approved sample into publication
admission is a separate change so it can be reviewed and rolled back safely.
