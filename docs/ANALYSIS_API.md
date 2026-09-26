# Published analysis read API

`GET /api/v1/analyses/{id}` reads one immutable `analysis_results.id` that has an
`analysis_publication_versions` record. It requires `read:analyses` and is controlled by the
independent, default-off `INFOHUB_API_ANALYSES_ENABLED` switch. Internal attempts and unpublished
results are not addressable through this route.

## Returned provenance

The response includes the exact publication view version and state time, task and output schema,
subject version, ordered input manifest, input hash, pipeline and provider, requested and resolved
model, prompt hash, canonical parameters hash, analysis and availability times, structural
validation state, result/review/evidence states, evidence IDs and validated output. `stale=true`
means a later publication is currently selected for the same subject version and task; the old
immutable result remains readable by its own ID for audit and reproducibility.

The endpoint never returns rendered prompts, raw response or output storage references, provider
request IDs, token counts, prices, costs, error detail or model reasoning. Those values remain in
the internal operations ledger. Evidence payloads require the separate `read:evidence` contract.

The response uses a principal-bound ETag over the dataset epoch and complete public view.
`If-None-Match` supports strong and weak comparison. Unknown query parameters, including the
future `as_of` view, currently return 422 rather than pretending to provide historical review
state.

## Operations

This endpoint needs no schema migration and does not invoke a model. Enabling it does not enable
analysis jobs or provider network access. Windows production remains unchanged until the finished
release is intentionally deployed and its feature flag is enabled.
