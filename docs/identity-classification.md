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

`legitimate = ∅` is the honest value for a **machine**; its `upsert_identity` MUST carry
`allowed_roles = []` (msg-1484 §2, Einstein endorsed msg-1485). The `independence_class` it
carries is **not restated here**: see [§ Write half](#write-half--revised-by-msg-1704--msg-1706).
(The earlier text said `independence_class = null`. The live API rejects that value, as
msg-1703 §2 measured.)

## The four identity names this repo writes

Enumerated by grepping every `chatroom_post_message` and `chatroom_open_thread` call site in
`src/`. Four names appear as the `author` (post) or `owner` (thread) argument:

| identity_name          | writer                                                                    | grep evidence                                                |
| ---------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `naysayer-pr-review`   | `PrReviewOrchestrator.post_critique` (`orchestrator.py`)                  | `_DEFAULT_NAYSAYER_AUTHOR = "naysayer-pr-review"` (line 31)  |
| `orchestrator`         | `PrReviewOrchestrator._open_thread` (thread `owner`, not a post `author`) | `_DEFAULT_OWNER = "orchestrator"` (line 30)                  |
| `pr-gate-relay`        | `Conductor._post_pr_gate_relay` (`conductor/core.py`)                     | `_PR_GATE_RELAY_AUTHOR = "pr-gate-relay"` (line 136)         |
| `conductor-relay`      | `Conductor._post_as_conductor_relay` (`conductor/core.py`)                | `CONDUCTOR_RELAY_AUTHOR = "conductor-relay"` (module-level)  |
| `spirrowgames-ops`     | `NaysayerPrReviewDriver` — GitHub review submission, not a chatroom post  | `naysayer_github_token` docstring, `pr_review.py`            |

`spirrowgames-ops` is a **GitHub identity**, not a magickit chatroom author, so it is out of scope
for this epic (which is about the `role` column on chatroom messages and the `allowed_roles`
column on chatroom-side identity records). It is listed for completeness so the enumeration is
not read as four-when-there-are-five; the classification below covers the four chatroom names.

There is also `conductor-probe`, which `scripts/thread_heads.py` used (until
T-unread-correlated-count-scale moved that probe to `chatroom_list_threads`, which takes no identity)
as a "never posts and never marks read" inbox identity to enumerate thread heads. It is **retired**:
no code in this repository calls magickit under that name any more, so it has no entry in
`spec/identity/legitimate_roles.yaml`. That is deliberate rather than an omission — a chatroom post
authored as `conductor-probe` is therefore reported by `scripts/identity_findings.py` under
`unclassified_authors` (the signal that the write half must not proceed), whatever role it does or
does not claim. While the entry existed (`kind: machine`, `legitimate: []`), only a post that
claimed a role would have surfaced, as `residual`; a role-less post would have been absorbed.

## Classification

The two questions Bohr's §5 posed:

1. **participant** (the identity IS an LLM actor whose voice the post carries verbatim) →
   `legitimate = {role(s) the actor plays}`.
2. **machine** (the identity IS harness code; no LLM speaks under this name) →
   `legitimate = ∅` (`allowed_roles = []`; `independence_class`: see § Write half).

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

**Consequence for the write half**: `allowed_roles = ["naysayer"]`. The payload is built by
`src/spirrow_mindwire/identity/registration.py::build_upsert_identity_args` from this entry. The participant's `independence_class` is declared once, on the
entry in `spec/identity/legitimate_roles.yaml` (decided by msg-1704 §4 and endorsed by Einstein
msg-1705 / msg-1707).

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

**Consequence for the write half**: `allowed_roles = []`, payload built by
`src/spirrow_mindwire/identity/registration.py::build_upsert_identity_args` (see § Write half).

The 258/258 null count from PR #153's commit message (`orchestrator: 258/258 null`) refers to
`role` values on posts credited to this name — but grep shows no post site here. Those 258 posts
must be from a legacy code path (pre-refactor) or from a caller in a different repo; the read
half's `identity_findings.py` script MUST enumerate them from the live corpus and either (a)
confirm they are all writes by code no longer running, in which case the `legitimate = ∅`
classification stands, or (b) surface them as `residual > 0` findings for the write-half
implementer to reason about before registration. This is the "residual" mechanism from msg-1493
§3, applied.

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

**Consequence for the write half**: `allowed_roles = []`, payload built by
`src/spirrow_mindwire/identity/registration.py::build_upsert_identity_args` (see § Write half). The 26/26 null count (PR #153 commit message) is honest and
stays: a machine's post keeps `role = null` after the write half as well (msg-4902 §2).

### `conductor-relay` — **machine**, `legitimate = ∅`

**Primary source** (`src/spirrow_mindwire/conductor/core.py`, `CONDUCTOR_RELAY_AUTHOR` constant
and the guard-(i) redirect write-back path in `run()`):

The relay is the write-back for T-human-terminal-overuse D-1 (Bohr msg-2540, Einstein msg-2539
ACCEPT). When guard (i) redirects a design→implement handoff to the human terminal — a non-human,
non-attested-naysayer author nominated the implementer — the conductor writes back one observation
into the design thread so the head moves off the redirected `NEXT: <implementer>` token. Without
this write-back, head_skip Stage 1 does not SKIP (the token is not `human` or `none`), and the
loop bounces forever (measured 288 times across 5 threads before this landed, msg-2537 §4).

The relay's body is the conductor's own framing — a restatement of the routing verdict (which
carve-outs did NOT apply, what the redirect target is) — not any role's verbatim speech. Reusing
`pr-gate-relay` as the author would put a D-1 write-back into the same author bucket the PR-gate
verdict readers key on (`gate_records.RELAY_AUTHOR` narrows readers to that author for exactly this
noise-rejection reason, module docstring line 34–41), silently blurring two distinct facts. So a
separate machine identity is registered here (Einstein msg-2539 Obj-1, Bohr msg-2540 §1 fix).

`conductor-relay` is therefore **machinery** — same reasoning as `pr-gate-relay`. `legitimate = ∅`,
`kind = machine`. Giving this identity a role would violate the I-6 invariant that both machine
entries above document (msg-2540 §1-4 explicitly pins that constraint: the loader hard-rejects
`kind=machine` with a non-empty `legitimate` list, and giving the relay a role to route around that
would fabricate exactly the evidence the invariant exists to make meaningful).

**Consequence for the write half**: `allowed_roles = []`, payload built by
`src/spirrow_mindwire/identity/registration.py::build_upsert_identity_args` (see § Write half). No live-corpus count yet — this identity is registered by T-human-
terminal-overuse before its first post, so `residual` starts empty; the first `identity_findings`
run after the landing will confirm that (or surface unexpected earlier writes, which would be a
live-corpus finding not a spec change).

## What this classification does NOT decide

- **NOT this: `allowed_roles` for `naysayer-pr-review`.** This file decides it, and the value is
  `{naysayer}` — under the msg-1585 §3 correction the entitlement is `legitimate` and nothing
  else. What this file cannot contain is the *live-corpus* half: `residual`
  (`observed \ legitimate` — roles it has claimed but may not) and `unused`
  (`legitimate \ observed` — the entitlement it has not exercised). Both come from
  `scripts/identity_findings.py`. A non-empty `residual` is a finding the write half must reason
  about before registering (msg-1493 §3); a non-empty `unused` is reported and gates nothing.
- **`independence_class` values.** This file originally left them open. The write half now
  settles them; see § Write half.
- **Reachability of `upsert_identity`.** Since answered: magickit exposes `upsert_identity`
  and `get_identity` on the MCP surface this repo already uses (live `tools/list`, measured
  at the start of PR-B).

## Write half — revised by msg-1704 / msg-1706

This section supersedes every `independence_class = null` the read half wrote above. The live
enum rejects null (msg-1703 §2). Einstein's msg-1707 endorsed the design:

- **Payloads are not restated in this doc.** Every `upsert_identity` argument set is built by
  `src/spirrow_mindwire/identity/registration.py::build_upsert_identity_args`, the only constructor of those arguments in this repo, from the entry in
  `spec/identity/legitimate_roles.yaml`. The machine value is one named constant
  (`MACHINE_INDEPENDENCE_CLASS` in `identity/classification.py`), added to Prismind by
  ADR-2026-08-25-20. The participant value lives on its YAML entry. The reason for this
  arrangement (msg-1706 §2) is that a doc that restates a payload can drift into prescribing a
  value the API rejects. A doc that names the constructor cannot.
- **Guard (both directions, all in mindwire; msg-1706 §1).**
  `registration.py::check_identity_against_classification` requires:
  `kind = machine` ⟹ `allowed_roles = []` **and** `independence_class = machine`;
  `kind = participant` ⟹ `allowed_roles ≠ []` **and** `independence_class ≠ machine`.
  The participant side is intentionally left open, not a closed set.
  Prismind enforces none of this, because it does not own the role vocabulary
  (ADR-2026-05-29-10). The guard runs on every outgoing payload, on the read-back after
  registration, and on the live store in `scripts/identity_findings.py` (the `store` block,
  which is the msg-1706 §4 tamper check).
- **Registration** is `scripts/register_identities.py --apply`. Deployment of the `machine`
  value is verified only by that run's live result. A `success=False` stops the run. No local
  copy of Prismind's enum exists in this repo, whether as a tuple or an import (msg-1706 §2 / DoD 4).
- **Forward effect (msg-1704 §5).** `naysayer-pr-review` is registered with
  `independence_class = independent`. Today this changes no behaviour, because the naysayer
  allowlist is config. Once the planned magickit reader that treats
  `independence_class == "independent"` as naysayer lands, this identity is promoted to a
  naysayer identity next to Einstein. Whoever lands that reader should decide with that effect
  in view.

## Requirement-vs-artifact table

| Spec requirement (msg-id, ¶)                                                                     | Reflected here?                                                                                        |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| msg-1179 §5: implementer classifies each identity by primary-source read, records reasoning     | Yes — four sections above, one per chatroom identity, each with quoted primary source                  |
| msg-1179 §5: three classified, "not all-machine by default"                                     | Yes — `naysayer-pr-review` = participant, other three = machine, with the divergence explained        |
| msg-1484 §2 / msg-1485: machinery uses `allowed_roles = ∅`, not a fabricated enum member         | Yes — the "consequence for the write half" bullets say `allowed_roles=[]` for every machine identity  |
| msg-1484 §5: `naysayer-pr-review` as participant means role MUST be supplied, not erased        | Yes — that exact quote is cited under `naysayer-pr-review`'s section                                   |
| msg-1493 §2 as corrected by msg-1585 §3: `allowed_roles := legitimate`                          | Yes — decided here; `legitimate` IS the entitlement, no live read needed to fix it                     |
| msg-1493 §3: residual ≠ ∅ ⇒ surface as finding, do NOT silently drop                            | Deferred to `scripts/identity_findings.py`                                                             |
| msg-1585 §3: `unused = legitimate \\ observed` reported, blast radius zero, outside the lock     | Deferred to `scripts/identity_findings.py` (`derivation.unused`, `totals.authors_with_unused`)         |
