"""``NaysayerPrReviewDriver`` — the independent PR-diff code-review gate as a *driver* (ADR-19 N-1).

ADR-2026-06-04-19 (driver-化 unify, decided ``T-naysayer-unify-impl`` msg-430/433): the naysayer
is a single judging-**behavior** SOT with surface-specific transports. The design-time naysayer
is the loop's :class:`~spirrow_mindwire.adapters.naysayer_sdk.NaysayerSdkAdapter` (the sole
registry ``NAYSAYER_QUALIFIED`` adapter); the develop→main PR-review gate (Tier B, ADR-07 §2.2)
is **no longer a RoleAdapter** — it is this **driver**, invoked directly by
:class:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator` (no watcher/dispatch round-trip).

What stays in **code** (the driver), not the LLM (D-7 / ADR-16):

1. the **L1 CI-gate** (ADR-16 §D-2): a non-green CI short-circuits to REQUEST_CHANGES / COMMENT
   *before* the costly content review — fail-closed (failure / pending / UNKNOWN never APPROVE);
2. the **injection-safe verdict parsing** (last standalone ``VERDICT:`` line, anchored at column
   zero so no diff hunk line — ``+``, ``-`` *or* the space-prefixed context line — can supply one;
   never APPROVE on a truncated / length-capped review);
3. the **T22 GitHub-review submission** as the separate ``spirrowgames-ops`` identity, with the
   same-identity 422 → COMMENT fallback;
4. the **A-3 two-pass ADR-pointer structure** (spec-thread ``T-pr-gate-adr-index-scope``
   msg-692..705): pass 1 = index-less verdict pass (unchanged behaviour, sole source of the
   ``VERDICT:`` line); pass 2 = index-injected, JSON-only ADR-pointer collection whose output is
   rendered as a NON-BLOCKING hints section and cannot alter the verdict. The verdict-safety of
   pass 2 is a structural invariant, not a prompt guardrail — the driver never reads a verdict
   token from pass-2's return value (msg-692 §2). All definitions of ``pointer`` / ``N`` / ``p``
   / ``d`` / marker / target-driver live in :mod:`.pr_review_adr_pointers`, not in chat history
   (Einstein condition, spec msg-822).

Only the adversarial *judgement* is delegated — to Lexora's ``naysayer`` (Gemini) tier via
**one-shot** ``chat_completion`` calls (``transport != judge``: spinning up an Agent SDK loop to
read a static diff would be YAGNI — msg-430/432; calling ``chat_completion`` TWICE in parallel is
transport, not an Agent loop). The 5-principles SOT is injected verbatim via the SAME
:func:`~spirrow_mindwire.naysayer.principles.build_preamble` entry point the design-time agent
uses (ADR-17 D-1) — that single judging-behavior core unifies the two surfaces; the transport is
optimised per surface.

Fail modes (ADR-07 §2.6, fail-closed): Lexora **or** GitHub unreachable on pass 1 → the call
raises (the caller fail-loud); an empty pass-1 Lexora reply is likewise fail-loud. Pass 2 is
fail-OPEN (msg-694 §3-3): any failure (timeout / HTTP error / cancellation / parse-fail /
oversize) collapses to ``ADR-INDEX: unavailable`` on the marker so pass 1's verdict + posting +
GitHub submission never depend on pass 2 succeeding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..github.client import (
    CiState,
    CiStatus,
    GitHubClient,
    GitHubHTTPError,
    GitHubReviewClient,
    PrRef,
    ReviewEvent,
    ReviewInfo,
    naysayer_github_token,
)
from ..lexora.client import (
    LEXORA_BACKEND_TIMEOUT_SECONDS,
    ChatMessage,
    LexoraChatClient,
    LexoraClient,
    LexoraTimeoutError,
)
from .pr_review_adr_pointers import (
    AdrPointerSelection,
    append_marker,
    available_log_line,
    build_pr_review_pass1_system_prompt,
    build_pr_review_pass2_messages,
    load_manifest_ids,
    select_adr_pointers,
    strip_wrapping_fences,
    unavailable_log_line,
)
from .principles import (
    NAYSAYER_MODEL_TIER,
    PrinciplesError,
    objection_classes,
    principles_version,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = NAYSAYER_MODEL_TIER  # N-4: pinned in one place (naysayer.principles)
# Gemini 3.1 Pro (the current naysayer tier) is a reasoning model with a 1M+ token context: its
# reasoning tokens count against the OUTPUT budget, so a real review of a large diff spends a big
# slice on reasoning before emitting the critique + VERDICT line. The old 16000 ran out mid-review
# on big PRs (finish_reason=length, which decide_verdict then forces to REQUEST_CHANGES — a false
# RC); 32000 leaves room for reasoning plus a complete critique (connector-relay confirmed ~30k is
# enough in practice).
_DEFAULT_MAX_TOKENS = 32000
# M3 (T34): the CLIENT timeout must be backend + margin so the client always outlives the backend:
# the backend therefore surfaces its result (completion / partial / error) as the response, and we
# never time out *before* it does (the old equal-900s tie could lose that race, producing a
# client-side TimeoutException with no backend answer). On a genuine timeout the driver degrades
# to a COMMENT-hold addressed to the human (T-infra-failure-posts-empty-rc, see
# :meth:`_degrade_on_timeout`); the margin is about *who reports the timeout*, not safety.
# The backend fact (900s) is the SINGLE source of truth in ``lexora/client.py`` — imported here as
# ``LEXORA_BACKEND_TIMEOUT_SECONDS`` rather than re-hardcoded, so the two files cannot drift.
_CLIENT_TIMEOUT_MARGIN_SECONDS = 60.0
_DEFAULT_TIMEOUT_SECONDS = LEXORA_BACKEND_TIMEOUT_SECONDS + _CLIENT_TIMEOUT_MARGIN_SECONDS
# The REVIEWABILITY gate, not a context-capacity limit: this is the largest RAW diff the naysayer
# fully reviews and can therefore APPROVE. Beyond it the diff is truncated, and decide_verdict
# force-RCs a truncated review ("too big to review thoroughly in one shot — split the PR"). So the
# cap defines "small enough to review rigorously in a single pass", NOT "small enough to fit the
# model's context". The old 60_000 chars (~15-20k tokens) was below real PRs (e.g. #93 ~127k chars),
# so legitimate PRs got truncated → false RC. 150_000 chars covers the largest real PR seen so far
# (#93 ~127k) with margin while — given a fine task-splitting discipline (keep PRs small) — keeping
# the reviewability gate tight: a diff beyond this should have been split, so it truncates →
# force-RC. The truncate-then-never-APPROVE path is KEPT as the safety valve: a diff too big to see
# in one shot force-RCs rather than rubber-stamping on a partial view.
_MAX_DIFF_CHARS = 150_000

# Fraction of ``_MAX_DIFF_CHARS`` at which the gate STARTS warning the human that the reviewability
# cliff is approaching (T-gate-silently-suppresses-approve-on-truncated-diff). Chosen as a RATIO,
# not an absolute margin: an absolute value ties the warning band to the current cap size (so a
# future revisit of ``_MAX_DIFF_CHARS`` would silently invalidate the warning discipline); a ratio
# survives cap changes with its meaning intact. 0.8 leaves two full rounds of typical growth
# (msg-1872 measured ~10-15k/round on #182) inside the warning band before the cliff — enough
# lead time to split the PR while it still parses cleanly, which is not enough at a one-round
# margin (splitting is not a one-round activity, so a one-round warning is essentially "you have
# already lost"). This is a one-sample calibration and it may change; that is why the reason is
# recorded here rather than left implicit.
_DIFF_WARN_RATIO = 0.8
_DIFF_WARN_THRESHOLD = int(_MAX_DIFF_CHARS * _DIFF_WARN_RATIO)

# T22: the GitHub login the naysayer submits reviews under (= the review-side identity). The
# debounce counts only reviews by this login (other reviewers — Copilot, the author — are ignored).
_DEFAULT_REVIEW_LOGIN = "spirrowgames-ops"

# GitHub review states that carry an actual naysayer verdict (= a Gemini-spent review round).
# COMMENTED / DISMISSED / PENDING are NOT verdicts — a CI-gate hold, a dismissed review, or the
# escalation COMMENT itself spent no review — so the debounce (head-unchanged reuse + the round cap)
# counts only these. Counting non-verdicts would let a comment-only interaction prematurely, and
# then permanently, escalate the gate (Copilot + independent naysayer review on PR #113).
_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED")

# A verdict must be its own line, starting at COLUMN ZERO (``^...$`` with MULTILINE, and no
# leading-whitespace class after ``^``).
#
# The anchor was ``^\s*`` until 2026-08-16, above a comment asserting that a ``VERDICT: APPROVE``
# *inside the reviewed diff* could never satisfy it because diff hunk lines carry a +/-/space
# prefix. That was true for ``+`` and ``-`` and FALSE for the third: ``\s`` matches a space, and a
# unified-diff CONTEXT line is prefixed with exactly one space — the most common line kind in any
# diff. So the one defence the comment named did not cover the one case that mattered.
#
# ``matches[-1]`` (below) does not rescue it: last-wins only helps while the injected copy comes
# BEFORE the model's real verdict. A model that states its verdict and then quotes the offending
# hunk — a normal thing for a reviewer to do — puts the injected APPROVE last.
#
# Why column zero and not "a little indentation": every leading-space allowance re-admits the
# context line, because the context prefix IS one space. ``^[ \t]{0,3}`` would have changed
# nothing. The choice is therefore binary, and the measurement settles it — sweeping every PR of
# the four Spirrow repos for reviews authored by ``spirrowgames-ops`` (2026-08-16: 499 review
# bodies, 413 plain verdict lines) found the verdict at column 0 in 413 of 413; indented ones do
# not occur. The driver's own short-circuit bodies (CI-gate, debounce, timeout-degrade) likewise
# render theirs at column 0.
#
# The failure direction is safe by construction: an unmatched verdict is not an open gate but no
# verdict at all, and _parse_model_verdict returns UNPARSEABLE which decide_verdict maps to a
# fail-closed REQUEST_CHANGES gate. A model that someday indents its verdict costs a red gate,
# not a false APPROVE.
#
# This pattern is coupled to _PR_REVIEW_SYSTEM_PROMPT, which must teach the exact shape accepted
# here — narrowing one without the other makes the gate refuse the form its own instructions
# prescribe. The prompt's exemplar was indented two spaces AND carried a trailing parenthetical,
# so it did not parse under this anchor (the indent) nor under the old ``^\s*`` one (the
# parenthetical, which ``\s*$`` will not cross). Both are fixed, and
# test_system_prompt_verdict_exemplar_is_accepted_by_the_parser asserts the prompt text itself
# against this pattern so the two cannot drift apart again silently.
#
# ---- What this anchor does NOT buy (do not repeat the mistake this comment replaced) ----
#
# It makes VERBATIM diff text inert, because a hunk line keeps its +/-/space prefix and so cannot
# begin at column 0. That is the whole of it. It is NOT immunity to injection. A model that
# RE-TYPES an injected line without the prefix — most plausibly by quoting it inside a fenced
# block — emits a genuine column-0 match, and ``matches[-1]`` (last-wins) only covers that while
# the quote comes BEFORE the model's own verdict. A quote placed after it still wins; measured
# 2026-08-16, not hypothesised. That residual is recorded rather than fixed here: closing it means
# deciding what may legitimately surround a verdict line (fences, quoting rules), which is a
# design question, not a regex tweak.
#
# While it stays open, the one mitigation that IS available is to deny the exploit its string: keep
# column-0 ``VERDICT: APPROVE`` out of the sources this gate reads and out of the prompt it is
# given. See the note above _PR_REVIEW_SYSTEM_PROMPT.
#
# ---- Bold verdicts fail closed, deliberately (ruling: T-verdict-regex-space-prefix-injection) ----
#
# ``**VERDICT: APPROVE**`` does not match this pattern, so _parse_model_verdict returns
# UNPARSEABLE and decide_verdict maps that to a fail-closed REQUEST_CHANGES gate.
# That is a CHOICE, not an oversight. The same sweep found 9 bold verdict lines (spirrow-mindwire
# #69/#71/#72/#73/#74, Spirrow-VoxelWorld #51); of the 4 that read APPROVE, none was ever submitted
# as an APPROVED review — the gate is not known to have opened on one. Reasons to leave it strict:
# the failure direction costs one redundant red round, no current driver output takes the bold form
# (413 of 413 are plain), and widening the accepted shape is what lets quoted text be read as an
# assertion.
#
# The reversed twin — read this before "making the two consistent":
#
#   layer 2 (``NEXT:`` handoff parsing, PR #151)   this line (``VERDICT:`` gate parsing)
#   ------------------------------------------    -------------------------------------
#   ``**NEXT: X**`` is accepted (tolerant)        ``**VERDICT: X**`` is rejected (strict)
#   miss  -> loop halts SILENTLY and waits on     miss  -> one extra red round; loud and cheap
#            a human who has no signal to look
#   over-match -> one wasted turn                 over-match -> the gate OPENS on text the model
#                                                              may merely have quoted
#
# Same surface defect (markdown emphasis defeats a line parser), opposite damage asymmetry, hence
# opposite treatment. This is not an inconsistency to be tidied up. Revisit only when a FALSE RED
# is actually observed here — a review body whose bold verdict forced a REQUEST_CHANGES the author
# did not intend.
#
# ---- Divergence back-reference (R-4a, rider-3 msg-2130 §1) -------------------------------------
# This regex is used with a LAST-WINS anchor (see ``_parse_model_verdict``: ``matches[-1]``).
# The objection-block parser next to it (:func:`parse_objections`) uses STRICT-SINGLE (D-1):
# two column-zero markers derive MISSING rather than picking one. The two parsers therefore
# disagree on how to react to a column-zero echo — deliberately.
#
# Whether the last-wins discipline here should follow the objection parser to strict-single is
# an open question under ``T-verdict-echo-after-real-verdict`` (msg-1979); rider 3 (msg-2072 §5,
# discharge in msg-2130 §1) chose the "explicit-justification" branch of R-4 rather than the
# "unify" branch, so the divergence is named on BOTH sides — here and in ``parse_objections`` —
# to keep a future reader from making it consistent by touching this line prematurely. This note
# is DESCRIPTIVE: it records that a decision is pending, not which way it should go.
# ------------------------------------------------------------------------------------------------
_VERDICT_RE = re.compile(
    r"^VERDICT:\s*(APPROVE|REQUEST[ _-]?CHANGES|COMMENT)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# ---- The exemplar below is REQUEST_CHANGES on purpose. Do not "restore the symmetry". ----
#
# There is deliberately no column-0 ``VERDICT: APPROVE`` anywhere in this file — nor anywhere under
# ``src/``. Such a literal is an exploit string for the residual recorded above _VERDICT_RE (a
# quote placed AFTER the model's own verdict wins), and it reaches the parser through two channels
# that both close only by the string not existing:
#
#   * this file is reviewed BY this gate, so the literal rides in every diff that touches it, and
#     re-typing it out of the diff (dropping the +/space prefix) is the ordinary way to discuss it;
#   * the prompt is handed to the model on every review, so a model that restates its own
#     instructions emits the line with no diff involved at all.
#
# An exemplar teaches the SHAPE of the line, not which conclusion to reach. REQUEST_CHANGES teaches
# the identical shape under the identical anchor, and if it is ever echoed it lands on the
# fail-closed side: "quoting this opens the gate" becomes "quoting this turns it red". Measured
# 2026-08-16 on the previous exemplar: a body reading ``VERDICT: REQUEST_CHANGES`` (the model's
# own) followed by a quote of the APPROVE exemplar parsed as APPROVE.
#
# The APPROVE form is therefore described in prose rather than shown. Pinned by
# test_no_src_file_teaches_a_column_zero_approve_verdict (scans ``src/`` with _VERDICT_RE itself)
# and test_quoting_the_prompt_exemplar_cannot_open_the_gate (replays the echo).
#
# ---- Objection classes (T-naysayer-blocking-bar-undefined Stage 1, msg-2031 / msg-2033) ----
#
# SUPERSEDES the Stage 0 affordance (PR #188, merged as 043d3b9) that this same paragraph used to
# describe. Stage 0 gave the reviewer a prose slot for non-blocking observations; Stage 1 replaces
# that slot with a NAMED class per objection, taken from the ``objection_classes`` map in the
# principles frontmatter, plus a machine-readable block the driver parses. The Stage 0 wording is
# not kept alongside it — two ways to say "this one is a nit" is the dual-management defect the
# class system exists to remove.
#
# Three properties of the wording below are load-bearing:
#
#   * It does NOT enumerate the class names. ``build_preamble()`` already injects the frontmatter
#     verbatim into this same system prompt, so an enumeration here would be a second copy of the
#     enum that can drift from the SOT. The absence is pinned by
#     ``test_no_src_file_duplicates_the_objection_class_vocabulary``.
#   * Its objection-block exemplar uses PLACEHOLDER class names, not real ones. The exemplar is
#     handed to the model on every review, so a model restating its instructions emits it; a
#     placeholder parses as ``UNKNOWN``, which :func:`derive_verdict` counts as blocking. The echo
#     therefore lands on the fail-closed side — the same reasoning that makes the VERDICT exemplar
#     a REQUEST_CHANGES (see the note above _PR_REVIEW_SYSTEM_PROMPT).
#   * It adds NO new column-0 ``VERDICT:`` line, so the column-0 APPROVE ban and the quoted-echo
#     defence are untouched.
#
# What Stage 1 deliberately does NOT do: change any verdict. The parsed block feeds
# ``VerdictDecision.derived_verdict``, which no gate path reads (see the SHADOW note there).
#
# The problem this addresses (Bohr, msg-1923 §1): "blocking" had no definition anywhere, so the
# same class of 1-2-line prose defect landed on opposite sides of the verdict in adjacent rounds
# of one PR (#186 R6 = APPROVE, R7 = REQUEST_CHANGES), and a third case in another repository
# (spirrow-verimend#3 R3) cost the loop a round for a defect that misleads nobody at runtime.
#
# The prompt below now names "blocking" (the property that forces REQUEST_CHANGES) and gives the
# reviewer a place to record NON-blocking observations (nits) alongside blocking objections. This
# closes an asymmetry Bohr diagnosed on the same thread that produced this change: the previous
# prompt had a non-blocking slot only on the APPROVE side ("name the single weakest remaining
# point"), so the ONLY place a reviewer could record a nit was inside an APPROVE body — and the
# moment a nit was weighed as material, the reviewer had no shape in which to say "one blocking
# problem, plus three nits", so the pressure went straight to REQUEST_CHANGES. Measured on PR #186
# rounds 6 and 7 (msg-1923 §1): the same class of 1-2-line prose defect landed on opposite sides
# of the verdict in adjacent rounds. This is the Stage 0 half of the response (D-3 in msg-1926 /
# msg-1930 §4): purely additive prompt formatting, NO change to verdict semantics — the gate still
# posts REQUEST_CHANGES iff the model wrote it, and the advisory section (if any) is prose that
# rides in the body next to the blocking objections. The Stage 1 half (per-objection ``class`` +
# code-side ``VERDICT`` derivation) is the Tier-C decision that follows this one — a bump of
# ``spec/NAYSAYER_PRINCIPLES.md`` ``version: 1 → 2`` — and is deliberately NOT done here.
#
# What this affordance does NOT do, and why the wording is careful:
#
#   * No "blocking" / "advisory" enum is introduced in the prompt beyond the existing 6-item list of
#     what qualifies as "the real problems" (correctness / edge / security / invariant / untested /
#     regression). Fixing the taxonomy is Stage 1's job; anticipating it here would either fossilise
#     a wording that Stage 1 revisits or (worse) drift from whatever Stage 1 settles on.
#   * No new column-0 ``VERDICT:`` line. The exemplar remains the single REQUEST_CHANGES line, so
#     test_system_prompt_verdict_exemplar_is_accepted_by_the_parser stays green and neither the
#     column-0 APPROVE ban nor the quoted-echo defence introduced above is weakened.
#   * "You may additionally" — permissive, not mandatory. A blocking objection alone still counts
#     as a complete review; the affordance exists to remove pressure, not to add a checklist item.
_PR_REVIEW_SYSTEM_PROMPT = """\
You are the independent naysayer performing adversarial CODE REVIEW of a pull \
request's diff in a Spirrow MindWire ChatRoom thread. You are a different model \
from the implementer. Assume the change is flawed until proven otherwise and \
find the real problems.

What kinds of problem exist, and which of them force a change before merge, are \
defined by the `objection_classes` map in the frontmatter of the naysayer \
principles above. That map is the only list of class names; this prompt does not \
repeat it. A class with `blocks: true` is one whose fix the implementer must \
make before merge, and its `evidence:` line states what you must be able to say \
to raise it — if you cannot say that, you have not established a blocking \
objection. A class with `blocks: false` is a real observation that does not \
force a change before merge; record those too, so that noticing one never \
pressures you into inflating it into a blocking objection.

For every objection, quote the specific hunk/line you object to and explain the \
concrete flaw. Do not fabricate problems and do not pad with generic caveats. \
If, after a genuine search, you find no blocking problem, say so and name the \
single weakest remaining point.

Then, at the very END of your reply and immediately BEFORE the verdict line, \
emit exactly one machine-readable objection block: the marker below at the \
start of its own line, followed by a single JSON array — no code fence, no \
prose between them.

<!-- mindwire:objections v1 -->
[{"class": "<a class name from objection_classes>", "where": "path:line", \
"evidence": "<what that class's evidence: line asks for>"}]

One element per objection you stated above, in the same order; `[]` if you \
stated none. Every objection you made in prose must appear, and the block must \
not add any you did not make. The block records what you already wrote — it \
does not replace the prose, and nothing in it changes the verdict line below.

End your reply with exactly one verdict line, in exactly this form — at the start of the line, \
holding nothing else (no indentation, no bold or backticks, no trailing note):

VERDICT: REQUEST_CHANGES

Write that line verbatim if you found at least one blocking problem. If, after a genuine search, \
you found none, write the same line with the single word APPROVE in place of REQUEST_CHANGES. \
Those two are the only verdicts, and nothing else may appear on the line. Non-blocking advisory \
observations, if any, belong in the body ABOVE this line — never on the verdict line itself.

Your reply is posted verbatim to the thread and submitted as your GitHub PR \
review body — reply directly with the review, no preamble.
"""

# The driver posts its critique to the review thread via this callback (supplied by the
# orchestrator), so the chatroom transport stays out of the judging core.
PostCritique = Callable[[str], Awaitable[None]]


class NaysayerPrReviewError(RuntimeError):
    """A PR review could not be completed (empty model reply, etc.) — fail-loud."""


@dataclass(frozen=True)
class PrReviewOutcome:
    """The result of a single PR review (returned to the caller for logging / the gate)."""

    verdict: ReviewEvent
    body: str
    ci_state: CiState
    head_sha: str | None
    truncated: bool = False
    finish_reason: str | None = None
    model: str | None = None
    principles_version: int | None = None
    ci_gated: bool = False  # True when the L1 CI-gate short-circuited the content review
    timed_out: bool = False  # M2 (T34): the Lexora review timed out → degraded to fail-closed RC
    skipped_head_unchanged: bool = False  # debounce: re-review skipped (head unchanged since last)
    rounds_capped: bool = False  # debounce: review-round cap hit → COMMENT escalation to human
    would_skip_head_unchanged: bool = False  # shadow: would-skip (head unchanged), measured
    would_cap: bool = False  # shadow: would-cap (cap hit), measured not acted
    # Pass-2 outcome (A-3 two-pass structure). Always set on every path that renders a
    # body (including CI-gate, debounce, timeout-degrade), so a caller inspecting the
    # outcome can locate the marker's ``pointers=<p>, dropped=<d>`` counters even when
    # the body was rendered by a short-circuit path. ``None`` iff the driver never
    # attempted pass 2 (e.g. a short-circuit path where the marker is stamped as
    # ``unavailable`` via a pre-built selection; see the code paths that use
    # :func:`_unavailable_selection`).
    adr_pointer_selection: AdrPointerSelection | None = None


# ─── T-gate-silently-suppresses-approve-on-truncated-diff ────────────────────────────
# Model verdict, gate verdict, and the gate NOTICE that makes them audible.
#
# Before this change: the gate's own decision (force-RC on a partial review) was invisible
# — the GitHub review body carried the model's ``VERDICT: APPROVE`` verbatim while the
# review state was ``CHANGES_REQUESTED``, and no channel said why. Measured on #182 (msg-
# 1871): R10 and R12 both wrote APPROVE and both were recorded RC; the seven rounds
# between them were spent by the implementer chasing non-existent findings. This block
# is the record layer that makes that history impossible to repeat.
#
# The single design rule that pays for the complexity: MODEL FACT and GATE FACT live at
# the same call site. ``VerdictDecision`` holds both plus the DiffView they were resolved
# against; ``render_gate_notice`` reads from that one object; the driver stamps the notice
# onto the same body that goes to both the chatroom relay and the GitHub review. No
# second source of truth, no reparse, no chance for the two channels to disagree.
# ─────────────────────────────────────────────────────────────────────────────────────


class ModelVerdict(Enum):
    """The verdict the MODEL stated (independent of what the gate decides to post).

    ``UNPARSEABLE`` is a first-class value, not a silent collapse into ``REQUEST_CHANGES``:
    when the two are indistinguishable, "the model said RC" and "the model said something
    the parser could not read" get the same notice, and the C-suppressed clause would
    then have to name overrides that never happened. The three-way distinction lets
    :attr:`VerdictDecision.suppressed` name precisely one thing — APPROVE overridden to
    RC — and lets the notice header state the model's actual answer honestly.
    """

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    UNPARSEABLE = "unparseable"


def _parse_model_verdict(critique: str) -> ModelVerdict:
    """Extract the model's stated verdict; three-way (APPROVE / REQUEST_CHANGES / UNPARSEABLE).

    The injection-safe parse (last standalone ``VERDICT:`` line at column zero — see the
    :data:`_VERDICT_RE` block for the anchor rationale, and note that last-wins on its own
    is no defence when a quote comes after the verdict) lives here as the ONE source. Every
    production caller consumes this through :class:`VerdictDecision` via :func:`decide_verdict`
    — no separate two-way projection exists anymore (round-5 PR-gate finding on PR #186
    msg-1890 removed the dead ``_parse_verdict`` wrapper along with its callers).

    UNPARSEABLE is a first-class return value, not a silent collapse to REQUEST_CHANGES:
    "the model wrote an unreadable verdict" and "the model wrote a REQUEST_CHANGES" are
    different facts that the gate notice header must report honestly (see the
    :class:`ModelVerdict` docstring for why the three-way distinction matters).
    """
    matches = _VERDICT_RE.findall(critique)
    if not matches:
        return ModelVerdict.UNPARSEABLE
    token = re.sub(r"[ _-]", "_", matches[-1].upper())
    if token == "APPROVE":
        return ModelVerdict.APPROVE
    # COMMENT / REQUEST_CHANGES / anything else the model wrote → same "objection" bucket.
    return ModelVerdict.REQUEST_CHANGES


# ─── Objection classes (T-naysayer-blocking-bar-undefined Stage 1) ───────────────────
#
# The model tags each objection with a class from the ``objection_classes`` map in the
# principles frontmatter and emits them as one JSON array in a marked block at the end of
# its reply. This module parses that block and DERIVES a verdict from it —
# ``derived_verdict`` — which is recorded and reported but never posted.
#
# **The Stage 1 safety property, stated once so it cannot be lost:** ``derived_verdict``
# does not appear in ``decide_verdict``'s ``gate_verdict`` computation. Whatever this
# parser does — mis-parses, is fed an injected block, crashes on nothing at all — the
# verdict submitted to GitHub is bit-identical to the pre-Stage-1 behaviour. The robustness
# of this parser is the thing being MEASURED, not something the gate depends on yet
# (msg-2033 §J-3). Reversing that is Stage 2, a separate Tier-C decision whose stated
# pre-conditions include an independent review of this parser's injection surface.
_OBJECTIONS_SENTINEL = "<!-- mindwire:objections v1 -->"

# Anchored at COLUMN ZERO. A unified-diff line carries a ``+``/``-``/space prefix, so a VERBATIM
# quote of this file's own diff (which necessarily contains the sentinel literal) cannot satisfy
# the anchor. The residual is real: a model that RE-TYPES the marker without the prefix produces
# a genuine match. This is the exploit surface :data:`_VERDICT_RE`'s own last-wins discipline is
# still exposed to (pending on ``T-verdict-echo-after-real-verdict``); this parser diverges by
# design (see :func:`parse_objections` D-1 note). In Stage 1 the residual costs nothing (nothing
# is posted from it); the rider-3 review (T-rider3-objection-parser-injection-surface) closes
# the parser side of the reversal's pre-conditions.
_OBJECTIONS_SENTINEL_RE = re.compile(rf"^{re.escape(_OBJECTIONS_SENTINEL)}\s*$", re.MULTILINE)


class ObjectionParse(Enum):
    """How the objection block of one critique read. Every value is recorded, none is fatal.

    ``UNKNOWN`` and ``NO_EVIDENCE`` are element-level facts promoted to the report so the
    gate notice can name what happened; ``MISSING`` covers both "no marker" and "the array
    would not parse", because the derivation treats them identically (fail-closed) and a
    finer split would suggest the two are handled differently when they are not.

    A **sub-reason** for ``MISSING`` is carried separately on :class:`ObjectionReport`
    (``missing_reason``), never on this enum: the derivation MUST be blind to it (all
    ``MISSING`` variants derive REQUEST_CHANGES) but the shadow measurement MUST see it
    (rider 2 needs to tell "D-1 over-fired" from "D-3 over-fired" from "the model wrote
    no block at all"). Splitting the enum would leak the distinction into the derivation
    path; hanging it off the report keeps derivation single-valued and measurement rich.
    """

    OK = "ok"
    EMPTY = "empty"
    MISSING = "missing"
    UNKNOWN = "unknown-class"
    NO_EVIDENCE = "no-evidence"


class ObjectionMissingReason(Enum):
    """Sub-reason attached to a :class:`ObjectionParse.MISSING` report (rider 3, msg-2130 §3).

    Every value derives REQUEST_CHANGES identically — the derivation reads
    :class:`ObjectionParse.MISSING` and stops. This enum exists so the shadow log line
    can name *which* MISSING cause fired, without which ``parse=missing`` is a black
    box that folds the enum's sub-causes (currently six: ``no-marker`` / ``multi-marker``
    / ``prose-between`` / ``bad-json`` / ``not-a-list`` / ``principles-error``) into one.
    **It is NOT surfaced on the posted D-divergence notice** (msg-2212 §1 / msg-2213):
    the notice is readable to the model whose diff is under review, and naming which
    parser wall it tripped gives that model (or an attacker whose exploit is in the
    diff) a per-attempt oracle to hill-climb. Rider 2 reads the sub-cause off the log
    line, which lives in the operator-side channel and is not returned to the reviewed
    revision.

    The two counters rider 2 was asked to instrument (msg-2130 §3) map to
    :attr:`MULTI_MARKER` (D-1 fired) and :attr:`PROSE_BETWEEN` (D-3 fired). The other
    values distinguish the pre-existing MISSING causes so a rise in either counter can
    be told apart from a rise in the baseline ``no_marker`` / ``bad_json`` cases.
    """

    NO_MARKER = "no-marker"  # zero column-zero markers (baseline: no block written)
    MULTI_MARKER = "multi-marker"  # D-1 fired: two or more column-zero markers
    PROSE_BETWEEN = "prose-between"  # D-3 fired: payload does not start with ``[``
    BAD_JSON = "bad-json"  # ``raw_decode`` raised on the payload
    NOT_A_LIST = "not-a-list"  # top-level JSON value was not a list
    PRINCIPLES_ERROR = "principles-error"  # vocabulary load failed (unreachable in practice)


@dataclass(frozen=True)
class Objection:
    """One element of the model's objection block, resolved against the class vocabulary."""

    objection_class: str
    where: str
    evidence: str
    known: bool  # the class name is in ``objection_classes()``
    blocks: bool  # counts toward the derived REQUEST_CHANGES


@dataclass(frozen=True)
class ObjectionReport:
    """The parse of one critique's objection block.

    ``missing_reason`` is set iff ``status is ObjectionParse.MISSING``. See
    :class:`ObjectionMissingReason` for why the sub-reason lives here rather than being
    baked into the ``status`` enum.
    """

    status: ObjectionParse
    objections: tuple[Objection, ...] = ()
    unknown_classes: tuple[str, ...] = ()
    missing_reason: ObjectionMissingReason | None = None

    @property
    def blocking(self) -> tuple[Objection, ...]:
        return tuple(o for o in self.objections if o.blocks)

    @property
    def advisory(self) -> tuple[Objection, ...]:
        return tuple(o for o in self.objections if not o.blocks)

    def counts_by_class(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for o in self.objections:
            counts[o.objection_class] = counts.get(o.objection_class, 0) + 1
        return counts

    def counts_label(self) -> str:
        counts = self.counts_by_class()
        return ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "none"


def _missing_report(reason: ObjectionMissingReason) -> ObjectionReport:
    """Construct a MISSING report tagged with its sub-reason (rider-3 msg-2130 §3).

    The ``reason`` argument is required — a call without one would produce a MISSING report
    whose cause is unknowable, which is precisely the black-box the instrumentation exists
    to eliminate. All MISSING cases still derive REQUEST_CHANGES (:func:`derive_verdict`
    only looks at ``status``); the reason surfaces on the shadow log line ONLY, never on
    the posted D-divergence notice (msg-2212 §1 / msg-2213: naming the sub-cause in the
    review body hands the reviewed revision an oracle to hill-climb against).
    """
    return ObjectionReport(status=ObjectionParse.MISSING, missing_reason=reason)


# Deepest bracket nesting :func:`parse_objections` will hand to ``raw_decode``. Well under
# CPython's recursion limit (empirically ``raw_decode("[" * 200)`` still returns ValueError,
# ``"[" * 1000`` raises RecursionError), and orders of magnitude above any real objection
# block: the schema is a flat list of flat objects, so honest payloads nest 2 deep.
_MAX_PAYLOAD_NESTING = 100


def _nesting_exceeds(text: str, limit: int = _MAX_PAYLOAD_NESTING) -> bool:
    """Cheap pre-scan: could any suffix of ``text`` nest brackets deeper than ``limit``?

    **Bound, not catch (msg-2385 §5).** ``json``'s decoder recurses per open bracket, so a
    payload of a thousand ``[`` raises ``RecursionError`` — which is a ``BaseException``, not
    a ``ValueError``, so it escapes :func:`parse_objections`'s ``except ValueError`` and
    breaks the "Never raises" contract in that function's docstring. Catching it is not a
    fix: at the moment ``RecursionError`` fires the stack is already exhausted, so the
    handler itself can re-raise. Rejecting over-deep input *before* the decoder sees it is
    deterministic and pinnable.

    A second, sharper reason, measured rather than argued: widening the two
    ``except ValueError`` clauses to ``except (ValueError, RecursionError)`` makes the D-7
    loop treat an over-deep chained payload as "this bracket did not open a list" and step
    past it, so the critique parses ``EMPTY`` and derives **APPROVE**. The catch does not
    merely fail to restore "Never raises"; at the loop's call site it converts a crash into
    a false APPROVE — the one outcome this parser exists to make impossible.

    **The metric: count openers, never decrement (msg-2433 §4).** Soundness, in one
    sentence: the decoder deepens its recursion only when it consumes a structural ``[`` or
    ``{``, and it consumes each character at most once, so the depth reached from ANY start
    offset is at most the number of ``[``/``{`` characters in ``text``. That is why one
    scan of the whole payload covers the primary decode AND every re-entry the D-7 loop
    makes at ``rest[bracket:]``: ``rest`` is a slice of this same string, so its opener
    count cannot exceed the whole string's. Bounding each slice separately would re-read the
    tail once per ``[`` and reintroduce the O(N^2) shape (measured at 1.2 s worst case,
    msg-2388 E-5) for no extra safety. Note what the proof does NOT need: any knowledge of
    which brackets sit inside string literals.

    **Why the two obvious refinements are worse — measured, not argued (msg-2433 §3).** The
    first form of this scan tracked ``depth - floor`` (the rise above the lowest point seen)
    and claimed that counting brackets inside JSON strings "only ever over-rejects". That
    claim was exactly half true, and the false half was the fail-open one: counting an
    in-string OPENER over-estimates, but counting an in-string CLOSER *under*-estimates. So
    ``["]", ["]", … ]`` walked ``depth`` between 0 and 1 forever, ``depth - floor`` never
    passed 1, and a 996-opener payload reached the decoder and raised ``RecursionError``
    straight out of :func:`parse_objections` (gate msg-2432; reproduced here, cliff bisected
    at exactly 996 openers). Nor is the repair the gate then asked for — "track string state
    and ignore brackets inside strings": :func:`_chains_another_block` probes
    ``rest.find("[", probe)`` and therefore hands ``raw_decode`` start offsets that are
    INSIDE string literals, which a string-aware metric is blind to by construction.
    Witness: ``'"' + "[" * 30 + '"'`` reaches depth 30 from offset 1 and that metric scores
    it 0. On a 200,000-case differential fuzz against a per-start-offset oracle the
    string-aware metric under-estimated 5,609 times, the ``depth - floor`` form it would
    have replaced 910 times, and the opener count below 0 times.

    **The residue, at its measured width.** Counting in-string openers and never
    decrementing over-rejects: a payload whose text after the marker holds more than
    ``limit`` ``[``/``{`` characters IN TOTAL is refused even though it nests two deep.
    That is fail-closed (MISSING → REQUEST_CHANGES, never APPROVE), the direction
    msg-2385 §2's F invariant permits. Width: over all 54 real gate critiques carrying a
    column-zero marker (PRs #140-#217, GitHub review bodies), the opener count is at most
    **5** (mean 2.4, p95 4) against a limit of 100 and a measured crash cliff of 996. So
    :data:`_MAX_PAYLOAD_NESTING` does not move: it sits ~20x above the largest honest
    payload observed and ~10x below the cliff.
    """
    opened = 0
    for ch in text:
        if ch in "[{":
            opened += 1
            if opened > limit:
                return True
    return False


def _chains_another_block(rest: str) -> bool:
    """D-7. True iff ``rest`` carries a second array that could have been the objection block.

    ``rest`` is the text :func:`parse_objections` did not consume. V-1 — ``raw_decode`` stops
    at the end of the first JSON value, so trailing text is structurally invisible — is also
    an injection surface: a critique that writes a decoy ``[]`` right after the marker and its
    REAL blocking array underneath parses EMPTY and derives APPROVE. Both the chained and the
    prose-separated variant were measured open on ``main`` (msg-2361 / msg-2388 E-2b). So the
    remainder is scanned and the whole parse REFUSED — fail-closed (MISSING → RC), so it can
    never soften a verdict, which is why "reject" beats "pick the later array" (selecting the
    later array is the fail-open ``find("[")`` shape D-3 already rejects). This does not
    contradict V-1: trailing text is still not *consumed*, it is *inspected*, and its presence
    disqualifies the block. D-4 stays dropped — arbitrary prose after the block is still legal,
    only a chained array that CLAIMS AN OBJECTION CLASS is refused.

    **CALLER CONTRACT (msg-2425 §6): ask this only when the primary read derives APPROVE.**
    :func:`parse_objections` calls it at exactly two places, both of them on that branch — the
    empty-array return and the assembled report when ``report.blocking`` is empty. Skipping the
    scan when the primary already carries a blocking objection is safe *because* the attack it
    defends against needs the parser to reach APPROVE: a primary with a blocking objection
    derives REQUEST_CHANGES no matter what follows it, so a refusal there buys zero bits, and an
    attacker who plants a blocking objection in order to stop the scan has thereby produced the
    REQUEST_CHANGES he was trying to avoid.

    ``report.blocking`` is WIDER than "the model named a blocking class", and the skip covers
    all of it: an unknown class (including a misspelling), a non-dict or class-less element,
    and a blocking objection carrying no evidence each set ``blocks=True``, so those primaries
    skip the scan too. That is the definition working rather than an exception — the only
    property the skip needs is "this primary already derives REQUEST_CHANGES", which holds for
    every one of them (measured across this change: derived stays REQUEST_CHANGES in all three
    shapes, while the reported status sharpens from ``bad-json`` to the specific cause).

    DEPENDENCY, stated so the next person to move it sees it: this reasoning rests entirely on
    :func:`derive_verdict` mapping "any blocking objection" to REQUEST_CHANGES. That dependency
    now carries a SECOND load — the predicate's lower bound below rests on the same mapping,
    because the two arrays that bound it from below (a misspelled class; a blocking class with
    no evidence) are worth refusing only while the parser still reads them as blocking. If
    ``derive_verdict`` ever derives APPROVE with a blocking objection present, BOTH the skip and
    the lower bound become unsound, and both call sites must be revisited.

    **Where the predicate's line sits, and why it is not arbitrary** (msg-2429 §1-§3, correcting
    the "any object" shape this file shipped at 82e50fb, the "non-empty" narrowing of 6b009d9
    and the "any list" shape of #206 R5/R6). The attack is "show the parser a benign array, show
    the human the real one", so it only works if the parser COULD have read the later array as
    the model's block. Exactly one key decides that, and it is ``class``: the element loop in
    :func:`parse_objections` reads ``element.get("class")`` and nothing else to tell an objection
    from noise, so an element that carries no ``class`` key is counted unknown-class exactly like
    an element that is not a dict at all. A chained list holding nothing that CLAIMS A CLASS
    therefore hides nothing: ``[]`` adds no objection to what precedes it, and ``[1]`` /
    ``["key"]`` / ``[1, 2]`` / ``[{"port": 80}]`` / ``[{"id": 1}, {"id": 2}]``, HAD the parser
    read them as the block, all derive UNKNOWN → REQUEST_CHANGES (measured, one parse each). No
    attacker reaches APPROVE through them, so refusing them buys no coverage and costs a false RC
    on the ordinary citation, subscript, list literal AND list-of-dicts of everyday prose.

    Measured on a 76-case corpus (7 attacks; 18 ordinary trailing-prose shapes and 5 dict-literal
    shapes, each against an empty, an advisory-only and a blocking primary), scored end to end
    through :func:`parse_objections`:

    ======================================  =========  ============  ============
    predicate                                  correct   false fires   attacks let
                                                                              past
    ======================================  =========  ============  ============
    ``isinstance(chained, list)``               50/76            26             0
    ``... and chained``                         52/76            24             0
    ``... any dict`` (82e50fb)                  64/76            12             0
    ``... any dict with "class"`` (here)        72/76             4             0
    ``... any dict whose class is known``       73/76             2             1
    ======================================  =========  ============  ============

    Two of the false fires in EVERY row are the same input — an honest critique that quotes a
    REAL objection array, residue 3 below — so the row-to-row deltas are unaffected by whether
    one counts that inseparable case as a miss. The EIGHT this round removes are the four
    dict-literal shapes whose objects claim no class, each against the two APPROVE-deriving
    primaries: ``[{"port": 80}]``, ``[{"name": "a", "value": 1}]``, ``[{"id": 1}, {"id": 2}]``
    and the same list nested inside an object, ``{"items": [{"port": 80}]}`` — the loop's
    ``probe = bracket + 1`` re-entry reaches an inner list, so nesting does not hide it. The four
    that REMAIN are both halves of residue 3 below: ``[{"class": "btn-primary"}]`` (2, an
    ordinary ``class`` key) and a critique quoting a real objection array (2, inseparable).
    Under a BLOCKING primary no row false-fires at all — that is the caller contract, not this
    predicate. False RC is permitted (msg-2385 §2, F) but it is not free: this parser's output IS
    the shadow measurement rider 2 is calibrating, and a defense that fires on ordinary critiques
    saturates the signal it instruments.

    **The line is bracketed from BELOW as well, which is why it stops here.** One notch narrower
    opens a measured fail-open in each of two directions, and both are pinned:

    * **Requiring the class to be IN the vocabulary** lets ``[{"class": "corectness", "where":
      …, "evidence": …}]`` — a misspelling — chain behind a decoy ``[]`` and derive APPROVE.
      The parser itself reads that array as unknown-class → blocking, so it is precisely an
      array that "could have been the block".
    * **Additionally requiring ``where`` and ``evidence``** lets ``[{"class": <blocking>,
      "where": "a.py:1"}]`` chain and derive APPROVE. That is not merely an evasion: it
      reinstates, on the chained side, the inversion ``parse_objections`` refuses on the primary
      side, where ``NO_EVIDENCE`` deliberately does not demote a blocking objection precisely so
      that "write no evidence" cannot become the cheapest way to soften a verdict. (Requiring
      ``where`` OR ``evidence`` rather than both opens neither of these two — measured — so it
      is the conjunctive form that is unsafe, not the mention of those keys.)

    Both are pinned by :func:`test_d7_refuses_a_chained_array_that_claims_a_class_but_little_else`,
    which exists so that a later reading of "match the schema more strictly" cannot quietly move
    the line down. **Measured and NOT adopted:** requiring ``class`` plus (``where`` and
    ``evidence``, or a known class) scores 74/76 here — better than the predicate above on this
    corpus, and rejected anyway. It is a condition fitted to the corpus rather than derived from
    the criterion, and it still lets the one-key misspelling ``[{"class": "corectness"}]`` through
    (measured: derives APPROVE). ``class`` alone follows from the criterion, because ``class``
    alone is what the element loop reads.

    **WHAT IS LEFT OVER.** Three residues, deliberately kept, in three different senses —
    listed apart because merging them has twice produced a wrong remedy:

    1. **DISCARDED (surplus fail-closure).** A chained list of SCALARS a human would read as
       objections, e.g. ``["correctness: foo is broken"]``, is not refused. It is structurally
       indistinguishable from ``["key"]``, so refusing it returns every false fire above. It is
       not fail-open in the machine's direction — as shown above that array cannot make the
       parser say APPROVE — so what is given up is surplus fail-closure, not attack coverage.

    2. **NOT CLOSABLE (the region gate msg-2420 found).** A trailing block that the machine
       cannot parse but a human reads as objections: Python-style single quotes, a trailing
       comma, plain prose, a markdown bullet list, YAML, or nothing at all. The scan cannot see
       any of these, and closing the JSON-syntax corner of it is measurably worse than leaving
       it open. Measured on a 35-case corpus (the 11 above plus 24 ordinary review-prose
       shapes): this predicate scores 35/35 with zero false fires — unchanged by this round's
       narrowing, since none of those shapes carries a ``class`` key and adding a conjunct can
       only make the predicate fire on fewer inputs — while fail-closing on a
       remainder ``[`` that does not decode scores 18/35 — 17 false fires, namely markdown
       links ``[text](url)``, type annotations ``list[str]`` / ``Optional[int]``, subscripts
       ``arr[i]``, ``[TODO]``, section references, log timestamps ``[2026-09-03 00:43]`` and
       character classes ``[a-z]+`` — and reds
       :func:`test_d7_does_not_fire_on_honest_blocks_or_on_trailing_prose`. It also buys
       nothing, because the same evasion is reachable without any bracket at all: writing the
       objections as prose, as a bullet list, as YAML, or omitting them entirely all derive
       APPROVE under BOTH shapes (measured, four ways). An attacker who can be told "use single
       quotes" can be told "use prose", which is strictly less control for the same result.

    3. **NARROWED TWICE, NOT CLOSED (msg-2425 §6, then msg-2429 §4).** State this one at the
       predicate's WIDTH, not at its intent — describing it by what it was meant to catch is
       what produced the two rounds of correction it has already had. The exact remaining
       surface: an APPROVING primary (an empty block, or advisory objections only) whose
       remainder contains a JSON array holding at least one object with a ``class`` key. That
       covers two different things, and only the first is inseparable.

       (i) A critique that quotes a REAL objection array — a critique ABOUT this parser is the
       realistic case, and msg-2413 §1 measured the gate's own round-1 review self-jamming on
       it. Scope the impossibility claim exactly (msg-2425 §6, correcting msg-2419 §3): no
       predicate **over the remainder alone** can separate this from the attack, because the
       two remainders are byte-identical. The separator that does exist is not in the remainder
       — it is the primary read, which is what the caller contract above uses — and an approving
       primary is precisely the case where the primary carries no signal, so this remnant
       survives by construction.

       (ii) A literal that uses ``class`` as an ordinary key with no relation to objections:
       ``[{"class": "btn-primary"}]`` in a review of HTML/CSS/Java/ML code. This one IS
       separable in principle and is not separated: 2/76 on the corpus above, a cost taken
       knowingly rather than an oversight, because every narrower predicate measured opens a
       fail-open (see the lower bound above). If shadow measurement ever shows (ii) firing in
       practice, the fix is NOT a narrower predicate — it is to stop reading the remainder's
       shape at all (require the block last, the family of D-4, dropped once already), and that
       is a redesign, not an edit here.

       Both are warts of D-1's re-typed-marker family, with the same loud, human-overridable
       exit; an approving critique avoids them by placing the example ABOVE the marker.

    ``bad_json`` rather than a new :class:`ObjectionMissingReason` member: this is the shape
    #206 lands as ``payload_unparseable`` (which merges ``bad-json`` and ``not-a-list``), so
    reusing the existing member keeps this backport free of an enum change #206 would
    immediately rename, and off the enum-derived exhaustive pin in
    ``test_missing_reason_never_leaks_into_the_posted_notice``.
    """
    probe = 0
    while (bracket := rest.find("[", probe)) >= 0:
        try:
            # D-6 has already bounded the nesting of the whole payload, and ``rest`` is a
            # slice of it, so this re-entry cannot raise RecursionError either.
            chained, _ = json.JSONDecoder().raw_decode(rest[bracket:])
        except ValueError:
            probe = bracket + 1
            continue
        if isinstance(chained, list) and any(isinstance(e, dict) and "class" in e for e in chained):
            return True
        probe = bracket + 1
    return False


def parse_objections(critique: str) -> ObjectionReport:
    """Parse the objection block out of ``critique``. Never raises — D-6 below carries the
    single bound that makes that true, and the two occasions on which it was false.

    Five outcomes, all recorded, none of which changes what the gate posts:

    ``OK``           marker present, array parsed, every class known and evidenced.
    ``EMPTY``        the array is ``[]`` — a legal statement that nothing was objected to.
    ``MISSING``      no column-zero marker, more than one column-zero marker, the payload
                     after the marker does not begin with a JSON array, a SECOND array is
                     chained behind the first (D-7), or the payload nests deeper than
                     :data:`_MAX_PAYLOAD_NESTING` (D-6).
    ``UNKNOWN``      some element names a class outside the vocabulary.
    ``NO_EVIDENCE``  some blocking element carries no evidence.

    ``NO_EVIDENCE`` deliberately does NOT demote the objection to advisory. Demotion would be
    fail-open, and worse, it would make "write no evidence" the cheapest way to soften a
    verdict — inverting the evidence obligation the class system is built on (msg-2033 §J-3).

    Malformed ELEMENTS inside a well-formed array (not an object, missing ``class``) are
    counted as unknown-class rather than dropped: silently discarding an element the model
    wrote is the one behaviour that could make the derived verdict quieter than the prose.

    **D-1 (strict-single sentinel, rider-3 msg-2072 / msg-2073).** Two or more column-zero
    markers in one critique derive MISSING — not "last-wins", not "concatenate all". Last-wins
    is bypassable by a re-typed copy placed AFTER the model's own block (the same residual
    :data:`_VERDICT_RE` still carries), and concatenation lets an attacker (or the model's own
    verbosity) *add* blocking objections at will, which is a new denial-of-service surface
    aimed straight at the reversed gate. MISSING derives REQUEST_CHANGES, which is fail-closed
    and *loud*: the reason is named in the report, so a human can see what happened. The
    accepted side-effect: a PR touching this file whose reviewer re-types the marker at
    column zero self-jams into RC. The override path is human, which is the right shape for
    a rare deadlock caused by discussing the parser inside the parser's own gate.

    **D-3 (strict bracket placement, rider-3 msg-2072).** After fence-stripping, the payload's
    first non-whitespace character must be ``[`` — no prose in between. This closes an
    F-a-direction window (msg-2074 §1): a scan-forward ``find("[")`` would silently anchor to
    a benign or malicious empty array further down (``Here are my objections: []`` etc.),
    discarding the model's real block. Failing this trips MISSING → RC, which is loud and
    fail-closed. The reliability cost (models that lead with a short sentence get their
    objection block dropped) is Stage-1 shadow-measurable before the reversal.

    **D-5 (explicit whitespace handling, rider-3 msg-2130 §2 / gate advisory on #198).** The
    D-3 check runs ``.lstrip()`` on the payload IN THIS FUNCTION rather than trusting
    :func:`strip_wrapping_fences` to have trimmed it. Two reasons: (a) the pre-D-5 code was
    correct only by accident — ``payload.startswith("[")`` worked because the helper happens
    to call ``.strip()`` even when no fence is present, so a future optimisation limiting the
    helper to "mutate only when a fence exists" would silently break D-3 by turning every
    ``\\n[...]`` payload into MISSING; (b) D-5 is a REINFORCEMENT of D-3, not a relaxation
    (whitespace cannot carry an injected payload nor construct a false array, so the receiving
    language is the same). The load-bearing wall is now self-sufficient. Pinned by
    :func:`test_v2_fence_less_payload_with_leading_newline_is_accepted` and
    :func:`test_d5_payload_starts_after_leading_whitespace_independent_of_helper`.

    **D-6 (depth bound before the decoder, msg-2380 / msg-2385 §5 / msg-2388 E-5; metric
    corrected by gate msg-2432 / msg-2433).** "Never raises" above was false on ``main``:
    ``json``'s decoder recurses per open bracket, so a payload of a thousand ``[`` leaked
    ``RecursionError`` — a ``BaseException``, so the ``except ValueError`` below never saw
    it — and took the whole review call down with it, since :func:`decide_verdict` calls
    this function unconditionally. Unlike the D-7 window below, this one is NOT shadow-only:
    the crash is real on ``main`` today.

    It was ALSO false on this branch, for four review rounds. The first bound counted
    brackets inside string literals in BOTH directions, so a payload that hides a ``]`` in a
    string at every level (``["]", ["]", … ]``) held the measured rise at 1 and let the same
    ``RecursionError`` escape from here. ``main`` crashes on a strict superset of the inputs
    this file ever crashed on — the branch narrowed the surface at every step, it never
    widened it — but a docstring that says "Never raises" while a witness makes it raise is
    the defect either way. Both are closed by bounding the input rather than catching the
    error. :func:`_nesting_exceeds` carries the metric, its one-sentence soundness proof,
    the two refinements that measured worse (including the one this gate round asked for),
    and the residue at its measured width.

    **D-7 (trailing-list defense, msg-2361 / msg-2363, ported here by msg-2397 §9).** V-1's
    property — ``raw_decode`` stops at the first ``]``, so trailing text is structurally
    invisible — is also an injection surface. A critique that writes a decoy ``[]`` right
    after the marker and its REAL blocking array underneath parses ``EMPTY`` and derives
    APPROVE. Both the chained variant and the prose-separated variant were measured open on
    ``main``. So the remainder is scanned by :func:`_chains_another_block`: any ``[`` in it that
    begins a JSON list HOLDING AT LEAST ONE OBJECT WITH A ``class`` KEY means a chained block,
    and the parse is refused. A list holding nothing that claims a class cannot be the model's
    block — ``class`` is the only key the element loop below reads to recognise an objection, so
    such a list would have been counted unknown-class and derived REQUEST_CHANGES even if the
    parser HAD read it — and refusing it would cost the ordinary ``[1]`` / ``my_dict["key"]`` /
    ``[]`` / ``[{"port": 80}]`` of everyday prose while buying nothing (msg-2429 §1). The scan
    runs on the APPROVE-deriving branches only (msg-2425 §6): an empty primary array, and an
    assembled report with no blocking objection. That helper's docstring carries the predicate's
    placement, the corpus that bounds it from ABOVE and the two fail-opens that bound it from
    BELOW, the caller contract, the :func:`derive_verdict` property the skip depends on, and the
    three residues.
    This does NOT contradict V-1 — trailing text is still not *consumed*; it is now
    *inspected*, and its presence disqualifies the block rather
    than being selected as the payload (selecting the later array is the fail-open ``find("[")``
    shape D-3 already rejected). D-4 stays dropped: arbitrary prose after the block is still
    fine, only a chained array that CLAIMS AN OBJECTION CLASS is refused.

    **What is NOT done here.** D-2-prime (a non-regression floor on ``gate_verdict``: the derived
    read may never lower the gate below the pre-Stage-1 baseline) is a Stage-2 constraint on
    :func:`decide_verdict`, not a parser change; ``derived_verdict`` does not participate in
    ``gate_verdict`` yet (see the SHADOW note above :data:`_OBJECTIONS_SENTINEL`), so wiring
    D-2-prime is premature until the reversal is on the table. R-4 (unify the two parsers' anchor
    strategy, or justify the divergence, before the reversal) is discharged by the D-1 note
    above and the divergence back-reference at :data:`_VERDICT_RE` (msg-2130 §1): the objection
    parser is deliberately strict-single, ``_VERDICT_RE`` remains last-wins for the reasons on
    ``T-verdict-echo-after-real-verdict``, and the divergence is named on BOTH sides rather
    than left implicit. V-1 (verify the array-terminator method): confirmed by inspection —
    ``raw_decode`` stops at the end of the JSON value, so anything after ``]`` is structurally
    invisible to this parser (matching the note next to the ``raw_decode`` call below); no
    code change is needed.

    **MISSING sub-reasons (rider-3 msg-2130 §3).** Every MISSING return path carries an
    :class:`ObjectionMissingReason` so the shadow log line and the D-divergence notice can
    distinguish which cause fired (``multi_marker`` = D-1, ``prose_between`` = D-3,
    ``no_marker`` / ``bad_json`` / ``not_a_list`` = pre-existing causes). This is the
    instrumentation rider 2 needs to tell "D-1/D-3 over-fired" from "the model wrote nothing".
    """
    matches = list(_OBJECTIONS_SENTINEL_RE.finditer(critique))
    if not matches:
        # Baseline: the model wrote no column-zero marker at all. Same fail-closed outcome
        # as MULTI_MARKER, but a different signal for shadow measurement.
        return _missing_report(ObjectionMissingReason.NO_MARKER)
    if len(matches) > 1:
        # D-1: two-plus markers derive MISSING → REQUEST_CHANGES. The question "which match
        # wins?" is deleted rather than answered — see the docstring above for why last-wins
        # and concatenate-all were both rejected.
        return _missing_report(ObjectionMissingReason.MULTI_MARKER)
    payload = strip_wrapping_fences(critique[matches[0].end() :])
    # D-5: ``.lstrip()`` here in the parser, so the D-3 check does not depend on
    # ``strip_wrapping_fences`` incidentally trimming leading whitespace. See the docstring
    # for the coupling this breaks. Whitespace-only leaders are fine — they carry no payload.
    if not payload.lstrip().startswith("["):
        # D-3: no scan-forward. If the payload's first non-whitespace char is not ``[``,
        # fall through to MISSING rather than anchor to some later ``[`` in the prose.
        return _missing_report(ObjectionMissingReason.PROSE_BETWEEN)
    # D-3/D-5: raw_decode wants to start at the ``[``, so consume the leading whitespace
    # we just verified. The strip is safe because D-3 has already established that the
    # first non-whitespace char is ``[``.
    payload = payload.lstrip()
    if _nesting_exceeds(payload):
        # D-6. ONE scan, both ``raw_decode`` call sites (msg-2397 M7). It runs on the whole
        # payload and counts every ``[``/``{`` in it, which upper-bounds the depth reachable
        # from ANY start offset inside it, so it covers the primary decode below AND every
        # re-entry the D-7 loop makes at ``rest[bracket:]`` — ``rest`` is a slice of this
        # same string, so it cannot hold more openers. A second scan before
        # the loop would be provably unreachable, i.e. dead code no negative control can turn
        # red; the two-site coverage is pinned by behaviour instead, one test per call site.
        return _missing_report(ObjectionMissingReason.BAD_JSON)
    try:
        # ``raw_decode`` stops at the end of the array, so the verdict line (and any prose)
        # that follows the block is simply not consumed — no need to guess where it ends.
        # V-1 (rider-3 msg-2074): this is what makes trailing text after ``]`` structurally
        # invisible to the parser, which is why D-4 ("no trailing text allowed") was dropped
        # rather than turned into a code change (msg-2130 §3).
        parsed, consumed = json.JSONDecoder().raw_decode(payload)
    except ValueError:
        return _missing_report(ObjectionMissingReason.BAD_JSON)
    if not isinstance(parsed, list):
        return _missing_report(ObjectionMissingReason.NOT_A_LIST)
    # D-7, call site (a): an empty primary array derives APPROVE, so the remainder is where a
    # chained real block would hide. :func:`_chains_another_block` carries the whole rationale
    # — the predicate's line, the caller contract this branch satisfies, and the three
    # residues. Read it before moving either call site.
    rest = payload[consumed:]
    if not parsed:
        if _chains_another_block(rest):
            return _missing_report(ObjectionMissingReason.BAD_JSON)
        return ObjectionReport(status=ObjectionParse.EMPTY)

    try:
        vocabulary = objection_classes()
    except PrinciplesError:  # pragma: no cover - unreachable in practice, see below
        # A malformed SOT has already taken this call down: ``build_preamble()`` reads the
        # same file to assemble the system prompt, long before any critique exists to parse.
        # Narrow on purpose — swallowing every exception here would hide a real bug in this
        # parser behind a fail-closed status that looks like a badly-behaved model.
        return _missing_report(ObjectionMissingReason.PRINCIPLES_ERROR)

    objections: list[Objection] = []
    unknown: list[str] = []
    no_evidence = False
    for element in parsed:
        raw_class = element.get("class") if isinstance(element, dict) else None
        name = raw_class.strip() if isinstance(raw_class, str) else ""
        where = str(element.get("where", "")) if isinstance(element, dict) else ""
        evidence = str(element.get("evidence", "")) if isinstance(element, dict) else ""
        entry = vocabulary.get(name)
        if entry is None:
            unknown.append(name or repr(element)[:80])
            objections.append(
                Objection(
                    objection_class=name or "<malformed>",
                    where=where,
                    evidence=evidence,
                    known=False,
                    blocks=True,
                )
            )
            continue
        if entry.blocks and not evidence.strip():
            no_evidence = True
        objections.append(
            Objection(
                objection_class=name,
                where=where,
                evidence=evidence,
                known=True,
                blocks=entry.blocks,
            )
        )

    if unknown:
        status = ObjectionParse.UNKNOWN
    elif no_evidence:
        status = ObjectionParse.NO_EVIDENCE
    else:
        status = ObjectionParse.OK
    report = ObjectionReport(
        status=status, objections=tuple(objections), unknown_classes=tuple(unknown)
    )
    # D-7, call site (b): the primary parsed into objections, but if NONE of them blocks the
    # derived verdict is still APPROVE — advisory-only and (vacuously) empty reports alike —
    # so the same window is open and the same scan applies. When something does block, the
    # scan is skipped on purpose; :func:`_chains_another_block`'s caller contract says why
    # that is safe and names the :func:`derive_verdict` property it depends on.
    if not report.blocking and _chains_another_block(rest):
        return _missing_report(ObjectionMissingReason.BAD_JSON)
    return report


def derive_verdict(report: ObjectionReport) -> ReviewEvent:
    """The verdict the class vocabulary implies: RC iff any objection blocks (fail-closed).

    ``MISSING`` derives REQUEST_CHANGES (D-7b): a review whose machine-readable half could
    not be read is not evidence of "no blocking objection". Unknown classes count as blocking
    for the same reason, and an unevidenced blocking objection stays blocking (see
    :func:`parse_objections`).

    SHADOW: no caller posts this. See the block above :data:`_OBJECTIONS_SENTINEL`.
    """
    if report.status is ObjectionParse.MISSING:
        return ReviewEvent.REQUEST_CHANGES
    return ReviewEvent.REQUEST_CHANGES if report.blocking else ReviewEvent.APPROVE


@dataclass(frozen=True)
class VerdictDecision:
    """The full verdict picture of a single review — model side, gate side, and the diff view.

    ``gate_verdict`` is what actually gets posted to GitHub. ``model_verdict`` is what the
    model wrote in the critique. :attr:`suppressed` is precisely one thing: the model said
    APPROVE and the gate posted REQUEST_CHANGES anyway (the case msg-1871 is about).
    Every other mismatch (model=UNPARSEABLE gate=RC, model=RC gate=RC, ...) is not
    "suppression" and does not fire the C-suppressed notice — see the naysayer's O-1 in
    msg-1873 for why conflating them makes the notice itself dishonest.
    """

    model_verdict: ModelVerdict
    gate_verdict: ReviewEvent
    view: DiffView
    finish_reason: str | None
    # Stage 1 SHADOW pair. ``objections`` is the parse of the model's machine-readable block;
    # ``derived_verdict`` is what the class vocabulary implies. NEITHER is read by
    # ``gate_verdict`` above — grep this file: the only consumers are ``render_gate_notice``
    # (the D-divergence note) and the structured log line. Defaults keep them optional for the
    # short-circuit paths that construct no block at all.
    objections: ObjectionReport = ObjectionReport(status=ObjectionParse.MISSING)
    derived_verdict: ReviewEvent = ReviewEvent.REQUEST_CHANGES

    @property
    def diverged(self) -> bool:
        """The derived verdict disagrees with what the gate posted, or the block was unusable.

        Both halves matter for the shadow measurement: a disagreement is the signal the
        derivation is being calibrated on, and an unusable block is the reason a
        disagreement might be spurious. Reporting only the first would let a systematically
        unreadable block look like a systematically wrong derivation.
        """
        return self.derived_verdict is not self.gate_verdict or self.objections.status in (
            ObjectionParse.MISSING,
            ObjectionParse.UNKNOWN,
            ObjectionParse.NO_EVIDENCE,
        )

    @property
    def suppressed(self) -> bool:
        return (
            self.model_verdict is ModelVerdict.APPROVE
            and self.gate_verdict is ReviewEvent.REQUEST_CHANGES
        )


def decide_verdict(critique: str, *, view: DiffView, finish_reason: str | None) -> VerdictDecision:
    """Compute the model verdict, gate verdict, and pack them with the DiffView.

    The gate-verdict rule here — "force-RC on any partial review; otherwise mirror the
    model verdict" — is the same rule the pre-change ``_resolve_verdict`` implemented,
    now the SINGLE authoritative statement of it (test 9's oracle equivalence pins the
    24 combinations of model / truncated / finish_reason). The goal of this change is
    to make the existing behaviour AUDIBLE, not to change it. See msg-1874 §9 "参照
    実装をテスト内に置く".
    """
    mv = _parse_model_verdict(critique)
    if view.truncated or finish_reason == "length":
        gv = ReviewEvent.REQUEST_CHANGES
    elif mv is ModelVerdict.APPROVE:
        gv = ReviewEvent.APPROVE
    else:
        # UNPARSEABLE or REQUEST_CHANGES → gate stays closed (fail-safe collapse of the
        # two non-APPROVE model verdicts to a single gate verdict). This is the ONE case
        # where UNPARSEABLE and RC still look the same to the gate; the distinction
        # survives on ``model_verdict`` for the notice, so the record does not lie about
        # which one was written.
        gv = ReviewEvent.REQUEST_CHANGES
    # SHADOW (Stage 1). Computed AFTER ``gv`` and never fed back into it — the two statements
    # above are the whole of the gate rule and this change adds nothing to them.
    report = parse_objections(critique)
    return VerdictDecision(
        model_verdict=mv,
        gate_verdict=gv,
        view=view,
        finish_reason=finish_reason,
        objections=report,
        derived_verdict=derive_verdict(report),
    )


# Machine-readable markers. The unique sentinel + one marker per note lets an auditor grep
# the corpus for exactly the rounds a given note fired, without depending on the prose (which
# will change) or on which words happen to be shared between notes. msg-1876 §"同じ規律
# を当てたら、もう 1 件出た" — the prior draft used the word "split" as the B-diff key and
# collided with A-headroom, which also urges splitting; markers close that class of bug.
_GATE_NOTICE_SENTINEL = "<!-- mindwire:gate-notice v1 -->"
_MARKER_A_HEADROOM = "<!-- mindwire:note A-headroom -->"
_MARKER_B_DIFF = "<!-- mindwire:note B-diff -->"
_MARKER_B_LEN = "<!-- mindwire:note B-len -->"
_MARKER_C_SUPPRESSED = "<!-- mindwire:note C-suppressed -->"
_MARKER_D_DIVERGENCE = "<!-- mindwire:note D-divergence -->"


def _model_verdict_label(mv: ModelVerdict) -> str:
    return mv.value


def _gate_verdict_label(gv: ReviewEvent) -> str:
    return gv.value.upper()


def render_gate_notice(decision: VerdictDecision) -> str:
    """Render the gate-notice markdown block for ``decision``, or "" when quiet.

    Returns ``""`` (no sentinel, no markers) when all three axes are quiet:
    ``original_chars < warn_threshold``, ``finish_reason != "length"``, not suppressed.
    Otherwise returns the sentinel + a block containing the model/gate verdict header and
    one blockquoted note per fired condition (A-headroom / B-diff / B-len / C-suppressed).

    The block is designed to be PREPENDED to the critique (msg-1872 D-4 rationale): a
    reader who saw ``CHANGES_REQUESTED`` in the review state needs the reason at the top,
    not buried after a long critique whose last line may itself be truncated by the same
    length cap the notice is reporting.
    """
    view = decision.view
    fire_a = view.in_headroom and not view.truncated
    fire_b_diff = view.truncated
    fire_b_len = decision.finish_reason == "length"
    fire_c = decision.suppressed
    fire_d = decision.diverged
    if not (fire_a or fire_b_diff or fire_b_len or fire_c or fire_d):
        return ""

    lines: list[str] = [_GATE_NOTICE_SENTINEL]
    lines.append("> **GATE NOTICE**")
    lines.append(
        f"> model verdict: {_model_verdict_label(decision.model_verdict)}"
        f"   gate verdict: {_gate_verdict_label(decision.gate_verdict)}"
    )
    if fire_a:
        remaining = view.limit - view.original_chars
        pct = round(100.0 * view.original_chars / view.limit) if view.limit else 0
        lines.append(">")
        lines.append(f"> {_MARKER_A_HEADROOM}")
        lines.append(
            f"> **Approaching the diff limit.** This diff is {view.original_chars:,} of "
            f"the {view.limit:,} chars the gate can read ({pct}%; {remaining:,} left). "
            f"Once it crosses, reviews go partial and the gate force-posts "
            f"REQUEST_CHANGES regardless of findings — APPROVE becomes unreachable until "
            f"the PR is split. **Split now, while it can still pass.**"
        )
    if fire_b_diff:
        unread = view.original_chars - view.limit
        pct = round(100.0 * unread / view.original_chars) if view.original_chars else 0
        lines.append(">")
        lines.append(f"> {_MARKER_B_DIFF}")
        lines.append(
            f"> **Partial review — diff exceeded the review cap.** This diff is "
            f"{view.original_chars:,} chars; the gate reads at most {view.limit:,}. "
            f"{unread:,} chars ({pct}%) were never sent to the model. Findings and "
            f"endorsements below cover only the first {view.limit:,} chars — silence "
            f"about the remainder is not approval. **No number of further rounds will "
            f"produce APPROVE while the diff exceeds {view.limit:,} chars. Split the PR.**"
        )
    if fire_b_len:
        # STRICTLY ON THE OUTPUT AXIS. This note may only describe what happened to the
        # model's response — never make claims about the DIFF-size axis (A-headroom / B-diff).
        # An earlier draft said "This is a REVIEW-length issue, not a DIFF-size issue —
        # splitting the PR would not help.", which was factually true when B-len fired
        # alone but a direct contradiction of the split directive when A-headroom or
        # B-diff fired alongside it (round-6 PR-gate finding on PR #186 msg-1893). Same
        # discipline as Bohr's msg-1876 §O-3 for invariants: a note may only assert
        # about its own axis. If the reader needs to know whether a diff-size problem
        # ALSO exists, the presence or absence of A-headroom / B-diff answers that —
        # this note does not.
        lines.append(">")
        lines.append(f"> {_MARKER_B_LEN}")
        lines.append(
            "> **Review truncated by the model's output-token cap.** The critique below "
            "was cut off before the model finished writing it (finish_reason=length). "
            "Findings below may be incomplete."
        )
    if fire_c:
        lines.append(">")
        lines.append(f"> {_MARKER_C_SUPPRESSED}")
        lines.append(
            "> **Verdict suppressed by the gate.** The model wrote `VERDICT: APPROVE` "
            "but the gate posted `REQUEST_CHANGES` because the review below is partial "
            "(see the note(s) above); a review of a partial diff / partial output "
            "cannot open the gate."
        )
    if fire_d:
        # Rider 1 (msg-2031) put in a channel that has readers. ``spec/process/README.md``
        # (fail-open 宣言先, 旧 §N.3) says a degradation announced only to a log is an
        # announcement to nobody — measured: a correctly-declared ADR-index fail-open sat
        # unread for five weeks in review-artifact prose. So the divergence rides in the body
        # that is posted to the chatroom AND submitted as the GitHub review, next to the
        # verdict it is about. The classed objections themselves need no new store: the model
        # wrote them into this same body, which both channels already carry verbatim.
        report = decision.objections
        lines.append(">")
        lines.append(f"> {_MARKER_D_DIVERGENCE}")
        lines.append(
            f"> **Objection-class shadow (measurement only — nothing here changed the "
            f"verdict).** authored: {_model_verdict_label(decision.model_verdict)}   "
            f"posted: {_gate_verdict_label(decision.gate_verdict)}   derived from classes: "
            f"{_gate_verdict_label(decision.derived_verdict)}. Block parse: "
            f"`{report.status.value}`; {len(report.blocking)} blocking / "
            f"{len(report.advisory)} advisory; by class: {report.counts_label()}."
        )
        if report.unknown_classes:
            lines.append(
                f"> Class names outside `objection_classes`: "
                f"{', '.join(sorted(set(report.unknown_classes)))} (counted as blocking)."
            )
        if report.status is ObjectionParse.MISSING:
            # msg-2212 §1 / msg-2213: the sub-cause (``missing_reason``) is deliberately NOT
            # named in the posted notice. Naming which of D-1 / D-3 / no-marker / bad-json
            # fired hands the model — or an attacker whose diff is under review — a per-attempt
            # oracle they can hill-climb against ("D-1 fired → collapse to one marker"; "D-3
            # fired → drop the leading prose") until the block parses clean. In Stage 1 this
            # is shadow so the F-a-direction risk is bounded, but the reversal PR is the
            # payoff for this same oracle. Rider 2 still gets the sub-cause: it rides on
            # :func:`_log_objections` below, which is not posted anywhere the reviewed
            # revision can read. Pinned by
            # :func:`test_d_divergence_notice_never_names_the_missing_sub_reason`.
            lines.append(
                "> No readable objection block was found, so the derived side defaults to "
                "REQUEST_CHANGES (fail-closed). This says nothing about the review above."
            )
    return "\n".join(lines)


def _log_objections(pr_slug: str, head_sha: str | None, decision: VerdictDecision) -> None:
    """One structured line per review — the AUXILIARY channel for the shadow (J-4 (iii)).

    Auxiliary, not the record: the gate notice above is where a reader finds this. A log line
    is what ``spec/process/README.md`` (旧 §N.3) measured as unread, so it is here for grepping
    a corpus of runs, never as the place a divergence is announced.

    The ``missing_reason=`` field (rider-3 msg-2130 §3) breaks ``parse=missing`` out into
    its five sub-causes (``no-marker`` / ``multi-marker`` / ``prose-between`` / ``bad-json`` /
    ``not-a-list``). Without it, a spike in ``parse=missing`` cannot be triaged: three
    independent signals — the model wrote nothing (baseline), D-1 fired (multi-marker), D-3
    fired (prose-between) — all collapse to the same string. The field is ``-`` when
    ``status != MISSING``, so the log line stays grep-friendly.
    """
    report = decision.objections
    missing_reason = report.missing_reason.value if report.missing_reason is not None else "-"
    logger.info(
        "naysayer objections %s (head %s): parse=%s missing_reason=%s blocking=%d advisory=%d "
        "by_class=%s authored=%s posted=%s derived=%s diverged=%s",
        pr_slug,
        head_sha or "?",
        report.status.value,
        missing_reason,
        len(report.blocking),
        len(report.advisory),
        report.counts_label(),
        _model_verdict_label(decision.model_verdict),
        _gate_verdict_label(decision.gate_verdict),
        _gate_verdict_label(decision.derived_verdict),
        decision.diverged,
    )


def prepend_gate_notice(body: str, decision: VerdictDecision) -> str:
    """Return ``body`` with the gate notice prepended (or unchanged when the notice is empty).

    Idempotent-shaped: called exactly once per review body, before both ``post_critique``
    (chatroom relay) and ``_submit_review`` (GitHub) — same body, same rendered notice,
    so the two channels cannot show different pictures. The blank line between notice and
    body prevents the critique's first line from being absorbed into the block quote.
    """
    notice = render_gate_notice(decision)
    if not notice:
        return body
    return f"{notice}\n\n{body}"


def _ci_gate_response(ci: CiStatus, pr_slug: str) -> tuple[ReviewEvent, str]:
    """Map a non-green CI state to a (verdict, body) — the L1 gate (ADR-16 §D-2).

    FAILURE → ``REQUEST_CHANGES`` (cite the failing runs). PENDING / UNKNOWN → ``COMMENT`` hold —
    never APPROVE while CI is not confirmed green (fail-closed). Caller invokes this **instead of**
    the Lexora content review (short-circuit).
    """
    head = ci.head_sha or "?"
    if ci.state is CiState.FAILURE:
        checks = ", ".join(ci.failing) or "one or more checks"
        return ReviewEvent.REQUEST_CHANGES, (
            f"CI is failing for {pr_slug} (head {head}): {checks}. Not reviewing "
            f"content until CI is green.\n\nVERDICT: REQUEST_CHANGES"
        )
    if ci.state is CiState.PENDING:
        return ReviewEvent.COMMENT, (
            f"CI is still running for {pr_slug} (head {head}). Holding review until the "
            f"checks complete — not approving while CI is pending (fail-closed)."
        )
    # UNKNOWN
    return ReviewEvent.COMMENT, (
        f"CI status for {pr_slug} (head {head}) could not be determined (fail-closed) — "
        f"not approving until CI is confirmed green. If this persists, check that "
        f"MINDWIRE_NAYSAYER_GITHUB_TOKEN has `Actions: Read-only`."
    )


@dataclass(frozen=True)
class DiffView:
    """The pre-truncation facts about a diff, paired with the text the model sees.

    Introduced by T-gate-silently-suppresses-approve-on-truncated-diff so that
    ``original_chars`` (the length BEFORE truncation) is captured at the same call site
    that produces ``text`` — every consumer of one is holding the other, and the gate
    notice's numbers cannot silently drift from the messages the model was sent.
    """

    text: str
    original_chars: int
    limit: int
    warn_threshold: int

    @property
    def truncated(self) -> bool:
        # Boundary: original_chars == limit is NOT truncated (the last-char-fit case). The
        # 24-case matrix in the tests pins both sides of this.
        return self.original_chars > self.limit

    @property
    def in_headroom(self) -> bool:
        # In the [warn_threshold, limit] band: the "close to the cliff, but the review is
        # still whole" state. Mutually exclusive with ``truncated`` by construction.
        return self.warn_threshold <= self.original_chars <= self.limit


def _make_diff_view(diff: str) -> DiffView:
    """Truncate ``diff`` to the review cap and pair the result with the pre-cap length.

    Replaces the bare ``str``-returning ``_truncate_diff``: an earlier design lost
    ``original_chars`` on the way to the verdict-resolution site, and the gate notice
    could not honestly say "51,829 chars were never sent to the model" from a truncated
    string alone. Here the length is captured BEFORE truncation, at the same call.
    """
    text = diff[:_MAX_DIFF_CHARS] + "\n\n[diff truncated]" if len(diff) > _MAX_DIFF_CHARS else diff
    return DiffView(
        text=text,
        original_chars=len(diff),
        limit=_MAX_DIFF_CHARS,
        warn_threshold=_DIFF_WARN_THRESHOLD,
    )


def _build_messages(text: str, pr_slug: str) -> list[ChatMessage]:
    """Pass-1 (verdict) messages — the SINGLE entry point for the pass-1 system prompt.

    ``text`` is the ALREADY-TRUNCATED diff body (i.e. ``DiffView.text``): truncation is
    performed ONCE at the fetch site via :func:`_make_diff_view` and threaded down as the
    view's ``.text``. This function does not re-invoke :func:`_make_diff_view` — doing so
    would run the truncation twice for the same review (a dual-management error the round-
    3 PR-gate review of PR #186 caught: the raw ``diff`` string was still surviving into
    the message builders alongside the ``view``, so both were computing the truncation
    independently). Tests can pass a plain short string here — that string is treated as
    already-truncated text, the same contract the production driver honours.

    Tests import this to construct the exact system message the driver sends, so any
    later drift between "what the test asserts on" and "what the driver actually sends"
    fails a test (T1 anti-tautology). ADR-17 D-1: the 5-principles SOT is injected
    verbatim via ``build_preamble()`` in the same single entry point the design-time
    agent uses, so a one-place edit to ``spec/NAYSAYER_PRINCIPLES.md`` propagates to
    both surfaces (fail-loud: a missing/blank SOT raises).
    """
    system = build_pr_review_pass1_system_prompt(verdict_task_prompt=_PR_REVIEW_SYSTEM_PROMPT)
    user = (
        f"Review the diff for pull request {pr_slug}. Critique it, quoting the "
        f"specific hunks you object to, and end with your VERDICT line.\n\n"
        f"```diff\n{text}\n```"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


def _build_pass2_messages(text: str, pr_slug: str) -> list[ChatMessage]:
    """Pass-2 (ADR-pointer) messages — the sibling entry point for the pass-2 prompt.

    ``text`` is the ALREADY-TRUNCATED diff body — same contract as :func:`_build_messages`
    (see its docstring). Because both passes are handed the same ``DiffView.text`` by the
    driver, pass 2 sees exactly the evidence pass 1 sees; a pointer emitted against a
    truncated hunk was at least judged from the same view.
    """
    return build_pr_review_pass2_messages(text, pr_slug)


# The pass-2 (ADR-pointer collection) call gets its OWN, shorter timeout so a wedged
# pass-2 cannot delay the pass-1 verdict + PR-review submission (msg-694 §3-3 fail-open,
# msg-696 §4 short pass-B timeout). Half the pass-1 budget keeps pass 2 comfortably
# generous for the small JSON payload it should produce, while ensuring a pass-2 hang
# still returns control to the driver within the pass-1 window rather than blocking it.
_ADR_POINTER_TIMEOUT_SECONDS = _DEFAULT_TIMEOUT_SECONDS / 2

# ``max_tokens`` for pass 2. Deliberately smaller than pass 1 because pass 2's entire
# legitimate output is a small JSON array (see MAX_POINTER_PAYLOAD_BYTES rationale in
# :mod:`.pr_review_adr_pointers`). A tighter budget also caps the pathological runaway:
# even if the model ignores the "JSON only" instruction, it cannot spend the full 32 000-
# token pass-1 output budget on prose here.
_ADR_POINTER_MAX_TOKENS = 4000


def _not_attempted_selection() -> AdrPointerSelection:
    """A synthesised ``unavailable(not-attempted)`` selection for short-circuit paths.

    Used by CI-gate, head-unchanged skip, round-cap escalation — paths where the driver
    intentionally does NOT run pass 2 (nothing to point at, or nothing to review). Keeps
    the marker invariant intact: every posted body carries the marker line, and
    ``not-attempted`` is a distinct log-side reason from a call that ran and failed.
    """
    return AdrPointerSelection(
        available=False,
        unavailable_reason="not-attempted",
        measured_bytes=0,
    )


def _call_failed_selection(exc: BaseException) -> AdrPointerSelection:
    """A synthesised ``unavailable(call-failed)`` for a pass-2 call that raised.

    Any exception on pass 2 (timeout / HTTP error / cancellation / anything unexpected)
    collapses to this — pass 2 is fail-open (msg-694 §3-3): its failure never propagates
    into pass 1's verdict path. The exception message is retained for the M5' log line
    only; the marker itself stays reasonless (msg-705 M3).
    """
    return AdrPointerSelection(
        available=False,
        unavailable_reason=f"call-failed: {type(exc).__name__}",
        measured_bytes=0,
    )


def _log_pass2(pr_slug: str, selection: AdrPointerSelection, raw: str) -> None:
    """Emit the M5' log line for a pass-2 outcome (unavailable OR available)."""
    if selection.available:
        line = available_log_line(selection, raw)
    else:
        line = unavailable_log_line(selection)
    if line:
        logger.info("%s (%s)", line, pr_slug)


class NaysayerPrReviewDriver:
    """Independent PR-diff code review via Lexora (one-shot) + GitHub (T20 → ADR-19 driver)."""

    def __init__(
        self,
        *,
        lexora: LexoraChatClient | None = None,
        github: GitHubReviewClient | None = None,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        lexora_url: str | None = None,
        github_token: str | None = None,
        skip_if_head_unchanged: bool = False,
        max_review_rounds: int = 0,
        review_login: str = _DEFAULT_REVIEW_LOGIN,
        shadow: bool = False,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._skip_if_head_unchanged = skip_if_head_unchanged
        self._max_review_rounds = max_review_rounds
        self._review_login = review_login
        self._shadow = shadow
        self._lexora: LexoraChatClient = (
            lexora
            if lexora is not None
            else LexoraClient(lexora_url, timeout_seconds=timeout_seconds)
        )
        # T22: the naysayer authenticates as a SEPARATE GitHub identity (spirrowgames-ops via
        # MINDWIRE_NAYSAYER_GITHUB_TOKEN) so its APPROVE / REQUEST_CHANGES is not "approving your
        # own PR". An explicit github_token arg wins (tests / overrides); otherwise resolve the
        # naysayer identity (which falls back to the shared token until the distinct one is
        # provisioned — see naysayer_github_token).
        self._github: GitHubReviewClient = (
            github
            if github is not None
            else GitHubClient(github_token if github_token is not None else naysayer_github_token())
        )

    async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
        """Review one PR: L1 CI-gate → (if green) Lexora judge → post critique → submit review.

        ``post_critique`` posts the critique body to the review thread; it is invoked **before**
        the GitHub submission so the human still sees the critique even if the submit fails.
        Returns the :class:`PrReviewOutcome`. Raises on an empty model reply or an unreachable
        Lexora / GitHub (fail-closed — the caller never treats a failed review as a pass).

        Marker invariant (msg-690 M2, msg-692 §3): every body this driver posts (CI-gate,
        debounce skip/cap, timeout-degrade, normal APPROVE/REQUEST_CHANGES) carries the ADR-
        pointer marker as its final line — a short-circuit path stamps ``ADR-INDEX: unavailable``
        because pass 2 was not attempted, so an inspecting human can always see whether the
        review carried an ADR-index cross-check.
        """
        # L1 CI-gate (ADR-16 §D-2): the APPROVE must imply CI green for the reviewed head SHA.
        # Query CI BEFORE the (costly) content review and short-circuit when it is not green —
        # fail-closed: failure / pending / UNKNOWN never APPROVE (fetch_ci_status never raises).
        ci = await self._github.fetch_ci_status(pr)
        if ci.state is not CiState.SUCCESS:
            verdict, body = _ci_gate_response(ci, pr.slug)
            selection = _not_attempted_selection()
            body = append_marker(body, selection)
            await post_critique(body)
            await self._submit_review(pr, verdict, body)  # no Lexora call — gate stands in
            return PrReviewOutcome(
                verdict=verdict,
                body=body,
                ci_state=ci.state,
                head_sha=ci.head_sha,
                ci_gated=True,
                adr_pointer_selection=selection,
            )

        # Debounce (cost lever, default off): consult the PR's prior naysayer reviews to (a) skip a
        # re-review of an unchanged head, or (b) cap the re-review rounds — both BEFORE the costly
        # Lexora/Gemini call, mirroring the L1 CI-gate short-circuit above. Fail-soft: an unreadable
        # / empty review list disables both and falls through to a full review. In ``shadow`` mode
        # the same decisions are computed and LOGGED (the counterfactual saving) but NOT acted on —
        # the full review still runs, so behaviour / coverage are unchanged while the saving is
        # measured.
        would_skip_head_unchanged = False
        would_cap = False
        if self._skip_if_head_unchanged or self._max_review_rounds > 0:
            prior = [
                r for r in await self._github.fetch_pr_reviews(pr) if r.login == self._review_login
            ]
            skip = (
                self._skip_unchanged_response(pr, ci, prior)
                if self._skip_if_head_unchanged
                else None
            )
            # Count only the naysayer's own VERDICT reviews toward the cap (same _VERDICT_STATES as
            # _latest_verdict_review). A CI-gate-hold COMMENT, a DISMISSED review, or the escalation
            # COMMENT spent no Gemini review, so they must not inflate the round count — else a
            # comment-only interaction would prematurely, and then permanently, escalate the gate
            # (Copilot + naysayer review on #113).
            verdict_rounds = sum(1 for r in prior if r.state in _VERDICT_STATES)
            cap_hit = 0 < self._max_review_rounds <= verdict_rounds
            # Skip takes precedence over the cap: the enforcing path returns on a skip before it
            # ever evaluates the cap, so a single ``if skip ... elif cap_hit`` keeps shadow mode on
            # the same precedence — else a PR meeting both would double-count (naysayer #114).
            if skip is not None:
                if self._shadow:
                    would_skip_head_unchanged = True
                    logger.info(
                        "naysayer debounce SHADOW: would SKIP %s (head %s already reviewed) "
                        "— 1 Gemini review saved",
                        pr.slug,
                        ci.head_sha,
                    )
                else:
                    verdict, body = skip
                    selection = _not_attempted_selection()
                    body = append_marker(body, selection)
                    await post_critique(body)
                    return PrReviewOutcome(
                        verdict=verdict,
                        body=body,
                        ci_state=ci.state,
                        head_sha=ci.head_sha,
                        skipped_head_unchanged=True,
                        adr_pointer_selection=selection,
                    )
            elif cap_hit:
                if self._shadow:
                    would_cap = True
                    logger.info(
                        "naysayer debounce SHADOW: would CAP %s (%d verdict reviews >= cap %d) "
                        "— 1 Gemini review saved",
                        pr.slug,
                        verdict_rounds,
                        self._max_review_rounds,
                    )
                else:
                    body = (
                        f"Naysayer review-round cap reached for {pr.slug} "
                        f"({verdict_rounds} prior verdict reviews >= "
                        f"cap {self._max_review_rounds}). "
                        f"Escalating to the human (Tier-C) instead of spending another review — "
                        f"the back-and-forth should be adjudicated, not re-litigated by the gate."
                        f"\n\nVERDICT: COMMENT"
                    )
                    selection = _not_attempted_selection()
                    body = append_marker(body, selection)
                    await post_critique(body)
                    await self._submit_review(pr, ReviewEvent.COMMENT, body)
                    return PrReviewOutcome(
                        verdict=ReviewEvent.COMMENT,
                        body=body,
                        ci_state=ci.state,
                        head_sha=ci.head_sha,
                        rounds_capped=True,
                        adr_pointer_selection=selection,
                    )

        # CI green → content review (Lexora). Fail-closed: an unreachable GitHub raises here.
        diff = await self._github.fetch_pr_diff(pr)
        # Capture the pre-truncation view ONCE, at the fetch site, so ``original_chars`` and
        # the truncated ``text`` the model actually sees are paired forever. Every downstream
        # consumer (message builders, both Lexora passes, verdict resolution, gate notice)
        # reads from ``view`` — the raw ``diff`` local is not passed further and the message
        # builders take ``view.text`` rather than the raw diff, so the truncation is computed
        # ONCE and never recomputed (T-gate-silently-suppresses-approve-on-truncated-diff,
        # round-3 PR-gate finding on PR #186 corrected the earlier draft that recomputed).
        view = _make_diff_view(diff)
        truncated = view.truncated

        # A-3 two-pass structure (msg-692 §1): run pass 1 (verdict) and pass 2 (ADR-pointer
        # collection) in parallel. Pass 1 = judge; pass 2 = index-injected hint collection whose
        # output cannot alter the verdict (structural guarantee — the driver never reads a
        # verdict token from pass 2's return value). Both fire against the SAME diff at the SAME
        # commit, so a reviewer can trust the ADR pointer section corresponds to the same
        # evidence the verdict was formed on.
        pass1_result, pass2_selection, pass2_raw = await self._run_two_passes(view, pr.slug)

        if isinstance(pass1_result, LexoraTimeoutError):
            # T-infra-failure-posts-empty-rc: pass 1 did not finish within the client timeout.
            # Degrade to a COMMENT-hold (addressed to the human) via the same post_critique +
            # _submit_review path, rather than letting the timeout propagate and crash the
            # pipeline. COMMENT (not REQUEST_CHANGES): the gate did not reach a verdict, and
            # an empty RC would trigger the conductor to spawn the implementer against a body
            # that carries no fix — see :meth:`_degrade_on_timeout`. Pass 2's own outcome
            # (whatever it was — usually also unavailable when Lexora is wedged) is stamped on
            # the marker of the degrade body, keeping the marker invariant intact.
            return await self._degrade_on_timeout(
                pr,
                ci,
                pass2_selection=pass2_selection,
                pass2_raw=pass2_raw,
                post_critique=post_critique,
                would_skip_head_unchanged=would_skip_head_unchanged,
                would_cap=would_cap,
            )
        if isinstance(pass1_result, BaseException):
            # Non-timeout Lexora / other exception on pass 1 keeps propagating (fail-loud). Pass
            # 2's coroutine already completed one way or the other (return_exceptions=True), so
            # we do not leak a background task by raising here.
            raise pass1_result

        completion = pass1_result
        body = (completion.content or "").strip()
        if not body:
            raise NaysayerPrReviewError(
                f"naysayer returned empty review (finish_reason={completion.finish_reason!r}) "
                f"for {pr.slug}; refusing to post/submit empty"
            )
        # Compute the FULL decision (model verdict + gate verdict + suppression fact) from
        # one call: this is the ONLY site the raw critique is parsed on the notice path, and
        # the same ``decision`` feeds both the ``verdict`` posted to GitHub and the notice
        # prepended to the body — the two cannot drift. ``verdict`` matches the pre-change
        # gate rule (force-RC on any partial review) at all 24 combinations of the input
        # matrix — see ``_oracle_gate_verdict`` in tests/test_pr_review_driver.py, which
        # re-states the rule as a 3-line reference and asserts equivalence.
        decision = decide_verdict(body, view=view, finish_reason=completion.finish_reason)
        verdict = decision.gate_verdict
        _log_objections(pr.slug, ci.head_sha, decision)
        # Prepend the gate notice BEFORE the ADR-pointer marker is appended: the notice sits
        # at the head of the body so a reader who sees ``CHANGES_REQUESTED`` in the review
        # state finds the reason at the top of the critique, not after a possibly-truncated
        # tail (msg-1872 D-4). The notice is "" when no axis fired, in which case body is
        # returned unchanged (msg-1876 invariant 6: no marker → no sentinel).
        body = prepend_gate_notice(body, decision)
        # Stamp the ADR-pointer section + marker onto the body BEFORE posting / submitting, so
        # the chat-room copy, the GitHub review, and the outcome all carry the same rendered
        # body (the marker is the final line — the driver relies on that fixed position).
        _log_pass2(pr.slug, pass2_selection, pass2_raw)
        body = append_marker(body, pass2_selection)

        await post_critique(body)
        # Fail-closed: unreachable GitHub raises here too (posted first, so the human sees it).
        await self._submit_review(pr, verdict, body)
        return PrReviewOutcome(
            verdict=verdict,
            body=body,
            ci_state=ci.state,
            head_sha=ci.head_sha,
            truncated=truncated,
            finish_reason=completion.finish_reason,
            model=completion.model or self._model,
            principles_version=principles_version(),
            would_skip_head_unchanged=would_skip_head_unchanged,
            would_cap=would_cap,
            adr_pointer_selection=pass2_selection,
        )

    async def _run_two_passes(
        self, view: DiffView, pr_slug: str
    ) -> tuple[Any, AdrPointerSelection, str]:
        """Execute pass 1 + pass 2 concurrently, return their (typed) outcomes.

        ``view`` is the :class:`DiffView` produced ONCE at the fetch site by
        :func:`_make_diff_view`. Both passes read ``view.text`` — the raw diff string
        does not reach this method, so the truncation cannot be recomputed here (round-3
        PR-gate finding on PR #186). Same view = same evidence for both passes.

        Returns a triple:

        * ``pass1_result`` — the ``ChatCompletion`` on success, OR the exception raised by
          pass 1 (``LexoraTimeoutError`` for the safe-degrade path, or any other Exception
          for the fail-loud path). The caller inspects it with ``isinstance``.
        * ``pass2_selection`` — the :class:`AdrPointerSelection` produced by running pass 2's
          raw ``content`` through the M1'/M2 pipeline. On any pass-2 failure (timeout /
          HTTP error / cancellation) this is a synthesised ``unavailable(call-failed)``
          selection so the marker is always well-defined (fail-open).
        * ``pass2_raw`` — the raw ``content`` string pass 2 returned (empty string on
          failure). Retained for the M5' log line (msg-701 §2, msg-703 M5').
        """
        pass1_task = self._lexora.chat_completion(
            model=self._model,
            messages=_build_messages(view.text, pr_slug),
            max_tokens=self._max_tokens,
        )
        pass2_task = self._collect_adr_pointers(view.text, pr_slug)
        # ``return_exceptions=True`` isolates the two passes: pass 2's exception must never
        # reach the caller (fail-open), and pass 1's exception is inspected below so the
        # LexoraTimeoutError → degrade path is preserved while other exceptions still
        # propagate fail-loud.
        pass1_outcome, pass2_outcome = await asyncio.gather(
            pass1_task, pass2_task, return_exceptions=True
        )
        if isinstance(pass2_outcome, BaseException):
            # Pass 2 collapses to fail-open unavailable on ANY exception (timeout, HTTP error,
            # cancellation, unexpected). msg-694 §3-3.
            return pass1_outcome, _call_failed_selection(pass2_outcome), ""
        pass2_selection, pass2_raw = pass2_outcome
        return pass1_outcome, pass2_selection, pass2_raw

    async def _collect_adr_pointers(
        self, text: str, pr_slug: str
    ) -> tuple[AdrPointerSelection, str]:
        """Run pass 2 → M1'/M2 pipeline. Returns (selection, raw_content).

        ``text`` is the ALREADY-TRUNCATED ``DiffView.text`` — same evidence pass 1 sees,
        same contract as :func:`_build_pass2_messages` (see its docstring).

        Applies :data:`_ADR_POINTER_TIMEOUT_SECONDS` as the pass-2 budget via
        ``asyncio.wait_for``: a wedged pass 2 cannot exceed this budget even if the Lexora
        client timeout is longer. On timeout the pipeline gets no content and produces
        ``unavailable(call-failed)`` — the exception is caught here so pass 2's timeout can
        never itself be observed by the caller (pass 2 is fail-open).
        """
        try:
            completion = await asyncio.wait_for(
                self._lexora.chat_completion(
                    model=self._model,
                    messages=_build_pass2_messages(text, pr_slug),
                    max_tokens=_ADR_POINTER_MAX_TOKENS,
                ),
                timeout=_ADR_POINTER_TIMEOUT_SECONDS,
            )
        except (TimeoutError, LexoraTimeoutError) as exc:
            return _call_failed_selection(exc), ""
        raw = completion.content or ""
        manifest_ids = load_manifest_ids()
        selection = select_adr_pointers(raw, manifest_ids)
        return selection, raw

    async def _degrade_on_timeout(
        self,
        pr: PrRef,
        ci: CiStatus,
        *,
        pass2_selection: AdrPointerSelection | None = None,
        pass2_raw: str = "",
        post_critique: PostCritique,
        would_skip_head_unchanged: bool = False,
        would_cap: bool = False,
    ) -> PrReviewOutcome:
        """T-infra-failure-posts-empty-rc: a timed-out Lexora review → COMMENT-hold + human stop.

        A pass-1 client timeout is an INFRA failure — the gate itself did not complete, so no
        verdict was reached. Post an explanatory critique addressed to the HUMAN, submit a
        ``COMMENT`` review (not ``REQUEST_CHANGES``), and return with ``timed_out=True`` so the
        timeout is observable. ``REQUEST_CHANGES`` was the previous choice; the failure mode it
        produced — the conductor spawning the implementer on an empty relay with nothing to fix
        — was worse than the fail-closed asymmetry it bought (T-infra-failure-posts-empty-rc §1).

        The judging rule this path now follows is the one already applied elsewhere in this
        module: ``REQUEST_CHANGES`` is a fix signal, so it is reserved for reviews whose body
        carries implementer-actionable content (a failing CI check, an objection block, a
        parseable critique). Reviews where the gate itself could not finish (CI UNKNOWN in
        :func:`_ci_gate_response`; the round-cap escalation above; this timeout path) post a
        ``COMMENT`` and let the conductor's non-RC branch stop at the human. No new conductor
        wiring is required — the routing at ``conductor/core.py`` already treats non-RC verdicts
        as ``StopReason.HUMAN``.

        ``COMMENT`` is not one of ``_VERDICT_STATES``, so the ``skip_if_head_unchanged`` reuse
        path (:meth:`_latest_verdict_review`) cannot pick this posting up as the "already
        reviewed this head" verdict; a timeout body will not calcify as the head's standing
        verdict and be re-served on subsequent fires.

        The pass-2 selection (may be a real outcome if pass 2 finished before pass 1 timed out,
        or ``call-failed`` if it too failed) is stamped on the marker so the timeout-degrade
        body carries the marker like every other body — the marker invariant does not weaken
        on the safe-degrade path.
        """
        head = ci.head_sha or "?"
        body = (
            # 1. This is NOT a fix request — do not push a "fix" on the strength of this post.
            f"This is not a fix request. Do not push a change on the strength of this posting: "
            f"the naysayer gate for {pr.slug} did not reach a verdict, so there is no critique to "
            f"answer.\n\n"
            # 2. What failed (client timeout / head SHA).
            f"What happened: the naysayer's pass-1 review call exceeded the configured Lexora "
            f"client timeout against head {head} and did not complete.\n\n"
            # 3. No verdict was reached ∴ this PR has NOT been approved by the gate.
            f"No verdict was reached. This PR has not been approved by the naysayer gate; do not "
            f"merge on the strength of this review.\n\n"
            # 4. Addressed to the human: re-fire the gate, or adjudicate.
            f"Addressed to the human: re-fire the gate against the current head, or adjudicate "
            f"this PR directly (Tier-C).\n\n"
            f"VERDICT: COMMENT"
        )
        selection = pass2_selection if pass2_selection is not None else _not_attempted_selection()
        _log_pass2(pr.slug, selection, pass2_raw)
        body = append_marker(body, selection)
        await post_critique(body)
        await self._submit_review(pr, ReviewEvent.COMMENT, body)
        return PrReviewOutcome(
            verdict=ReviewEvent.COMMENT,
            body=body,
            ci_state=ci.state,
            head_sha=ci.head_sha,
            model=self._model,
            principles_version=principles_version(),
            timed_out=True,
            # Preserve the shadow counterfactual flags across the timeout degrade so per-PR
            # object-level telemetry matches the SHADOW log lines (naysayer #114 weakest point).
            would_skip_head_unchanged=would_skip_head_unchanged,
            would_cap=would_cap,
            adr_pointer_selection=selection,
        )

    @staticmethod
    def _latest_verdict_review(prior: list[ReviewInfo]) -> ReviewInfo | None:
        """The naysayer's most recent review carrying a real verdict (APPROVED / CHANGES_REQUESTED).

        COMMENTED / PENDING / DISMISSED carry no verdict, so they never define "already reviewed
        this head". ``submitted_at`` is ISO-8601 (lexicographically sortable); a missing value sorts
        oldest.
        """
        verdicts = [r for r in prior if r.state in _VERDICT_STATES]
        if not verdicts:
            return None
        return max(verdicts, key=lambda r: r.submitted_at or "")

    def _skip_unchanged_response(
        self, pr: PrRef, ci: CiStatus, prior: list[ReviewInfo]
    ) -> tuple[ReviewEvent, str] | None:
        """A (verdict, body) reusing the prior verdict iff the naysayer already reviewed this head.

        Returns ``None`` (→ proceed to a full review) when the head SHA is unknown, there is no
        prior verdict review, or the latest verdict review was against a different commit.
        """
        if ci.head_sha is None:
            return None
        latest = self._latest_verdict_review(prior)
        if latest is None or latest.commit_id != ci.head_sha:
            return None
        verdict = ReviewEvent.APPROVE if latest.state == "APPROVED" else ReviewEvent.REQUEST_CHANGES
        body = (
            f"No change since the last naysayer review of {pr.slug} "
            f"(head {ci.head_sha[:12]}): the prior verdict {verdict.value} stands. "
            f"Skipping a re-review of an unchanged head (cost lever).\n\nVERDICT: {verdict.value}"
        )
        return verdict, body

    async def _submit_review(self, pr: PrRef, verdict: ReviewEvent, body: str) -> None:
        """Submit the PR review, falling back to COMMENT on the same-identity 422.

        GitHub forbids a formal APPROVE / REQUEST_CHANGES on your *own* PR. T22 provisions the
        naysayer a distinct identity (``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` = ``spirrowgames-ops``) so
        the formal verdict goes through. This COMMENT fallback backstops the two ways the two
        identities can still coincide: (a) the window before that token is provisioned, and
        (b) — measured on PR #194, 2026-08-29 — a PR **opened by** ``spirrowgames-ops``, which
        collides from the other end and is not fixed by any token change. We re-submit the same
        body as a COMMENT so the verdict (in the body) is still recorded, rather than fail-closed-
        halting on a credential-config issue.

        A COMMENT is NOT a formal verdict: it applies no ``CHANGES_REQUESTED`` block and
        ``_latest_verdict_review`` does not see it. Case (b) therefore degrades the gate to
        advisory for the whole life of that PR — the fix is to re-open the PR under the author
        identity, not to lean on this fallback.

        Until 2026-08-29 this fallback was dead code: it branches on text that
        :func:`~spirrow_mindwire.github.client._error_detail` dropped. See that docstring.
        """
        try:
            await self._github.submit_review(pr, event=verdict, body=body)
        except GitHubHTTPError as exc:
            if exc.status_code == 422 and "own pull request" in str(exc).lower():
                await self._github.submit_review(pr, event=ReviewEvent.COMMENT, body=body)
            else:
                raise

    async def aclose(self) -> None:
        """Close the shared Lexora + GitHub clients (driver teardown)."""
        await self._lexora.aclose()
        await self._github.aclose()


__all__ = [
    "NaysayerPrReviewDriver",
    "NaysayerPrReviewError",
    "PrReviewOutcome",
]
