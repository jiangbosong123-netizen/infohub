# Document and raw-evidence read API

This contract exposes redacted provenance for immutable document inputs. It never serves the
captured payload itself. Both routes are controlled by the independent, default-off
`INFOHUB_API_EVIDENCE_ENABLED` switch.

## Routes and authorization

- `GET /api/v1/items/{id}/evidence?version_id={version_id}` requires both `read:items` and
  `read:evidence`. The gateway enforces `read:evidence`; the handler additionally enforces
  `read:items` before reading data. `INFOHUB_API_ITEMS_ENABLED` must also be enabled.
- `GET /api/v1/evidence/{id}` requires `read:evidence` and returns one raw-record metadata view.

The item route requires an exact immutable `version_id`; it never silently follows the mutable
current-version pointer. Results are ordered by raw-record ID. `limit` is 1–100, and signed
cursors bind the API key, authorization version, dataset epoch, item ID and version ID.

## Public evidence view

The public raw-record view contains only its ID, public source key, observation and ingestion
times, media type, payload SHA-256, payload kind, truncation state and byte size. The detail route
also lists the non-restricted document versions that reference that record and each input role.

Payload bytes, excerpts, storage references, request and redirect URLs, request or response
headers, source external IDs and retention controls are deliberately absent. A raw record that is
not attached to any document version returns 404. A record attached only to restricted documents
returns 403. Duplicate aliases are not exposed as public document references.

The detail response uses a principal-bound ETag over its complete public view and dataset epoch.
`If-None-Match` supports strong and weak comparison. Unknown, duplicate, empty, non-canonical or
out-of-range parameters use the stable v1 error envelope.

## Operations

This unit needs no schema migration and never performs network access or reads a payload blob.
Enabling this switch does not enable the event, analysis or item APIs. Windows production remains
unchanged until the completed release is intentionally deployed and the flags are enabled.
