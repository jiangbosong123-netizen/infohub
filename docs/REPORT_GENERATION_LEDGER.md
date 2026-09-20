# Report model-generation ledger (P21h)

Migration 20 adds an append-only provenance chain for a future model-generated
report. It creates `report_generation_runs` and `report_generation_attempts`,
then adds nullable `generation_attempt_id` to `report_versions`. No existing
report version, legacy daily report, or input snapshot is rewritten. Versions
created before this migration may retain a null link; new `llm` versions must
name a matching, `valid_draft` attempt. A structured fallback cannot claim a
model attempt.

A run points to one immutable report input snapshot in the same dataset. It
records the provider and requested model, prompt-template ID and SHA-256,
rendered prompt CAS reference and SHA-256, exact parameters JSON, and prepare
time. Repeated attempts belong to that run. Each attempt records its ordinal,
resolved model and optional provider request ID; valid/invalid/refused/failed
status; raw response CAS reference and hash when a response exists; validated
draft JSON for a valid result; validation report; optional token and cost
figures; and start, finish and record times. Attempts and runs cannot be
updated or deleted. The database rejects a valid draft without a raw response
reference or validated JSON, and rejects an LLM version whose attempt uses a
different snapshot, dataset, provider, model or prompt identity.

This is a **schema boundary**, not an invocation path. It does not yet store
CAS objects, verify bytes against the declared hashes, enforce model budget,
perform semantic review, or approve/publish a draft. Those actions require
separate guarded code and tests. Until then the scheduled worker continues to
publish only the deterministic structured fallback on its opt-in path; the
default legacy path remains guarded against retry overwrites.

The [isolated Mac-copy migration rehearsal](evidence/p21h-report-generation-migration.json)
staged migrations 1–19 and applied only migration 20. All 33,569 article rows
and the digest of 9 legacy reports were unchanged; the two new tables were
empty, SQLite integrity was `ok`, and foreign keys passed. This is not a
Windows production migration. A fresh production backup and copy rehearsal
remain mandatory before any production deployment.
