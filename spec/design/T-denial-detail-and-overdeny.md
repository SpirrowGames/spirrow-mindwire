# STATUS: RETIRED — historical record only (2026-08-26)

**This entire document — both the PR-1 section and the PR-3 section that follows
it — describes machinery that has been removed from the codebase. Do not use it
as a spec. Do not go looking for the functions, modules, or fields it names.**

The per-call `can_use_tool` allow-list, together with the classifier
(`allowlist.py`), the denial record (`denial_record.py`), the redactor, the
`ImplementerAllowlistError` type, and every layer-A / layer-B field the two
sections below discuss (`build_denial_record`, `rule_id`, `indirection_gate`,
`corroborated`, `context_window`, `match_offset`, `evidence_status`,
`target_root_relation`, …) were retired on **2026-08-20** by these commits, all
ancestors of `main`:

| commit | subject |
|---|---|
| `773ee24` | `feat(implementer): remove the allow-list gate, with its tests (1/2) (#165)` |
| `e7b359a` | `feat(implementer): remove the allow-list gate from the adapter` |
| `72339ee` | `chore(allowlist): delete the modules the gate used (#166)` — deleted `src/spirrow_mindwire/allowlist.py` (414 lines), `src/spirrow_mindwire/denial_record.py` (151 lines), `src/spirrow_mindwire/adapters/implementer_allowlist.yaml` (135 lines) |

The invariants those modules approximated are now enforced outside the agent
(GitHub org ruleset `guard-default-branch`, `spirrow_mindwire.preflight` P0/P1/P2,
the egress proxy allow-list, and the implementer clone being disposable). See
`src/spirrow_mindwire/adapters/implementer.py` module docstring for the
first-person narrative of the retirement, and `spec/design/T-design-spec-delivery.md`
fact **E-12** for the environmental record.

The reason this file remains in the tree, rather than being deleted, is that the
measurement work behind it (msg-916 through msg-1714 of thread
`T-denial-detail-and-overdeny`) produced architectural lessons that apply to any
future refusal-diagnostic surface in this project. Those lessons are recorded in
**§K — Knowledge to keep** at the bottom of the file. Everything above that
section is the archived reasoning that produced them.

If you are reading this because a future halt looks similar to what it
describes: **check §K first**, and open a new thread rather than trying to
resurrect this one.

---

# T-denial-detail-and-overdeny — PR-1: record what was attempted, not just which rule fired

- **status**: **retired 2026-08-26** — the machinery this section describes
  (`ImplementerAllowlistError`, `build_denial_record`, `allowlist.py`,
  `denial_record.py`, `_RAW_COARSE`, `_INDIRECTION_RE`, layer A / layer B, the
  redactor, and every field they own) was **deleted** on 2026-08-20 (see the
  file header for commit list). The section is kept as a historical record of
  the reasoning that produced §K's lessons. **The implementation described
  below does not exist in the codebase.**
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

---

# PR-3 — give structural verdicts a first-order evidence surface

**STATUS: RETIRED — do not implement (2026-08-26)**

**Retirement decided**: Bohr, msg-1714, on Heisenberg's P3-S1 halt report
(msg-1713). Endorsed by Einstein (msg following msg-1714).

**Why retired**: the substrate PR-3 was designed to extend — the per-call
`can_use_tool` allow-list, `build_denial_record`, `ClassifiedAction`,
`_RAW_COARSE`, `_INDIRECTION_RE`, layer A / layer B, the redactor, and the
`denial[...]` log line — was **deleted on 2026-08-20** by commits `773ee24` /
`e7b359a` / `72339ee` (see file header for details). The `rule_id='structural'`
halts PR-3 was designed to make readable **cannot occur anymore**, because the
classifier that produced them is gone.

**Do not resurrect this spec.** msg-1714 explicitly rejects the option of
retargeting PR-3 at whatever refusal surfaces exist today (org-ruleset /
`preflight` / egress proxy): those surfaces have **zero halt observations**
attached to them and **have not been checked** for a diagnostic gap. Designing
against unmeasured targets was rejected by the same discipline (`P3-N3`) this
spec used to defer strike counters. See **§K** (Knowledge to keep) at the
bottom of the file for the architectural lessons this line of work produced,
and **§T** (Trigger retargeting) for what the resume conditions were rewritten
to name.

- **status**: **retired 2026-08-26** — spec landed for the historical record only
- **author of the spec below**: Bohr (proposer). Transcribed verbatim from msg-1711.
- **cleared by**: independent naysayer (Einstein, msg-1139 category-error correction
  + endorsement of `evidence_status.present`, redact-then-truncate order, and
  substrate-check-then-halt discipline).
- **Tier-C history**: approved 2026-08-16 (msg-1138 — implementation start);
  **retired 2026-08-26** (msg-1714).
- **anchor from this point on**: this file, not any msg-id.

> Source: msg-948 §2 (freeze) + msg-947 (Einstein: drop D2a, take D2b, drop D3) +
> msg-1138 (trigger register) + msg-1139 (Einstein's category-error correction).
> This section is self-contained; the chatroom does not need to be replayed to
> implement it.

## P3-0. Purpose and contract

**Purpose**: give `rule_id='structural'` denials a **first-order evidence surface**.

**Contract (identical to PR-1)**: **do not change any verdict. Make it readable
without stopping any halt.** Halt frequency is not this PR's goal (msg-1138 §2).

**Root cause (single)**: PR-1's diagnostic vocabulary is built around
`match_offset`. Structural verdicts carry no offset, so every field keyed off it
(`match_offset` / `context_window` / `corroborated`) goes null at once and
**"this rule class has no evidence by design" collapses into "we tried and
failed"**.

## P3-1. Invariants (absolute)

- **No input-derived data in layer A.** PR-3 adds only the `P3-D1` fixed enum to
  layer A.
- **Never accept "it is an enum, so layer A is fine."** Even a one-bit
  "is-it-inside-the-repo" is input-derived → layer B. One exception rewrites the
  invariant from "guaranteed by structure" to "judged by quantity".
- **Layer B reuses PR-1's existing redactor. No second redaction policy is
  created.**

## P3-2. S — work items

| id | content |
|---|---|
| **P3-S0** | **Land this section into `spec/design/T-denial-detail-and-overdeny.md` first.** The anchor from this point on is this file |
| **P3-S1** | **Substrate check.** Verify PR-1's surfaces (`build_denial_record` / layer A / layer B / redactor / `rule_id` / `indirection_gate`) still exist on current `main` as written. **On divergence, do not implement — report and stop** (§P3-6) |
| **P3-S2** | Implement `P3-D1` (`evidence_status`) |
| **P3-S3** | Implement `P3-D2b` (structural verdict evidence surface) |
| **P3-S4** | Add tests `P3-T1`..`P3-T6` |

**Only the recording path may be touched.** `_RAW_COARSE` / `_classify_bash` /
`_classify_single_bash` / `_INDIRECTION_RE` / allow-list YAML judgement logic
are off limits.

## P3-3. D1 (final) — separate "not applicable" from "failed"

**Layer A. Fixed enum. No input-derived data.**

| id | field | value | meaning |
|---|---|---|---|
| P3-D1 | `evidence_status` | `present` | evidence surface was rendered into layer B |
| | | `not_applicable_for_rule_class` | this rule class has no evidence surface by design (operations without a target, etc.). **Vacuous, not a failure** |
| | | `lookup_failed` | evidence was attempted but the lookup raised or returned an unexpected shape |

**Collapsing these two values (`not_applicable_for_rule_class` /
`lookup_failed`) is what cost six sessions.** Do not write the collapsing
implementation.

**`corroborated`'s three values are not changed** (PR-1 contract; different
axis). `corroborated='unknown'` being vacuous truth is readable from
`indirection_gate=False` (msg-945).

## P3-4. D2b (final; main line) — give structural verdicts an evidence source

**All layer B.** Renders `ClassifiedAction`.

| id | field | layer | spec |
|---|---|---|---|
| P3-D2b-1 | `target_root_relation` | **B** | Relation to root: `repo` / `workspace` / `temp` / `home` / `external` / `relative_unresolved` / `unknown`. **Enum, but input-derived → layer B fixed** |
| P3-D2b-2 | `target_tail` | **B** | Only the last one segment. Full path forbidden. **Apply PR-1's existing redactor** |
| P3-D2b-3 | `target_tail_cap` / `target_tail_truncated` | **B** | Length cap and whether truncation occurred. **Do not collapse "chopped by the cap" and "already short"** |
| P3-D2b-4 | `target_origin` | **B** | `literal` (as written in the command) / `resolved` (classifier supplied the root). **Say which value is being rendered** |

**P3-D2b-5 (order fixed)**: **redact, then truncate.** The reverse order
(truncate then redact) creates the exact same-shape hole as the residue PR-1's
Tier B flagged (a boundary-clipped token slips past the redactor). Fix the
order in a test (`P3-T3`).

**P3-D2b-6 (do not eat globs)**: redaction acts on secret shapes only. `*` /
`?` / `[` and other shell metacharacters must survive it — **the true face of
over-deny is "the classifier resolved a glob or a relative path to a root the
user did not intend"**, and losing the metacharacters loses the distinction.

**P3-D2b-7 (render target)**: **render "what the classifier considered the
target", not "what it resolved to".** Emitting only the resolved form reads as
`FS_DELETE on <repo>/x` — **whether the classifier supplied the root becomes
unreadable again.** Collapse this and PR-3 meets `P3-AC1` without meeting its
purpose.

**P3-D2b-8 (do not paper over)**: structural verdicts whose target is
unidentifiable (operations without a `path`, etc.) take
`not_applicable_for_rule_class`; `P3-D1` reports it honestly. **The observed
deaths are fs-family; covering fs covers the observed damage.** Anything else
that halts brings us back at that time.

**Note**: D2b never slices a window, so the existing residue msg-944 recorded
(regex-window boundary clipping) is **not extended**. Whether the existing
window itself is well-formed is the separated concern in §P3-8, not this PR.

## P3-5. Non-scope (do not include)

| id | item | reason |
|---|---|---|
| P3-N1 | **D2a** (synthesise `match_offset` from a span, reuse the existing window) | **Withdrawn** (msg-948 §1). If the classifier parses a normalised string, the span is an index into a different string than `raw` → the window slices **anywhere but the match** and **emits wrong content in a form that is not detectable as wrong** |
| P3-N2 | **D3** (`structural` sub-id split) | Einstein's YAGNI: **out of scope from the start** (msg-947) |
| P3-N3 | strike counter / threshold 2 / making floor-only non-fatal | Deferred (trigger in §P3-8). **Do not build a mechanism for a path that has zero observations** |
| P3-N4 | changes to the classifier / floor / allow-list tier classification | **Do not fix before measuring** |
| P3-N5 | new clauses in `obligations.yaml` | Do not carve a workaround for a defect we intend to fix into the SOT |
| P3-N6 | diagnostic surface for `ThreadIdCollisionError` and other non-allowlist guards | **Different category** (§P3-8) |

## P3-6. AC — acceptance

| id | content |
|---|---|
| **P3-AC1** | **From the log line of the next structural halt alone, without touching the raw command, a reader must be able to name both ①what the classifier considered the target and ②whether that is what was written or what the classifier resolved. Fail if ② is unreadable** |
| P3-AC2 | No existing denial verdict changes (M1 / M5 / M6 / M7 / M8 / M9 match the current measured values, gate green) |
| P3-AC3 | No input-derived data leaks into layer A (extend PR-1's T2 to cover the new fields) |

## P3-7. T — tests

| id | content |
|---|---|
| P3-T1 | Known token shapes (`ghp_…` / `github_pat_…` / `xox[baprs]-…` / `eyJ….….…` / `AKIA…`) do not survive into **the new layer-B fields** |
| P3-T2 | Layer A contains no input-derived data (post `evidence_status` too) |
| P3-T3 | **Order redact-then-truncate is fixed** (a fixture whose input leaks under the reverse order) |
| P3-T4 | `evidence_status.not_applicable_for_rule_class` / `lookup_failed` do not collapse |
| P3-T5 | `target_origin.literal` / `resolved` do not collapse |
| P3-T6 | Structural verdicts carry an evidence surface (regression guard) |

## P3-8. Deferred, with triggers (no new ledger)

| item | resume trigger | direction |
|---|---|---|
| floor-only non-fatal / strike counter / threshold 2 | first observation of a halt carrying `indirection_gate=True` (**instrument is already deployed**) | Undecided (frequency not measured) |
| D3 (`structural` sub-id split) | first halt where operation + target alone did not let a human judge whether the denial was correct | Undecided |
| Diagnostic surface for non-allowlist guards | **second observation** of the same class (msg-1138 §3 was the first) | **Do not push into denial telemetry.** `ThreadIdCollisionError` is a *domain invariant violation*, not an *authorization failure* → **fix in standard application error logging** (Einstein, msg-1139) |

---

## §4. Handling the 9.6-day gap — do not adapt by guess

The measurements (M1–M9, the two structural halts) are the **actual measured
values from 2026-08-12..16**, and `main` has moved 12+ commits since then.

- **Always run `P3-S1` before implementing.** If PR-1's surfaces still exist as
  written, proceed to `P3-S2`.
- **If they have diverged, do not go looking for a way to align — stop and
  report.** "It was probably renamed to this" is the same failure class this
  thread has been caught by three times.
- **`P3-AC1` does not depend on any particular thread.** msg-948 §4.3 named
  `T-human-terminal-overuse` / `T-design-spec-delivery` as living test benches,
  but #167 has landed and their quarantine states may have moved. **Any single
  structural halt is enough to complete acceptance.** The state of a specific
  test bench is a check item, not a blocker.

## §5. Substrate

`feature/denial-detail-and-overdeny` (both `local` and `origin` on `d79c45a`)
**is a copy of PR-1's branch and cannot serve as the substrate.** Cut a new
branch from current `main`. Base is `main` (this repo has no `develop`). Gate
is `bash .mindwire-gate`. Merging is always Tier-C (human).

## §6. Relation to the spec-delivery manifest (#167)

As Heisenberg noted, the manifest machinery has landed. **However this spec's
author has not read its interface, so a specific call shape is not prescribed
here** (§1's method: do not describe behaviour of a stage not verified to be
reached). `P3-S0` is "append this section to the existing file in PR-1's
format"; if the manifest side has registration requirements, follow the
**documented** contract on that side. If the two conflict, prefer the manifest
and record the delta in the PR body.

## §7. Separated concern (still open — do not mix into this PR)

**Whether the existing regex-derived layer-B window's exposure surface is
well-formed.** Einstein's argument (msg-947) does not refute D2a, but **if his
concern is real the target is the deployed window, not PR-3.** Neither of us
has verified either way → do not assert. Recommendation: separate thread.
Urgency is a Tier-C call, still open.

---

# P3-S1 substrate check result (2026-08-26; implementer: Heisenberg)

**Verdict: substrate DIVERGED. Implementation halted per §P3-2 / §4.**

## What P3-S1 measured

The frozen PR-3 spec above assumes the PR-1 surfaces exist on current `main`:

- `build_denial_record` in `src/spirrow_mindwire/denial_record.py`
- Layer A / layer B fields (`operation`, `reason`, `rule_id`, `corroborated`,
  `match_offset`, `match_line`, `line_count`, `detail_len`, `has_heredoc`,
  `indirection_gate`, `context_window`)
- The classifier's `_RAW_COARSE`, `_INDIRECTION_RE`, `_classify_bash`,
  `_classify_single_bash`
- `ImplementerAllowlistError` and the `delivery.failed` denial record
- The redactor / `render_denial`

## What is on `main = 5970a48` today

Grepping the tree for `build_denial_record`, `ImplementerAllowlistError`,
`_RAW_COARSE`, `_INDIRECTION_RE`, `render_denial`, `evidence_status`,
`indirection_gate`, `rule_id`, `corroborated`, `Operation.FS_DELETE`,
`ClassifiedAction`, `denial_record`, `allowlist.py`:

**Zero source hits.** The only matches are inside this design file and inside
`T-design-spec-delivery.md` (which references `ClassifiedAction` and
`can_use_tool` only historically). No test file references any of them either.

The removal is documented in the adapter's own module docstring
(`src/spirrow_mindwire/adapters/implementer.py` lines 9–51):

> There used to be one: an operation classifier plus a `can_use_tool` allow-list
> that denied four Tier C operations and branch-scoped the destructive git verbs.
> It was removed on 2026-08-20 after its reach was measured against what it cost.

The commits that dismantled the substrate are all ancestors of current `main`:

| commit | subject |
|---|---|
| `773ee24` | `feat(implementer): remove the allow-list gate, with its tests (1/2) (#165)` |
| `e7b359a` | `feat(implementer): remove the allow-list gate from the adapter` |
| `72339ee` | `chore(allowlist): delete the modules the gate used (#166)` — deleted `src/spirrow_mindwire/allowlist.py` (414 lines), `src/spirrow_mindwire/denial_record.py` (151 lines), `src/spirrow_mindwire/adapters/implementer_allowlist.yaml` (135 lines) |

`T-design-spec-delivery.md` E-12 corroborates:

> per-call の `can_use_tool` allow-list は 2026-08-20 に撤去された ∴ `git fetch` を拒める gate は存在しない。不変条件は agent の外（GitHub org ruleset `guard-default-branch` ／ `spirrow_mindwire.preflight` の P0-P2 ／ egress proxy の allow-list ／ implementer の clone が使い捨てであること）が担う

## Why implementation is halted

The frozen spec directs the implementer to:

- extend layer A with `evidence_status`,
- add layer-B fields (`target_root_relation`, `target_tail`, `target_origin`,
  …) driven by `ClassifiedAction`,
- extend PR-1's redactor,
- add tests that pin the new fields alongside `M1 / M5 / M6 / M7 / M8 / M9`
  which are measurements of the classifier's behaviour.

**Every one of those surfaces has been deleted.** There is no `ClassifiedAction`
to render, no redactor to reuse, no `M1..M9` to preserve as `P3-AC2` requires,
no `denial[...]` log line to enrich, no structural verdict in production to
diagnose. The `rule_id='structural'` halts that PR-3 was designed to make
readable **cannot occur anymore** because the classifier that produced them is
gone.

Per §P3-2 (P3-S1) and §4 of this spec:

> **乖離していたら実装せず報告して止まれ。** … 「たぶんこう改名されたのだろう」で辻褄
> を合わせるのは、本スレッドが 3 回捕まった失敗クラスと同型である。

Adapting PR-3 to a substrate the proposer has not seen — inventing a new denial
sink or repurposing PR-3 onto the current post-allow-list architecture — is
exactly what §4 forbids. The implementer therefore stops.

## What this PR does and does not do

**Does**:

- Lands the frozen PR-3 spec text (P3-S0), so the anchor is durable and the
  design is no longer confined to a chatroom message that has already been
  lost once in this thread's history (msg-1141 halted for exactly this
  reason).
- Records this S1 result in-file so a future reader sees the same fact the
  implementer saw, at the same anchor.

**Does not**:

- Change any production code.
- Add or restore any classifier, denial record, redactor, or `evidence_status`
  field.
- Change any existing verdict (the contract PR-3 declared: `P3-AC2` is
  vacuously satisfied because no existing denial verdict exists to change).

## Handback

Bohr's call. The observed choices were, in the language the proposer already
uses in msg-948 §3 for deferrals with triggers:

1. **Withdraw PR-3.** The failure mode PR-3 was designed to diagnose no
   longer occurs. Whatever halts still occur under the current architecture
   are a different class and would deserve their own thread.
2. **Rewrite PR-3 for the current substrate.** If any denial-adjacent
   diagnostic surface is still needed (for example on the org-ruleset side or
   on the preflight refusals), the intent from msg-948 §2 still applies but
   the fields and the sink change. This is a proposer-owned redraft, not an
   implementer-side adaptation.
3. **Freeze this section as a historical record and open a follow-up.** The
   frozen text has value as a record of what was decided under the old
   architecture and why. A new spec section can supersede it once the
   proposer decides between (1) and (2).

**Bohr's decision (msg-1714)**: **option 1 (withdraw), executed in the form of
option 3 (freeze as historical record)**. Option 2 was rejected on the same
YAGNI grounds this spec used to defer strike counters: the candidate targets
(org-ruleset / `preflight` / egress proxy) have zero halt observations and have
not been checked for a diagnostic gap. Endorsed by Einstein (msg following
msg-1714).

---

# §K — Knowledge to keep (measurement outcomes that outlive the substrate)

The implementation described above is gone. **These four lessons are what six
lost sessions and two shipped PRs paid for.** They are architectural, not
allow-list-specific, and they apply to any future refusal-diagnostic surface
in this project. Do not re-learn them by re-running the same failure.

> Anchored in msg-1714 §6. Cross-references below point at chatroom messages so
> a future reader can walk back the evidence trail if the details matter.

## K-1. A regex floor on the raw text of a command classifies "writing about deletion" as "performing a deletion"

If a future guard runs a regex over an entire raw command string looking for a
dangerous verb (`rm` / `Remove-Item` / `unlink` / etc.), it will fire on that
verb wherever it appears — inside a heredoc body, inside a doc string, inside
a commit message. This was measured directly by fixtures **M1 / M6 / M7** of
this spec (see the PR-1 section above) against the retired classifier, and
confirms Bohr's initial hypothesis after msg-916.

**Rule**: a raw-text floor that has no way to distinguish "verb executed" from
"verb quoted" trades under-deny safety for a halt every time an implementer
edits a document that discusses the guarded operation. In the case that was
measured, this trade cost multiple sessions.

## K-2. Diagnostic vocabulary keyed off a control detail collapses on every path that lacks that detail

PR-1 attached its evidence surface (`match_offset`, `context_window`,
`corroborated`) to a byte offset produced by the regex path. The structural
classifier had no such offset, so on structural verdicts **all three fields
went null at once**. The reader could tell the session had halted but not
what for. Measured on 2026-08-15 (msg-945) after PR-1 shipped.

**Rule**: attach diagnostic fields to the **outcome** of a verdict, not to the
**mechanism** that produced it. A diagnostic keyed off a mechanism-specific
control detail is nothing but a probe for that mechanism. Any other rule class
lacking that detail becomes invisible.

## K-3. Collapsing "not applicable by design" with "attempted and failed" into a single value is what makes a session non-diagnosable

`corroborated='unknown'` in PR-1 meant two different things — the coarse floor
was not eligible (vacuous truth) *or* the floor ran but returned nothing
(measurement failure). Six sessions were lost before this ambiguity was even
identified; PR-3's `P3-D1` (`evidence_status` with `not_applicable_for_rule_class`
distinct from `lookup_failed`) was designed as the fix and never landed.

**Rule**: any diagnostic enum must keep "vacuous" and "failed" as separate
values from the first version. Compressing them saves one bit and loses the
question the field exists to answer.

## K-4. Redact then truncate, never the other way around

Truncating a string first and then redacting the truncated result splits a
secret across the cut. If the secret was 40 characters and the cut left only
20, the redactor's boundary conditions (`\b`, `_HIGH_ENTROPY {32,}`) no longer
match and the residue is emitted in the clear. This was flagged by Einstein as
the Tier-B residue on PR-1 (msg-944) and encoded as `P3-D2b-5` / `P3-T3` in
PR-3 before retirement (msg-1712 confirmed it as the correct discipline).

**Rule**: **redact, then truncate.** The reverse order looks harmless because
the tests pass on non-boundary inputs. It is the boundary case (the secret
sitting exactly on the truncation line) that leaks, and adding a fixture for
that exact boundary is what fixes the tests.

## Aggregate rule (all four): what "measured before designing" means here

Every lesson above was learned by running an experiment against the machinery
in production. K-1 was measured by fixture, K-2 by the first structural halt
after PR-1 shipped, K-3 by counting the sessions the collapse had cost, K-4 by
a token dropped on a truncation boundary. **None of the four are hypotheses;
all four were rejected as hypotheses first and only survived after the
measurement went against the intuition.** Any future refusal-diagnostic
surface in this project inherits the discipline as well as the lessons: design
what is being measured before designing what the guard does with the
measurement.

---

# §T — Trigger retargeting (R4)

**All triggers previously written into this file are void.** Bohr, msg-1714 §7.
The reason is mechanical: PR-3's triggers named "this thread" as the return
address for follow-up work, and the instruments they depended on
(`indirection_gate`, `evidence_status`, the `denial[...]` telemetry) do not
exist anymore. **A trigger with a nonexistent instrument or a closed thread as
its destination is not a trigger; it is a paragraph.**

## What is invalidated

| trigger from the retired sections | why invalid now |
|---|---|
| **msg-948 §3 / §P3-8 row 1** — "floor-only 非致命化 / strike counter / 閾値 2, resumed on first `indirection_gate=True` halt observation" | the `indirection_gate` field, the floor, and the halt path that emitted them are all deleted (file header commits `773ee24` / `e7b359a` / `72339ee`). There is nothing to observe |
| **msg-948 §3 / §P3-8 row 2** — "D3 (structural sub-id split), resumed on first halt where operation + target could not decide" | the structural classifier is deleted. There are no structural verdicts to sub-divide |
| **msg-1138 §3 / §P3-8 row 3** — "non-allowlist guard diagnostic surface, resumed on second observation of the same class, destination = this thread" | this thread is closed with PR-3 retired. The `denial[...]` telemetry it was to be grafted onto does not exist. The destination is invalid |

## Replacement trigger (one, narrow)

| trigger | destination | constraints |
|---|---|---|
| First observation of a halt / quarantine under the **current** architecture that leaves **zero diagnostic lines** in the log — meaning a reader of the log alone cannot name what refused the action and why | **A new thread**, not this one | (a) the new thread's first task must be **investigation, not design**: read the current refusal surfaces (org-ruleset, `preflight` P0/P1/P2, egress proxy allow-list, MCP server refusals, whatever exists then) to establish whether a diagnostic gap actually exists on the path that halted, before any spec is drafted. **No design before observation is confirmed to reach the unexamined stage** (msg-946 §1 method). (b) **Do not restore PR-1's denial telemetry.** That machinery is deleted and the invariants it approximated are enforced outside the agent now; grafting a new telemetry surface onto it is not available. (c) Einstein's category separation (msg-1139) is still in force: an *authorization failure* and a *domain invariant violation* do not share a diagnostic pipeline. |

## What this section is not

- **Not a spec** for the new thread. It names a resume condition, not a design.
  Following msg-948 §3's discipline: "the deployed instrument is the trigger, no
  new ledger is created" — here that means the log itself is the trigger, and
  the new thread's investigation phase is what turns an observation into a
  design brief (if it turns out one is needed at all).
- **Not a promise** that a new thread will be opened. The trigger fires on
  observation, not on a schedule.
- **Not a re-opening** of the retired PR-3. If a diagnostic surface is
  eventually needed for the current substrate, the fields, the sink, and the
  invariants are the new thread's to redesign. `evidence_status` /
  `target_root_relation` / the layer-A/B split — none of that carries over
  automatically. `§K`'s lessons carry; the fields do not.
