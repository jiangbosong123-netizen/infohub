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
  daily cleanup path while immutable database ledgers and change history remain retained. A
  separate reference consumer now verifies complete snapshots and atomically commits change batches
  with its opaque cursor in a synthetic API/worker drill. Selected-projection policy,
  change-history compaction and the real P23 Windows/external-consumer drill are still absent; those
  remaining units keep the sync service pre-production.
- P15a now defines a strict `infohub.tone/1.0` result structure with speaker, target, versioned
  aspect, polarity, intensity, quote/span evidence and explicitly uncalibrated confidence. It is
  review-only: `valid` is rejected until CAS quote verification and the fixed evaluation admission
  are implemented. Declared target/speaker IDs are checked against the entity catalog in the same
  publication transaction. No model, historical tone output or production task has been enabled.
- P15b advances new tone runs to `infohub.tone/1.1`. Its JSON Pointer locators are verified against
  hash-checked UTF-8 raw CAS payloads before publication writes, and the verification hashes are retained in
  the result report. Version 1.0 stays readable but is not eligible for new runs or quote-verification
  claims. P15c adds the separate `infohub.tone-evaluation/1.0` annotation contract and a 16-case bilingual
  synthetic fixture covering negation, quotation, reported speech, sarcasm, mixed, unresolved targets and
  prompt injection. The fixture validates tooling only and explicitly fails the private-gold admission plan.
  P15c-2 adds hash-bound, no-model human tone review intake for a first and second independent reviewer;
  two opinions remain provisional and any prior private-evidence signature is invalidated by the new cases
  hash. P15c-3 adds a read-only fixed-pair agreement report for polarity Cohen kappa and exact agreement on
  speaker, target, aspect, intensity, evidence, phenomena and the full label. Real restricted-text sampling,
  tone-specific adjudication, HTML/PDF normalized text
  locators, model baselines and quality admission remain absent.
- P16 impact, P20 macro and sentiment outputs, the remaining portal cutover and final Windows
  consumer drills are later phases. A target endpoint in `openapi.yaml` is not proof that its route
  exists.

## Status rule

For any capability, use this precedence when deciding whether it is available:

1. current code and schema;
2. the module implementation document and its validation evidence;
3. this status page;
4. the target SPEC and OpenAPI design.

Target documents govern the intended semantics. They do not enable a route, migrate a production
database or certify an acceptance test by themselves.
