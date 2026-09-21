# `/mindwire-turn` — manual dispatch handbook

`/mindwire-turn` is the manual counterpart to the autonomous dispatcher: a
human runs one turn of the loop by hand, hands the reply back through the
same interfaces, and lets the loop continue on the next tick. This
document tells that human what they must write to `.mindwire/pin` before
they start.

The corresponding autonomous path is
[`src/spirrow_mindwire/dispatcher/core.py`](../src/spirrow_mindwire/dispatcher/core.py)
and
[`src/spirrow_mindwire/watcher/dispatcher.py`](../src/spirrow_mindwire/watcher/dispatcher.py);
both of them write `.mindwire/pin` via
[`src/spirrow_mindwire/spec_pin.py`](../src/spirrow_mindwire/spec_pin.py)
before every implementer / naysayer dispatch. The manual path has no
dispatcher to write on the operator's behalf, so the operator has to.

SPEC-2026-09-20-pin-hardening-and-id-audit D-32 is the governing text:
"dispatcher (コード側の `src/spirrow_mindwire/…` と `/mindwire-turn` に
よる手動ディスパッチの双方) は、すべての implementer / naysayer dispatch
の直前に `.mindwire/pin` を書く。" The mandate is on the operator when
`/mindwire-turn` is used, and on the code otherwise. There is no third
option — "書かない選択肢は存在しない".

## Before every manual dispatch: write `.mindwire/pin`

Two shapes are allowed. Pick one; write it verbatim (with the two
placeholder fields replaced); save at `<repo-root>/.mindwire/pin`; then
launch the agent.

`.mindwire/pin` is `.gitignore`d (see `.gitignore` and OBL-SPEC-PIN's
"Keeping `.mindwire/` out of version control ..." paragraph). Do not
commit it. Do not delete, rename, move, truncate, or rewrite it after
writing — OBL-SPEC-PIN forbids all of those. If the pin looks wrong,
open a new turn rather than editing the current one out.

### Shape A — resolved pin

Use when a spec has landed and applies to the turn. Fill in every
`<…>` placeholder before saving.

```yaml
schema_version: 1
spec_id: <SPEC-2026-MM-DD-slug>
thread: <T-thread-slug>
repo: spirrow-mindwire
branch: <feature/branch-you-are-on>
path: spec/design/<SPEC-2026-MM-DD-slug>.md
blob_sha: <40-hex git blob sha of the spec file>
commit: <40-hex git commit sha where the spec is on main>
pinned_at: <ISO-8601 UTC with literal Z suffix, e.g. 2026-09-21T12:00:00Z>
pinned_by: human
```

The `mode` field is deliberately omitted — SPEC-2026-09-20 §3-A declares
it optional with default `resolved`, and a pre-SPEC-2026-09-20 reader
also accepts this shape unchanged. Add `mode: resolved` explicitly if
you want the shape to be self-describing; do NOT write any other value.

### Shape B — bootstrap pin

Use when NO spec applies to the turn — the sanctioned "proceed on
message body" path (SPEC-2026-09-20 §3-B, D-34 BOOTSTRAP class).
`reason` is optional but strongly recommended: it tells the agent (and
the next reader of the git blame trail) why bootstrap was chosen.

```yaml
schema_version: 1
mode: bootstrap
pinned_at: <ISO-8601 UTC with literal Z suffix, e.g. 2026-09-21T12:00:00Z>
pinned_by: human
reason: "<one-line description of why no spec applies>"
```

**Do NOT** add any of `spec_id`, `thread`, `repo`, `branch`, `path`,
`blob_sha`, `commit` to a bootstrap pin — SPEC-2026-09-20 §3-A traps
every one of those as `PROHIBITED_FIELD` and the agent halts. A pin
that mixes `mode: bootstrap` with any resolved-form field is "neither
one nor the other".

## Which shape when — the two-question test

1. Does a spec named in `spec/design/` govern this turn's outcome? If
   yes, Shape A (resolved). If no, question 2.
2. Are you working directly from the message body in the thread —
   because the thread pre-dates the spec, or is design-work-in-flight,
   or is administrative? If yes, Shape B (bootstrap).

Both questions are for the operator. Do not ask the agent to decide; the
agent halts unless the pin already resolves.

## Placeholder cheatsheet

Field | What to put | How to get it
--- | --- | ---
`<SPEC-2026-MM-DD-slug>` | The spec's `spec_id` YAML front-matter | `grep '^spec_id:' spec/design/*.md`
`<T-thread-slug>` | The thread's `thread` YAML front-matter | Same file, `^thread:` line
`<feature/branch-you-are-on>` | Current git branch name | `git rev-parse --abbrev-ref HEAD`
`<40-hex git blob sha of the spec file>` | Git blob sha for the spec file on `main` | `git rev-parse main:spec/design/<spec_id>.md`
`<40-hex git commit sha where the spec is on main>` | The merge commit on `main` that landed the spec | `git log --format=%H --diff-filter=A --follow -- spec/design/<spec_id>.md \| tail -1` then confirm on `main`
`<ISO-8601 UTC with literal Z suffix>` | Wall-clock instant of pin write | `python -c "from datetime import datetime, UTC; print(datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00','Z'))"`

## Failure modes to expect

If you write a Shape A pin and a required field is empty, malformed, or
missing, the agent halts with `NO-PIN/MISSING_FIELD`. If you accidentally
mix a Shape B pin with a Shape A field, the agent halts with
`NO-PIN/PROHIBITED_FIELD`. If you set `mode:` to a value other than
`resolved` or `bootstrap`, the agent halts with `NO-PIN/MISSING_FIELD`
(SPEC-2026-09-20 §3-B step 3.5 branch 3 — fail-closed on unknown mode).
The full list of thirteen halt codes lives in OBL-SPEC-PIN
([`spec/process/obligations.yaml`](../spec/process/obligations.yaml))
and, verbatim, in
[`spec/design/T-spec-pin-hardening-and-id-audit.md`](../spec/design/T-spec-pin-hardening-and-id-audit.md)
§4-1.
