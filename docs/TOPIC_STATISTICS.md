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

`advance_topic_statistics()` implements the bounded, resumable builder. Its
document policy counts a stable document identity once when at least one
assignment's effective status is `accepted`; the latest human review overrides
the immutable original decision, and unreviewed legacy candidates remain
excluded. If several accepted assertions refer to the same document and topic,
the member manifest keeps all of them while the count remains one.

The event policy counts only the current version of `active` and `resolved`
events. A reference to any immutable version of the topic maps to the same
stable topic identity. Candidate, retracted, merged, and split events are not
public members.

Each call commits at most 250 topics and advances a stable-ID cursor in the same
transaction as its result rows. If an already-scanned topic becomes dirty, or
the topic catalog/version pointers change before completion, that build is
marked `failed` and the next call starts a fresh build. The old public pointer
does not move. A complete unchanged build appends one publication and moves the
pointer atomically; repeated calls with no dirty inputs return that publication
without creating another one.

The topic API must stay unavailable until the first complete build is
published. Scheduler integration and the API read projection remain separate
reviewable changes.
