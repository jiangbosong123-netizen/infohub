# Event dataset release gate

Schema 38 adds the final dataset-level gate before events can become an API-visible
publication set. A release review freezes one current dataset epoch, one current approved
event-review sample evaluation, and the exact list of admitted event versions. It does not
enable the Event API or change any event, match, evidence, or admission record.

## Fixed release policy

`event-release-v1` is code-owned policy. An approval requires all of the following:

- the sample belongs to the current dataset and is its latest approved evaluation;
- the sample metrics have not changed since evaluation;
- the sample's decision cutoff equals the current match-decision queue high-water mark;
- overall and every-stratum review completion are 100%;
- overall and every-stratum decision acceptance are at least 90%;
- at least one event has a current, valid, non-rejected admission;
- every event in the manifest still points to its current event version and current admission.

The minimums are rechecked from constants during strict database verification. They cannot be
weakened by values stored inside a release row. A rejected review may always be appended so an
operator can preserve why a dataset was withheld.

## Frozen proof

Each release stores the dataset ID and epoch, sample evaluation ID and metrics hash, a canonical
event manifest and hash, complete release metrics and hash, reviewer identity, reason, timestamp,
and the previous review ID. Manifest entries bind the event ID, event version ID, admission review
ID and version, public state, and admission metrics hash. Reviews form a contiguous append-only
chain; update and delete are blocked by database triggers.

Any later sampled-match correction, newly queued match decision, event admission review, event
version change, or event population change makes the approved release stale. Consumers must call
`approved_event_release`; it fails closed until a new sample/evaluation or release review is
recorded as required.

## Maintenance workflow

Run these commands only on a current, strictly verified database using the maintenance role:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-release-preview SAMPLE_EVALUATION_ID
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-release-review \
  SAMPLE_EVALUATION_ID approved EXPECTED_RELEASE_REVIEW_ID \
  "Current sample and admitted event manifest reviewed"
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-release-status
```

Use `none` for the expected review ID only when creating the first review in the current dataset
epoch. Preview first and copy the currently observed ID for every later decision. Approval is a
transactional write; status is read-only and fails when the latest approval has become stale.

This gate prepares a trustworthy event publication set. Event API response models, authorization,
pagination, and portal integration remain separate later units. Windows and production are not
changed by this migration.
