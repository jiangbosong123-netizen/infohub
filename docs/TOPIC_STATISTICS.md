# Auditable topic statistics

Migration 28 creates the publication foundation for the `document_count`,
`event_count`, and `counted_at` fields required by the v1 topic contract. It
does not calculate or publish counts yet.

A build freezes the assignment and event policy versions. Each topic receives
an immutable statistics version with an input-manifest hash. Every counted
document or event is retained as an ordered member containing its stable ID,
exact version ID, provenance JSON, and member hash. Counts therefore remain
inspectable after document, event, or review state moves forward.

The current read projection changes only when an entire build is `ready` and
an append-only publication version points to it. Publication versions form a
contiguous chain, so changing or rolling back the current pointer does not erase
which builds were previously exposed.
Strict database verification requires a ready build to contain exactly one
statistics row for every current topic version and requires its document/event
counts to equal the retained member rows. A partial build cannot become a
plausible public zero.

The dirty queue records changes to topic catalog versions, new topic
assignments, appended human reviews, and event current-version/status changes.
Migration queues every existing topic for the first build. Dirty rows are
disposable scheduling state; builds, statistics versions, and members are
append-only history.

The next implementation step is the resumable builder. Its document policy
will count a document once only when at least one assignment's effective status
is `accepted`; unreviewed legacy candidates remain excluded. Its event policy
will count only current stable public events and will retain the exact event
version as a member. Until the first complete build is published, the topic API
must stay unavailable.
