# Event review sample evaluation gate

Schema 37 adds an append-only evaluation ledger for immutable event match review samples.
An evaluation freezes the exact review cutoff, sample manifest, overall metrics, per-stratum
metrics, declared thresholds and a SHA-256 digest. It never changes individual match reviews
and it does not enable the Event API.

An approval requires a non-empty sample and all four declared checks to pass:

- overall review completion;
- every stratum's review completion;
- overall match acceptance;
- every stratum's match acceptance.

All thresholds use basis points from 1 through 10,000. This prevents a strong high-score
stratum from concealing an inaccurate decision type, matcher version or score band. Rejection
can always be recorded, preserving why a batch was unsuitable or incomplete. Evaluations form
a contiguous immutable chain and require the evaluator to supply the previously observed
evaluation ID.

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-sample-gate-preview BATCH_ID
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-sample-gate-review \
  BATCH_ID approved EXPECTED_EVALUATION_ID \
  10000 10000 9000 9000 "All sampled strata meet the declared policy"
```

The example requires 100% review completion and 90% acceptance overall and in every stratum;
it is an example, not a silently installed production threshold. The future dataset release
policy must state its required minimums and reject approvals created with weaker thresholds.

If a reviewer later corrects any sampled match, live metrics change and the current approval
becomes stale. A consumer of `approved_sample_evaluation` fails closed until a new evaluation
is appended. Strict database verification reconstructs every historical evaluation at its
stored cutoff and rejects an approval that did not satisfy its recorded thresholds.

The dataset-level release gate now binds an approved sample, current decision-population coverage,
event admission state and the exact release manifest; see
[`EVENT_DATASET_RELEASE.md`](EVENT_DATASET_RELEASE.md). Windows and production remain unchanged.
