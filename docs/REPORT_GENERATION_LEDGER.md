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

P21h is a **schema boundary**, not an invocation path. It introduces no model
call or publication path.

The [isolated Mac-copy migration rehearsal](evidence/p21h-report-generation-migration.json)
staged migrations 1–19 and applied only migration 20. All 33,569 article rows
and the digest of 9 legacy reports were unchanged; the two new tables were
empty, SQLite integrity was `ok`, and foreign keys passed. This is not a
Windows production migration. A fresh production backup and copy rehearsal
remain mandatory before any production deployment.

P21i adds `prepare_report_generation` and `record_report_response`. The first
requires a verified immutable snapshot, stores the exact rendered prompt in
the existing content-addressed blob store, and records the provider, requested
model, template hash and JSON parameters. Identical preparation is idempotent.
The second verifies that prompt blob before accepting a response, stores raw
response bytes in the same blob store, validates decoded JSON with the cited
draft contract, and appends a `valid_draft` or `invalid_draft` attempt. Bad
JSON and unsupported citations remain inspectable as raw bytes; validation
reports contain fixed error codes rather than untrusted response text. The
same response is idempotent and a run is limited to four distinct attempts.
Neither function invokes a provider or publishes a report version.

Both prompt and response CAS objects must be included in backups. The run
currently stores the **requested** model while the attempt stores the
resolved model returned by a future provider adapter. Usage and cost are
recorded only when supplied; budget authorization and cost verification must
precede a real paid provider call. `valid_draft` means structural references
passed, not that a claim is supported by the cited article. Semantic review
and an approval gate remain required before publication.

The [Mac-copy recording rehearsal](evidence/p21i-report-generation-recording.json)
stored one offline fixture prompt and one valid plus one invalid response
against a 120-item frozen input. It created no report version, left all 9
legacy reports unchanged, and made zero provider calls.
