# Cited model draft contract (P21g)

`render_validated_draft(manifest, draft)` is a pure validation and rendering
boundary for a future model-generated daily report. It neither calls a model
nor writes to the database. The caller must first load an immutable report
input with `load_frozen_manifest(snapshot_id)`; passing an unverified mapping
does not establish input provenance.

The draft is JSON with exactly `schema_version`, `date`, and `sections`:

```json
{
  "schema_version": "infohub.report-draft/1.0",
  "date": "2026-09-18",
  "sections": [{
    "channel": "stock",
    "claims": [{
      "text": "公司发布季度数据",
      "input_ordinals": [0]
    }]
  }]
}
```

Each section uses one of the existing `stock`, `ai`, or `robot` channels,
without duplicates. Each claim has 1–3 distinct input ordinals from the
*same channel* and up to 280 characters of single-line text. Each section
has 1–12 claims. Extra fields, arbitrary links or control characters in
claim text, unknown ordinals, unsafe source URLs, or a mismatched date fail
closed. The renderer controls all Markdown links: each is reconstructed from
the frozen input URL, with a numbered citation tied to the exact input
ordinal. Text and source labels are escaped. Coverage records the selected
material, number of claims and citations, and the historical point-in-time
limitation.

**A valid reference does not prove the cited article supports the claim.**
The result is explicitly marked `not_automatically_verified`. Publication
will require a separate immutable generation-attempt record with prompt,
provider/model and raw-response provenance, plus semantic review and
approval. This PR does not publish an LLM version, add a provider call, or
change the worker/portal switches.

The [Mac-copy rehearsal](evidence/p21g-report-draft-contract-rehearsal.json)
used a frozen 120-item report input, validated one cited claim in each of the
three present channels, and left report versions and the 9 legacy reports
unchanged. Windows production was not touched.
