# T-denial-detail-and-overdeny — PR-1: record what was attempted, not just which rule fired

- **status**: active
- **thread**: `T-denial-detail-and-overdeny` (spirrow-mindwire chatroom)
- **author of the spec below**: Bohr (proposer). Transcribed here verbatim in substance
  as spec task **S0**; the anchor for every later turn is *this file*, not a msg-id.
- **cleared by**: independent naysayer (Einstein), design round
- **Tier-C**: approved 2026-08-11 (S0 scope increase, 訂正 2, PR-1 start)

## Why this file exists

The design lived only in chatroom messages, so the implementer could not read it back
and halted under `OBL-DECLARE-UNREADABLE` — the same failure class this thread is
about (a declaration with no decided destination), reproduced inside the thread. The
anchor is moved from a volatile medium to a durable one.

## Observed defect

On 2026-08-11 six implementer sessions halted with `allow-list denied fs.delete`, and
**no record anywhere said what the session had tried to do**.

The classifier holds the raw command in `ClassifiedAction.detail` — the field's own
docstring says it "carries the raw command / path for messages" — but the error is
built from `decision.reason` alone, the static Tier-C string from the allow-list YAML.
The denial was loud about the *rule* and silent about the *act*.

Three of those halts quarantined threads, including `T-design-spec-delivery` (the fix
for the recurring "design not reachable" root cause) and this thread itself. The fix
for `fs.delete` could not be produced by the loop, because producing it required
sessions that `fs.delete` kept killing.

**This is not the same defect as #136 / #132.** The implementer's system prompt does
name deleting files as forbidden — the rule *is* delivered. What is missing is the
record of what tripped it.

## The two branches (why measurement comes before any fix)

`_RAW_COARSE`'s `fs.delete` pattern matches the **raw command string**, so a command
that merely *mentions* a deletion verb can be classified as performing one. But that
floor is gated by `_INDIRECTION_RE`, so it only runs when indirection is present.

Therefore the cause of the halts was one of:

- **(a)** the coarse floor fired on text inside a heredoc body (over-deny), or
- **(b)** the structural classifier genuinely resolved the command to a delete.

The cause could not be determined from any record, and **PR-1 does not guess**: it
makes the distinction observable and changes no verdict.

## S — work items

| id | content |
|---|---|
| S0 | Land this spec at `spec/design/T-denial-detail-and-overdeny.md` (done first) |
| S1 | Sink check. If `delivery.failed` has a structured record → put layer A in fields and **do not change the exception message string**. If not → preserve the `allow-list denied {op}: {reason}` prefix verbatim and append after ` \| ` |
| S2 | Find every `str.endswith()` / exact-match check on the denial message; reflect in S1's branch |
| S3 | One `render_denial` site only — no formatting spread across callers |
| S4 | Implement layers A and B |
| S5 | Tests T1–T4 |

**Only the recording path may be touched. `_RAW_COARSE` / `_classify_bash` /
`_classify_single_bash` / `_INDIRECTION_RE` / the allow-list YAML judgement logic are
off limits.**

## A — layer A (unconditional, no redaction needed, structurally free of input data)

| id | field | notes |
|---|---|---|
| A1 | `operation` | existing enum value |
| A2 | `reason` | existing static YAML string, unchanged |
| A3 | `rule_id` | which verdict fired — must distinguish structural / raw_coarse / mcp |
| A4 | `corroborated` | `"yes"` / `"no"` / `"unknown"` — **not a bool** |
| A5 | `match_offset` | `-1` when none |
| A6 | `match_line` | 1-based; `-1` when none |
| A7 | `line_count` | lines in detail |
| A8 | `detail_len` | length of detail |
| A9 | `has_heredoc` | detail contains a heredoc start |
| A10 | `indirection_gate` | did `_INDIRECTION_RE` fire (was the floor even eligible) |

**Forbidden in layer A** (enforced by T2): raw command, path, tool name, matched
literal, hash of detail. `sha256(detail)[:12]` and `matched_literal` were removed on
the naysayer's objection — do not reintroduce them.

## B — layer B (best-effort, redacted, disappears on failure)

| id | field | spec |
|---|---|---|
| B1 | `context_window` | ±100 chars around the match only, never the whole command. Redacts token shapes, `--token`/`--password`/`Authorization:`/`*_SECRET=` values, URL userinfo, long high-entropy runs. Escapes control chars and newlines. **Redactor raises → `<redact-failed>` (fail-closed).** `match_offset == -1` → `<no-match>` |

## M — measurement matrix (characterization; expectations record *current* behaviour)

| # | input | what it shows |
|---|---|---|
| M1 | heredoc writing the PR #138 PowerShell test body | the crux — record `indirection_gate` / `rule_id` |
| M2 | same body through the `Write` tool | does the classification path differ |
| M3 | `grep -rn "Remove-Item" tests/` | does a read-only search become FS_DELETE |
| M4 | `git commit -m "rm dead code"` | misfire via commit message |
| M5 | `rm -rf build/` | still FS_DELETE (under-deny regression guard) |
| M6 | heredoc body mentioning `git push --force` | same-shape over-deny, other verb |
| M7 | heredoc body mentioning `git reset --hard` | same |
| M8 | `bash -c "rm -rf /"` | still FS_DELETE — the floor's reason to exist |
| M9 | heredoc piped into `bash` | still FS_DELETE |

M5 / M8 / M9 stay as permanent regression guards. M1–M9 only call the classifier;
nothing is executed.

## T — tests

| id | content |
|---|---|
| T1 | known token shapes do not survive into output |
| T2 | layer A contains no input-derived data |
| T3 | M1–M9 characterization |
| T4 | message / record compatibility per S1's branch |

## AC — acceptance

| id | content |
|---|---|
| AC1 | the same input as 2026-08-11 yields **one record** from which "which verdict fired" and "was the match inside a heredoc body" are both readable |
| AC2 | not one existing denial verdict changes (M1–M9 match current measured values) |
| AC3 | T1 / T2 pass |

## Out of scope for PR-1

- strike counter (does not exist yet — PR-3)
- staging the consequence, i.e. making floor-only non-fatal (**PR-3**)
- changing `_RAW_COARSE` / the structural classifier / allow-list tier classification
  (PR-2 was withdrawn; the floor is not touched)
- adding a clause to `obligations.yaml` — a workaround for a defect we intend to fix
  must not be carved permanently into the SOT
- whether quarantine should fire (PR #138's territory)

## Residue (named, not silently accepted)

- The redaction list is a list of **shapes**. An unlisted secret shape survives into
  layer B. Layer A is the part that is safe by construction; layer B is convenience.
- `corroborated` is `"unknown"` whenever the coarse floor did not run, because there
  is then no floor verdict to corroborate. That is a vacuous-truth state, not a
  measurement, and PR-3 must not read it as evidence either way.
