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
  speaker, target, aspect, intensity, evidence, phenomena and the full label. P15c-4 adds third-person,
  hash-bound tone adjudication into a new immutable private dataset version; the final label retains both
  original reviews and invalidates any evidence signature made against older cases. Real restricted-text
  sampling, HTML/PDF normalized text locators, model baselines and quality admission remain absent from
  the checked-in public fixture. P15e-1 through P15e-3 now provide hash-bound release evidence, authorized
  human decision validation and controlled append-only admission. P15e-4a/4b add a shadow-only rollout,
  frozen full population, deterministic sample, immutable observations and a passed evaluation completion
  gate. P15e-4c-1 binds one completed rollout to an independently authorized production profile and
  terminal rollback ledger. P15e-4c-2 adds schema 44 and requires every new `valid` tone result to match
  the active release runtime and calibration inside the publication transaction, with activation provenance
  exposed by the analysis API. Rollback closes new writes while immutable results and exact retries remain
  readable. No real private gold, model execution, real release activation, valid production tone result,
  portal read switch or production deployment has been performed.
- P16a registers the review-only `impact/1.0` contract. Impact runs require an immutable event-version
  subject, catalog-backed target, fixed aspect/horizon vocabularies, and direct non-generated event
  evidence with role-aware support/contradiction validation. P16b adds the separate
  `infohub.impact-evaluation/1.0` annotation contract and a 16-case bilingual synthetic fixture for
  conditional plans, direct effects, conflicting evidence, numeric revisions, cross-entity effects,
  insufficient evidence and prompt injection. P16c adds hash-bound, no-model human impact review intake for
  first and second independent reviewers; both opinions remain provisional and any prior evidence signature is
  invalidated by the new cases hash. P16d adds a read-only fixed-pair agreement report with direction Cohen
  kappa as the gate field, diagnostic status/evidence-judgment kappa, and exact agreement on every controlled
  assessment component. The fixture and private intake remain non-publishable contract data.
  `valid` and calibrated impact remain closed; no impact model, private labels or production result exists.
  P16 adjudication/evidence review/metrics/admission, P20 macro and sentiment outputs, the remaining
  portal cutover and final Windows consumer drills are later phases. A target endpoint in `openapi.yaml` is not
  proof that its route exists.
- D23 (`single-owner-v1`, 2026-10-03) resolves U09: the owner is the only human annotator. Multi-person
  gold keeps its definition but is unreachable for now; owner-blind labels with a delayed intra-annotator
  recheck support only *experimental* quality claims, and versioned algorithm (silver) labels may enter
  train/dev but never act as evaluation truth. The protocol is recorded in `DECISIONS.md` §5. D23-a adds
  the `owner_labeled` and `algorithm_labeled` dataset states with tier-separation validation (relevance, tone
  and impact) and a hash-bound blind owner-label intake built on the shared review-intake core. D23-b binds
  the protocol to a label-definition version, publishes `relevance-definition-v1`, and adds a loopback-only
  relevance labeling console that re-verifies frozen source content, never reads model fields, and exports
  owner batches. D23-c adds `classification-metrics-v4` (annotation tier, explicit experimental-claim
  blockers, prediction slices) and a hash-bound prediction run for the legacy stored `tmt` field, sliced into
  LLM-only, policy-forced and unscored rows. A real 600-case relevance dataset is admitted privately and awaits
  owner labels. D23-d adds the fixed, recomputable recheck sample, a blind recheck console mode, a recheck intake
  that never changes final labels, and a same-owner kappa report that metrics read directly; experimental
  claims unblock only after a complete recheck with kappa >= 0.70 and no unresolved disagreement. D23-e adds
  reasoned, unblinded owner resolutions of recheck disagreements that become the final label while both
  earlier labels stay on record. D23-f adds a versioned relevance LLM labeler (`relevance-llm-v1`) with a
  call budget and full request/response audit; it is evaluated as a model on the owner test split before
  its train/dev outputs can be imported as silver, and owner labels always take precedence. The owner
  approved its small paid use on 2026-10-04 (U08). Tone/impact labeling UIs and silver labelers do not exist yet.
- A full cutover rehearsal (2026-10-04) took an isolated copy of the Mac legacy database (89,575 items) from schema
  0 to 46 with backfill, event projection, 358,300 legacy curation imports, search/hot/topic builders, every audit,
  bundle backup, isolated restore and portal smoke, offline. It found and fixed a quadratic story reindex, a full
  backlog sort on every job claim (schema 45) and two history-wide scans on every publication and budget check
  (schema 46). The resulting plan is `docs/CUTOVER_PLAN.md`; production remains un-upgraded and both hosts are
  currently not collecting.

## Status rule

For any capability, use this precedence when deciding whether it is available:

1. current code and schema;
2. the module implementation document and its validation evidence;
3. this status page;
4. the target SPEC and OpenAPI design.

Target documents govern the intended semantics. They do not enable a route, migrate a production
database or certify an acceptance test by themselves.
