# Topic review sampling

Migration 31 adds reproducible, immutable sampling batches for evaluating
candidate topic assignments. A batch freezes two independent high-water marks:

- the assignment queue sequence, which fixes the assignment population; and
- the review order sequence, which fixes the human decisions visible when the
  sample was created.

The selector derives a SHA-256 rank from the dataset, operator-supplied seed,
stable topic ID, assignment ID and queue sequence. It then takes at most the
configured number of unresolved candidates from each topic. The ordered member
manifest and its digest are stored with the batch. Strict database verification
reconstructs every historical sample from the two frozen high-water marks and
rejects changed members, counts, ranks or manifests.

Create and work a sample only from the maintenance role:

```text
python cli.py topic-review-sample-create 20 legacy-topic-review-v1
python cli.py topic-review-sample-report BATCH_ID
python cli.py topic-review-sample-queue BATCH_ID none 50 pending
python cli.py topic-review-preview ASSIGNMENT_ID
python cli.py topic-review ASSIGNMENT_ID accepted none "source evidence checked"
python cli.py topic-review-sample-report BATCH_ID
```

Creating the same seed and limit against the same two high-water marks is
idempotent and returns the existing batch. New assignments and later reviews do
not alter an old batch. The queue command defaults to unresolved sample members;
use `all` as its final argument to inspect decided members too.

`acceptance_bps` is the accepted share of *decided sampled candidates*. It is a
candidate precision estimate for the sampled rule output. It is not recall,
population coverage, model calibration or permission to publish. Empty and
partially reviewed batches keep this distinction explicit: acceptance is null
until at least one human decision exists, while `decided_bps` reports review
completion.

The workflow never creates review decisions and provides no bulk-accept path.
Each member must still pass through the append-only review ledger. A later
statistics publication still needs its separate admission review.

For a one-item-at-a-time browser interface, use the maintenance-only
[local topic review console](TOPIC_REVIEW_CONSOLE.md). It operates on one fixed
sample batch over loopback and preserves the same ledger and concurrency rules.

After a batch has been reviewed, use the
[sample evaluation gate](TOPIC_REVIEW_SAMPLE_GATE.md) to freeze the measured
result and the exact quality thresholds used by the evaluator.
