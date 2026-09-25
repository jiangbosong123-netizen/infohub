# Topic review audit export

The audit export creates a deterministic, point-in-time JSON package for one
immutable topic review sample. It is a read-only maintenance operation and does
not approve a sample, publish statistics or change an assignment decision.

Create an export at the current review high-water mark:

```text
INFOHUB_PROCESS_ROLE=maintenance python cli.py \
  topic-review-sample-export BATCH_ID /restricted/path/review.json current
```

To reconstruct an earlier review state, replace `current` with the exact
non-negative `topic_assignment_review_order` sequence. A cutoff beyond the
database's current review high-water mark is rejected. The destination must not
already exist.

Verify a package without connecting to the database:

```text
python cli.py topic-review-sample-export-verify /restricted/path/review.json
```

The package contains:

- the immutable sampling batch and its stored member-manifest digest;
- the explicit review cutoff sequence;
- every sampled assignment in stable ordinal order, with document, topic,
  method, source link and assignment evidence;
- the latest append-only human decision visible at that cutoff, including its
  predecessor, reviewer, reason, evidence and review sequence; and
- the overall and per-topic sample report reconstructed at the same cutoff.

`payload_sha256` covers the canonical payload. The standalone verifier also
reconstructs the sample selection manifest from every member and compares it to
the batch's immutable `manifest_sha256`. `exported_at` is outside the canonical
payload, so exporting the same batch at the same cutoff produces the same
content digest even at a different wall-clock time. The verifier rejects a
changed payload, member count or selection manifest. The checksum is not a
digital signature; authenticity still depends on the trusted database or a
separately authenticated delivery channel.

Exports contain article titles, URLs, reviewer identities and reasons. They are
private audit artifacts and must not be committed to the public repository.
New files are written atomically with owner-only permissions where the operating
system supports POSIX modes. Store them under a restricted path and include them
in the normal encrypted operational backup policy.
