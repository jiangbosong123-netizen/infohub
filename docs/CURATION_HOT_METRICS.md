# Versioned hotspot metrics foundation (P13j)

The existing `stories` row is a legacy clustering result. Its `heat`,
`source_count`, `item_count`, headline, and recency can disagree with current
curation publications, even when the portal has correctly filtered hidden
members. Recomputing every recent story on each page request would be costly:
the Mac dataset has more than 5,000 stories in the 48-hour window. This change
adds a separate, disposable `curation_story_metrics` projection keyed by story
ID. It does not overwrite a legacy story or change page reads yet.

The projection reserves a visible article count, a count of *known, distinct
publisher identities* (not feed/source IDs), an event heat value, a visible
representative headline and URL, visible company slugs, and the latest visible
article time. `computed_at` makes the half-life decay reproducible: the future
reader must decay stored heat from that timestamp to the query time. Unknown
publisher attribution contributes to article count but not publisher count.
No metric is calculated by this migration; the state starts `empty` and must
be built and validated before any read cutover.

Dirty triggers cover legacy story/membership edits, item fields that affect
visibility, ranking, provenance or display, document version changes, and
current translation/relevance/importance publication pointers. The future
builder will scan stories in bounded ID order, then drain the dirty queue in
transactions. It must use the same current-publication visibility and score
rules as the portal, and the same publisher identity function as legacy story
aggregation. An item deletion that removes a membership must recalculate the
surviving story; a story deletion cascades projection and dirty rows.

This bridge projection does not redefine stable events or NLP confidence.
Versioned `events` remain the future API entity; these metrics only keep the
legacy portal honest during migration. The table can be discarded without
altering event history or source documents. Mac copy migration evidence is in
`docs/evidence/p13j-hot-metrics-schema-rehearsal.json`; Windows production has
not been migrated.
