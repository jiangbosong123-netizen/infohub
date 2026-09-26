# Event publication admission

Schema 34 separates event clustering from public truth status. An `events` row and its
current `event_versions` row are internal identities and claims; neither is permission to
serve the event to API consumers. `event_admission_reviews` is the append-only human gate
that may classify one immutable current version as `reported`, `corroborated`, or
`confirmed`. No review, a latest `rejected` review, or a stale review means no publication.

This distinction is deliberate:

- `candidate` is a clustering state and remains internal;
- `reported` means at least one reviewed document/evidence pair supports the event;
- `corroborated` additionally requires two supporting documents from two independently
  verified original publishers;
- `confirmed` requires `confirmed_by_primary` knowledge plus verified original evidence
  from a publisher whose organization identity is one of the event's primary entities.

All public states require the current event version, a valid version hash, current active
entity/topic references, version-pinned raw evidence, direct support for every structured
fact, at least one accepted document-event match, and no pending or rejected current match.
Merged, split, and retracted identities cannot be admitted. These rules intentionally reject
the legacy story projection, whose events are `candidate`/`unknown`, whose links are pending,
and whose evidence is contextual rather than supporting.

Match status is resolved from the latest append-only human match review when one exists;
otherwise it remains the immutable matcher's original `review_status`. See
[`EVENT_MATCH_REVIEWS.md`](EVENT_MATCH_REVIEWS.md). A later match review changes the live
admission metrics hash and therefore invalidates an older publication decision.

Maintenance operators first inspect the frozen metrics, then append a decision using the
review ID they observed:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-admission-preview EVENT_VERSION_ID
INFOHUB_PROCESS_ROLE=maintenance python cli.py event-admission-review \
  EVENT_VERSION_ID reported EXPECTED_REVIEW_ID "Evidence and match reviewed"
```

Use `none` for the expected review ID only when no review exists. Concurrent or stale
writers fail instead of overwriting a decision. Review rows, their evidence metrics, and
their hashes are immutable. Any later evidence/link change changes the live metrics hash and
invalidates the prior admission until a new review is appended. A new event version also
requires its own review.

This foundation does not expose `GET /api/v1/events`. A fixed event-quality sample and a
dataset-level release gate are still required before the API can be connected. Windows and
production remain unchanged.
