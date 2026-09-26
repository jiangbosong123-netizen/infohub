# Event evidence read API

`GET /api/v1/events/{id}/evidence` exposes the provenance of the exact event version frozen in
the current valid `event-release-v1` manifest. It requires `read:evidence`; a key with only
`read:events` cannot use it. The endpoint shares the default-off `INFOHUB_API_EVENTS_ENABLED`
switch and fails closed when the release, admission, sample or match population is stale.

## Contract

The response identifies the dataset epoch, release review, event version and admission proof.
Each evidence row contains:

- evidence ID, role (`supports`, `contradicts`, or `context`), optional fact ID and availability;
- the exact immutable document version, its public URL, source, publisher, publication time and
  raw-input role;
- the exact raw-record ID, source, observation and ingestion times, media type, payload SHA-256,
  payload kind, truncation state and byte size.

The API deliberately omits raw payload contents and storage references, request and redirect
URLs, HTTP headers, external source identifiers and retention controls. Public document URLs are
normalized and have userinfo, fragments and common secret query parameters removed.

The list accepts only `limit`, `cursor`, `role`, and `fact_id`. Signed cursors bind one API key,
authorization version, dataset epoch, release review, event ID and exact filter set. Ordering is
stable by evidence ID and `consistency=release`. An event outside the current release returns 404.
Unknown, duplicate, empty, non-canonical or out-of-range parameters use the stable v1 error
envelope.

## Operations

No schema migration is required. Enabling the Event API switch does not weaken the separate
scope check. Windows production remains unchanged until the completed release is deliberately
deployed and the switch is enabled.
