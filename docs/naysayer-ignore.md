# `.naysayer-ignore` — data files the PR gate names instead of reading

Decided in thread `T-diff-cap-has-no-escape-for-indivisible-data-artifacts`
(Takahito ruling relayed in msg-8448; final design Bohr msg-8470, approved by
Einstein msg-8471). Implementation: `src/spirrow_mindwire/naysayer/naysayer_ignore.py`.

## Why

The gate reads at most 150,000 chars of a PR's diff (`_MAX_DIFF_CHARS`) and
force-posts REQUEST_CHANGES beyond that. A single data file (a recorded track,
an eval population) cannot be split across PRs, and its correctness is decided
by a machine check (a sha pin in a test, a manifest), not by a model reading
it. Such files are therefore named to the gate, not sent to it. The
dismiss + human-APPROVE workaround (msg-4423) is **not** made a rule; it stays
only until a repo carries its own `.naysayer-ignore`.

## The file

One file, `.naysayer-ignore`, at the repository root. The gate reads it from
the **PR head**, so a rule added in a PR applies to that PR. Every change to
the file itself is always reviewed in full.

```
# comment
Tools/tests/evidence/*.track.ndjson  # sha256+len pinned by Tools/tests/test_compare_tracks.py
data/eval/*.jsonl  # unchecked: record only
```

- Each line is `<glob>  # <guarantor>`. The glob matches the whole path:
  `*` and `?` stay inside one path segment, `**` crosses segments.
- The guarantor says what makes the data trustworthy. When nothing checks it,
  write `unchecked`. A line with no guarantor, an empty guarantor, or a
  negation (`!`) is invalid: it is not applied and the review names it with
  its line number and reason.
- Missing file (404) or an unreadable one: nothing is excluded.

## What is never excluded

Whatever a rule says, these are always sent in full, and the review names
the rule line that matched them: `.naysayer-ignore`, `.gitattributes`,
`.github/**`, and files with an extension in `NEVER_IGNORABLE_SUFFIXES`
(`.py .pyi .ps1 .psm1 .sh .bash .js .mjs .ts .tsx .rs .go .c .h .cpp .hpp .cs
.java .kt .lua .rb .toml .yml .yaml .cfg .ini`). `.json` can be excluded. A
rename or copy is excluded only when both sides match.

## What the gate does

1. Cuts the diff into one section per `diff --git` line and replaces each
   excluded section with one pointer line,
   `[excluded by .naysayer-ignore:L<n>] <path>  diff_chars=<n>`.
2. Applies the 150,000-char cap and the warn band to what is left. The
   delta of a later round goes through the same exclusion.
3. Writes a report — the driver writes it, not the model — into the prompt and
   at the end of the review body, with one line per excluded file:
   `[excluded by .naysayer-ignore:L<n>] <path>  bytes=<n>  sha256=<sha256 of the head blob>  blob=<40-hex git blob sha>  diff_chars=<n>  guarantor=<...>`.
   Deleted files say `bytes=0  sha256=deleted`. When the blob cannot be read,
   the file stays excluded and the line says `bytes=unknown  sha256=fetch_failed`,
   with `HASH UNAVAILABLE: <path>` below. Rules the base side does not
   carry are listed as `NEW RULE (this PR): L<n> <glob> → <k> files excluded`.
4. If the diff still exceeds the cap after exclusion, the gate notice names
   each file it dropped (`NEVER SENT: <path>`) or cut part-way
   (`PARTIAL: <path>`).

`mindwire pr-diff-size` applies the same exclusion, so its `original_chars`
equals the gate's in every case. It adds `excluded_chars=` and does not hash.

## Downstream

Each repo (playproof, voxelworld, …) adds its own `.naysayer-ignore` in its
own PR, which the gate reviews in full. Keeping data in the repo (playproof
`Docs/evidence-policy.md` §2) and not sending it to the gate in full do not
conflict.
