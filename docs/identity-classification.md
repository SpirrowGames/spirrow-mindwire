# Identity classification — primary-source read for the "role null must become impossible" epic

- **thread**: `T-role-null-must-become-impossible` (spirrow-mindwire chatroom)
- **spec source**: Bohr's msg-1179 §5, msg-1484 §5, msg-1493 §2 as corrected by msg-1585 §3 (`allowed_roles := legitimate`)
- **Tier-C**: approved 2026-08-24 (msg-1521 "Approve as-is; implement the read/write split PR now")
- **scope**: this file is the **read half's** §5 deliverable — classify each identity name this repo
  writes with, from **primary sources in this repo**, so a subsequent write half (in the identity
  store, not here) can supply `allowed_roles` **by construction** rather than by guess.

## Why classification is on the implementer

Bohr's msg-1179 §5, verbatim:

> **私はどちらとも決めない。** 判断材料は実コードにある — その post を書いているのは誰か
> (Gemini の critique を driver が転記しているのか、driver 自身の言葉か)、GitHub review artifact
> との関係はどうか。**実装者が一次照合して決め、理由を書くこと。**

The proposer refuses to guess between "machine" and "participant" because the honest answer sits in
the code that writes each post. That code is here, and this file is that read.

## The rule the classification feeds

From msg-1493 §2 **as corrected by msg-1585 §3** (the correction is the operative form):

> `allowed_roles := legitimate(§5 分類)`
> `residual := observed \ legitimate`
> `unused := legitimate \ observed`

**Legitimate** is this file (what may this identity honestly claim) and it alone decides the
entitlement — `allowed_roles` is what a future `upsert_identity` call MUST supply. **Observed** is
a live-corpus fact (what this identity has actually claimed since the cutoff) and it is *not* an
input to the entitlement: it feeds the two report-only sets. `residual` is exactly the evidence of
I-6-style fabrication the epic exists to surface (msg-1493 §3); `unused` is a right held but not
exercised.

msg-1493 §2's original `observed ∩ legitimate` is **withdrawn**. `chatroom_post_message` drops the
role of an author with no registered identity and records `null`, so `observed ⊆ allowed_roles`
already holds for every recorded role: the intersection could only ever *narrow* an existing
entitlement, never grant one. For an identity that is not yet registered — precisely the ones the
write half exists to register — `observed = ∅` is a certainty, so the intersection derived
`allowed_roles = ∅` for every one of them and the msg-1489 §4 biconditional then classified them as
machinery. Registration could not bootstrap (msg-1585 §1).

`legitimate = ∅` is the honest value for a **machine**; a subsequent `upsert_identity` MUST leave
`allowed_roles = []` (msg-1484 §2, Einstein endorsed msg-1485) and `independence_class = null`
(msg-1487 §4; its constituent §2 / §3 endorsed by Einstein msg-1488).

## The identity names this repo writes

Enumerated by grepping every `chatroom_post_message` and `chatroom_open_thread` call site in
`src/`. The following names appear as the `author` (post) or `owner` (thread) argument:

| identity_name                  | writer                                                                    | grep evidence                                                |
| ------------------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `naysayer-pr-review`           | `PrReviewOrchestrator.post_critique` (`orchestrator.py`)                  | `_DEFAULT_NAYSAYER_AUTHOR = "naysayer-pr-review"` (line 31)  |
| `orchestrator`                 | `PrReviewOrchestrator._open_thread` (thread `owner`, not a post `author`) | `_DEFAULT_OWNER = "orchestrator"` (line 30)                  |
| `pr-gate-relay`                | `Conductor._post_pr_relay` (`conductor/core.py`)                          | `_PR_GATE_RELAY_AUTHOR = "pr-gate-relay"`                    |
| `conductor-redirect-relay`     | `Conductor._post_redirect_relay` (`conductor/core.py`)                    | `_REDIRECT_RELAY_AUTHOR = "conductor-redirect-relay"`        |
| `spirrowgames-ops`             | `NaysayerPrReviewDriver` — GitHub review submission, not a chatroom post  | `naysayer_github_token` docstring, `pr_review.py`            |

`spirrowgames-ops` is a **GitHub identity**, not a magickit chatroom author, so it is out of scope
for this epic (which is about the `role` column on chatroom messages and the `allowed_roles`
column on chatroom-side identity records). It is listed for completeness so the enumeration is
not read as three-when-there-are-four; the classification below covers the three chatroom names.

There is also `conductor-probe` in `scripts/thread_heads.py`, which the module docstring says
"never posts and never marks read": it is a read-only inbox identity used to enumerate thread
heads. Since it is defined to never write, it does not appear in the observed-supply set the §2
derivation reads from. Listed for completeness; classification: **machine** (`allowed_roles = ∅`),
same reasoning as `pr-gate-relay` below.

## Classification

The two questions Bohr's §5 posed:

1. **participant** (the identity IS an LLM actor whose voice the post carries verbatim) →
   `legitimate = {role(s) the actor plays}`.
2. **machine** (the identity IS harness code; no LLM speaks under this name) →
   `legitimate = ∅` (`allowed_roles = []`, `independence_class = null`).

### `naysayer-pr-review` — **participant**, `legitimate = {naysayer}`

**Primary source** (`src/spirrow_mindwire/orchestrator.py`, `post_critique` inner function,
lines 279–314):

```python
async def post_critique(body: str) -> None:
    ...
    await self._call(
        "chatroom_post_message",
        {
            ...
            "author": self._naysayer_author,
            "content": body,
            # D-1 (T-dispatched-turn-gets-one-message). This is the Tier B
            # verdict — the single most gate-relevant message the harness
            # writes — and it recorded ``role: null`` 346 times out of 346
            # (live corpus, 2026-08-16). The claim is honest: this body IS
            # the independent naysayer's critique, relayed verbatim.
            #
            # Whether it RECORDS depends on ``self._naysayer_author`` being
            # a registered magickit identity with ``naysayer`` in its
            # allowed_roles; ...
            "role": Role.NAYSAYER.value,
        },
    )
```

The `body` argument to `post_critique` is the return value of `NaysayerPrReviewDriver.review` —
the independent naysayer's critique produced by a Lexora one-shot to Gemini (see `pr_review.py`
module docstring, lines 28–34, cite: "Only the adversarial *judgement* is delegated — to
Lexora's ``naysayer`` (Gemini) tier via **one-shot** ``chat_completion`` calls"). The orchestrator
is transport for that judgment; the JUDGMENT is the naysayer's own words.

Under Bohr's msg-1179 §5 wording (this identity classified as participant means "role must be
supplied, not erased"): `naysayer-pr-review` IS the identity of the independent-distribution
naysayer that produces the Tier-B verdict. `legitimate = {naysayer}`. Recording this identity as
machinery (`allowed_roles = []`) would erase the attestation of the most gate-relevant post the
harness writes — which Bohr's msg-1484 §5 forbids explicitly: "**`naysayer-pr-review` が
participant なら `[]` にしてはならない。それは Tier-B gate の verdict を「機械の発言」として
記録することになり、§3 と逆向きの捏造になる。**"

**Consequence for the write half**: `upsert_identity("naysayer-pr-review",
allowed_roles=["naysayer"], independence_class=<the value the T15 gradient assigns to a Gemini
tier participant>)`. The `independence_class` value MUST be non-null (the bidirectional invariant
of msg-1487 §3 / msg-1489 §4 requires `allowed_roles ≠ ∅ ⟺ independence_class ≠ null`). This repo does not own
the `independence_class` enum's SoT (ADR-2026-05-31-15); the write-half implementer must consult
the T15 gradient by primary source before selecting a value.

### `orchestrator` — **machine**, `legitimate = ∅`

**Primary source** (`src/spirrow_mindwire/orchestrator.py`):

- Line 30: `_DEFAULT_OWNER = "orchestrator"`.
- Line 187 (constructor): `owner: str = _DEFAULT_OWNER`.
- Line 496 (`_open_thread`): `"owner": self._owner` — this is the thread-metadata `owner` field
  on `chatroom_open_thread`, NOT an author on any post.

Grep confirms `_DEFAULT_OWNER` is only ever read as the thread-`owner` argument. `orchestrator`
never appears as an `author` on any `chatroom_post_message` call in `src/`. So no LLM speaks
under this name and no post's role stamp is ever set for it — it is a thread-metadata label the
harness stamps to identify who *opened* the ledger, not who authored anything.

**Consequence for the write half**: `upsert_identity("orchestrator", allowed_roles=[],
independence_class=null)`. Under the msg-1487 §3 / msg-1489 §4 bidirectional invariant, both
sides are set to their absence values together.

The 258/258 null count from PR #153's commit message (`orchestrator: 258/258 null`) refers to
`role` values on posts credited to this name — but grep shows no post site here. Those 258 posts
must be from a legacy code path (pre-refactor) or from a caller in a different repo; the read
half's `identity_findings.py` script MUST enumerate them from the live corpus and either (a)
confirm they are all writes by code no longer running, in which case the `legitimate = ∅`
classification stands, or (b) surface them as `residual > 0` findings for the write-half
implementer to reason about before registration. This is the "residual" mechanism from msg-1493
§3, applied.

### `conductor-redirect-relay` — **machine**, `legitimate = ∅`

**Primary source** (`src/spirrow_mindwire/conductor/core.py`, `_REDIRECT_RELAY_AUTHOR` and the
guard-(i) redirect branch of `_route` / `_post_redirect_relay`):

D-1 of `T-human-terminal-overuse` (msg-2540 §4): when the conductor's guard (i) redirects a
non-human, non-attested-naysayer handoff-to-implementer to the human terminal, it posts a
one-line factual observation of the routing event into the design thread. The observation
names who authored the intercepted turn, which carve-out was NOT taken (human / naysayer-attested),
what token the author wrote, and what the effective route is. Bohr's msg-2540 §2-5 established
that the relay's `NEXT:` follows author-based routing (non-implementer authors → `NEXT: <author>`
to unblock self-correction; implementer / self-nomination → `NEXT: human` because an implementer
author re-triggers guard (i) and any per-episode second relay → `NEXT: human` so a spinning author
cannot loop indefinitely at the loop's expense).

The body is the conductor's own framing of a routing event, not the words of any participant.
Same reasoning as `pr-gate-relay`: a role stamp here would fabricate the I-6 evidence the loop's
independence carve-outs exist to make meaningful. Two distinct machine identities (rather than
reusing `pr-gate-relay`) keep the two conductor write paths from blurring into one in
`identity_findings.py`: they are triggered by different structural events and their bodies say
different things, so a single reader can tell "the PR-gate verdict was relayed" from "guard (i)
intercepted a bad handoff" without inspecting the body.

`legitimate = ∅`.

**Consequence for the write half**: `upsert_identity("conductor-redirect-relay", allowed_roles=[],
independence_class=null)`.

### `pr-gate-relay` — **machine**, `legitimate = ∅`

**Primary source** (`src/spirrow_mindwire/conductor/core.py`, lines 619–636):

```python
result = await self._mcp.call_tool(
    "chatroom_post_message",
    {
        ...
        "author": _PR_GATE_RELAY_AUTHOR,
        "content": body,
        # No ``role`` here, deliberately (D-1 sweep, T-dispatched-turn).
        # The other two harness write paths now supply one; this relay does
        # not, because it holds no role. It is the conductor restating a
        # verdict the Tier B driver produced elsewhere, and the honest value
        # for "which role authored this" is none. Claiming ``naysayer``
        # because the content came from one would put a role stamp on a post
        # no reviewer wrote — manufacturing exactly the evidence the I-6
        # invariant exists to make meaningful.
    },
)
```

The comment is dispositive. The conductor states its own reasoning: this relay holds no role,
the honest value is none, stamping `naysayer` here would fabricate exactly the I-6 evidence the
gate exists to check. The `body` this posts is a re-statement, not a verbatim excerpt of the
naysayer's judgment (the naysayer's own verbatim critique goes out under
`naysayer-pr-review` per above). The relay's text is `f"PR-gate (Tier B independent naysayer) —
{pr_ref}\n\nVERDICT: {outcome.verdict.value} ...\n\n{outcome.body}\n\nNEXT: {nxt}"` — the
conductor's own framing wrapping the driver's outcome.

`pr-gate-relay` is therefore **machinery** — a mechanical transport that carries an outcome, not
an actor that produced one. `legitimate = ∅`.

**Consequence for the write half**: `upsert_identity("pr-gate-relay", allowed_roles=[],
independence_class=null)`. The 26/26 null count (PR #153 commit message) is honest and stays.

## What this classification does NOT decide

- **NOT this: `allowed_roles` for `naysayer-pr-review`.** This file decides it, and the value is
  `{naysayer}` — under the msg-1585 §3 correction the entitlement is `legitimate` and nothing
  else. What this file cannot contain is the *live-corpus* half: `residual`
  (`observed \ legitimate` — roles it has claimed but may not) and `unused`
  (`legitimate \ observed` — the entitlement it has not exercised). Both come from
  `scripts/identity_findings.py`. A non-empty `residual` is a finding the write half must reason
  about before registering (msg-1493 §3); a non-empty `unused` is reported and gates nothing.
- **`independence_class`'s exact value for `naysayer-pr-review`.** The T15 gradient (ADR-2026-05-31-15)
  is the SoT for that enum and lives in a repo this implementer cannot read (per
  `OBL-DECLARE-UNREADABLE`). Any value assigned here without reading T15 would be a guess.
- **Whether Prismind's `upsert_identity` API exists as-described in Bohr's spec.** The design
  references it by name and the write half depends on it, but no code in this repo calls it and
  no schema in this repo defines it. That reachability question is a write-half prerequisite.

## Requirement-vs-artifact table

| Spec requirement (msg-id, ¶)                                                                     | Reflected here?                                                                                        |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| msg-1179 §5: implementer classifies each identity by primary-source read, records reasoning     | Yes — three sections above, one per identity, each with quoted primary source                          |
| msg-1179 §5: three classified, "not all-machine by default"                                     | Yes — `naysayer-pr-review` = participant, other two = machine, with the divergence explained          |
| msg-1484 §2 / msg-1485: machinery uses `allowed_roles = ∅`, not a fabricated enum member         | Yes — the "consequence for the write half" bullets say `allowed_roles=[]` for both machine identities  |
| msg-1484 §5: `naysayer-pr-review` as participant means role MUST be supplied, not erased        | Yes — that exact quote is cited under `naysayer-pr-review`'s section                                   |
| msg-1493 §2 as corrected by msg-1585 §3: `allowed_roles := legitimate`                          | Yes — decided here; `legitimate` IS the entitlement, no live read needed to fix it                     |
| msg-1493 §3: residual ≠ ∅ ⇒ surface as finding, do NOT silently drop                            | Deferred to `scripts/identity_findings.py`                                                             |
| msg-1585 §3: `unused = legitimate \\ observed` reported, blast radius zero, outside the lock     | Deferred to `scripts/identity_findings.py` (`derivation.unused`, `totals.authors_with_unused`)         |
