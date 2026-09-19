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
No metric is calculated by the migration; the state starts `empty`. P13k adds
`INFOHUB_PROCESS_ROLE=maintenance python cli.py curation-hot-advance 100`.
Repeat the command until the report is `ready` with `dirty_remaining=0`.
Each call commits at most 100 story IDs. The cursor, computed rows, and dirty
acknowledgements share one transaction; failure leaves the batch retryable.
The builder uses current relevance/importance visibility and score, current
translation for the headline, and `publisher()` for known publisher identity.
Zero-visible and redirected stories get a zero row so the completeness count
can be checked against `COUNT(stories)`. P13l adds an independently controlled
portal read switch, `INFOHUB_CURATION_HOT_ENABLED=true`, which requires the
curation read projection. It reads these metrics only when the state is ready,
the dirty queue is empty, and story and metric counts agree. Otherwise it
displays the legacy values with an explicit old-metric notice. Turning the
flag off restores the legacy path without reversing migration 18.

The reader decays stored heat from `computed_at` to query time before sorting,
then ranks known multi-publisher stories ahead of single-publisher stories.
Channel and topic filters check *visible member items*; they cannot rely on
`stories.channel`, because the Mac data contains mixed-channel legacy stories.
The filter uses a set-based query so a page does not execute one curation
lookup per story. This remains a portal ranking, not calibrated sentiment or
an API-grade macro impact score.

Dirty triggers cover legacy story/membership edits, item fields that affect
visibility, ranking, provenance or display, document version changes, and
current translation/relevance/importance publication pointers. The future
builder scans stories in bounded ID order, then drains the dirty queue in
transactions. An item deletion that removes a membership must recalculate the
surviving story; a story deletion cascades projection and dirty rows.

This bridge projection does not redefine stable events or NLP confidence.
Versioned `events` remain the future API entity; these metrics only keep the
legacy portal honest during migration. The table can be discarded without
altering event history or source documents. Mac copy migration evidence is in
`docs/evidence/p13j-hot-metrics-schema-rehearsal.json` and the isolated full
build in `docs/evidence/p13k-hot-metrics-build-rehearsal.json`, and guarded
read timing in `docs/evidence/p13l-hot-metrics-read-rehearsal.json`; Windows
production has not been migrated.
