# Event read API

The Event API exposes only the exact manifest in the current valid `event-release-v1`
approval. It never reads candidate events directly and it fails closed when the release,
sample evaluation, match-decision population, event version, evidence, or admission has changed.

## Endpoints and authorization

- `GET /api/v1/events` requires `read:events`.
- `GET /api/v1/events/{id}` requires `read:events`.
- `GET /api/v1/events/{id}/evidence` requires the stronger `read:evidence` scope.

The list accepts `limit`, `cursor`, `q`, `type`, `state`, `entity_id`, and `topic_id`.
Unknown, duplicate, empty, non-canonical, or out-of-range parameters return the stable
`invalid_parameter` envelope. Cursors are signed for one API key, authorization version,
dataset epoch, release review, and exact normalized filter set. Pagination is ordered by stable
event ID and has `consistency=release`.

Responses identify the release review and manifest hash. Each event includes its immutable event
version, public admission proof, timestamps, public knowledge state, entity IDs, topic identity
and version pairs, and typed facts. Internal candidate status and machine match scores are not
presented as public knowledge. A missing ID outside the approved manifest returns 404 even when an
internal candidate with that ID exists.

Details return a principal-bound ETag over the dataset epoch, release proof, admission proof and
event payload. `If-None-Match` supports strong and weak comparison and returns 304 without a body.

## Operational switch

`INFOHUB_API_EVENTS_ENABLED` defaults to `false` independently of catalog and item APIs. Enabling
the switch does not bypass the release gate: without a current approved release, both endpoints
return `503 not_ready`. Windows production remains unchanged until the operator intentionally
deploys the completed release and enables the flag.

Evidence rows use a separately authorized contract documented in
[`EVENT_EVIDENCE_API.md`](EVENT_EVIDENCE_API.md). This keeps raw provenance disclosure and
pagination out of the initial event representation while every released event remains traceable
to immutable document versions and raw-record digests.
