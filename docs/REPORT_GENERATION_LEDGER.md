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

P21j adds append-only `report_generation_reviews` as migration 21. A review
names one generation attempt, a manual source-check reviewer, a nonempty
reason, decision, draft digest and time. The database rejects approval of an
invalid draft and rejects mutation or deletion of a recorded decision. It
also replaces the version-insert trigger: a newly inserted `llm` version now
requires both a matching valid attempt and its approved manual review.
Previously stored LLM versions remain readable without retroactively
fabricating a review. A rejected draft requires a new generation attempt
before approval; the old decision stays in the audit trail.

The schema does not authenticate the reviewer or perform the source check on
their behalf. A later maintenance-only workflow must verify the raw CAS
response, binding digest, and reviewer identity before inserting a review;
production publication remains disabled until that workflow is tested.
The [Mac-copy review migration rehearsal](evidence/p21j-report-review-migration.json)
staged through version 20 and applied only version 21 without changing the
33,569 article rows, 9 legacy reports or existing report versions.

P21k adds a maintenance-only operator workflow. `report-review-preview ATTEMPT_ID`
verifies prompt and response CAS bytes, reparses the raw response,
revalidates the closed draft contract, and prints each claim beside the
frozen source title, publisher and URL. It returns a `review_digest` of the
exact validated draft. The operator must read the cited original material;
the snapshot may only contain a headline or a mutable legacy summary.
`report-review ATTEMPT_ID approved|rejected DIGEST REASON` requires the same
digest, uses the local OS account as the self-attested reviewer, verifies CAS
again and appends one immutable decision. Repeating the same decision is
idempotent; a conflicting decision is rejected. Invalid drafts can only be
rejected. Both commands require the maintenance process role and a verified
current-schema database. Neither command publishes a report.

The local OS username is an audit label, not a separate authentication
mechanism; it must not be treated as verified identity across shared accounts
or remote sessions. The [Mac-copy workflow rehearsal](evidence/p21k-report-review-workflow.json)
used a synthetic response, showed its source, and recorded a fixture rejection
because no human source check occurred. It changed no report version or
legacy daily row and made no provider call.
