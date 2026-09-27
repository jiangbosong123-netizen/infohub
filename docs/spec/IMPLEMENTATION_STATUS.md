# SPEC implementation status

This page prevents the target architecture from being mistaken for current runtime behavior. The
formal SPEC was written on `codex/architecture-spec@b910e60` but was not merged into `main` while
the implementation PR series proceeded. It was restored on 2026-09-27 after `main@6ebf8e0`.

## Current boundary

- Database safety, environment isolation, durable jobs, publication sequencing, raw provenance,
  immutable document/event/analysis/report foundations, API authentication and the implemented
  P18 read contracts are present in `main`.
- Implemented v1 contracts remain default-off and Windows production has not been upgraded during
  the current build-out. Each implementation document under `docs/` and machine-readable record
  under `docs/evidence/` states its own validation and production status.
- P19 snapshot plus reliable incremental synchronization is the active phase. Schema 39 contains
  the snapshot ledger, the worker builds immutable research snapshots from completed SQLite
  backups, and the default-off HTTP API creates, reports and reads verified snapshot pages. The
  snapshot resume cursor now drives bounded, hash-verified incremental change reads and rejects
  key, authorization or epoch changes. Expired private snapshot files have a verified, idempotent
  daily cleanup path while immutable database ledgers and change history remain retained. Selected-
  projection policy, change-history compaction and consumer drills are still absent; those
  remaining units keep the sync service pre-production.
- P20 macro and sentiment outputs, the remaining portal cutover and final Windows consumer drills
  are later phases. A target endpoint in `openapi.yaml` is not proof that its route exists.

## Status rule

For any capability, use this precedence when deciding whether it is available:

1. current code and schema;
2. the module implementation document and its validation evidence;
3. this status page;
4. the target SPEC and OpenAPI design.

Target documents govern the intended semantics. They do not enable a route, migrate a production
database or certify an acceptance test by themselves.
