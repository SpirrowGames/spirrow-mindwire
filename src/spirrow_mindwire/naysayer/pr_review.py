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
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
    unavailable_log_line,
)
from .principles import NAYSAYER_MODEL_TIER, principles_version

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = NAYSAYER_MODEL_TIER  # N-4: pinned in one place (naysayer.principles)
# Gemini 3.1 Pro (the current naysayer tier) is a reasoning model with a 1M+ token context: its
# reasoning tokens count against the OUTPUT budget, so a real review of a large diff spends a big
# slice on reasoning before emitting the critique + VERDICT line. The old 16000 ran out mid-review
# on big PRs (finish_reason=length, which _resolve_verdict then forces to REQUEST_CHANGES — a false
# RC); 32000 leaves room for reasoning plus a complete critique (connector-relay confirmed ~30k is
# enough in practice).
_DEFAULT_MAX_TOKENS = 32000
# M3 (T34): the CLIENT timeout must be backend + margin so the client always outlives the backend:
# the backend therefore surfaces its result (completion / partial / error) as the response, and we
# never time out *before* it does (the old equal-900s tie could lose that race, producing a
# client-side TimeoutException with no backend answer). On a genuine timeout the driver degrades to
# a fail-closed REQUEST_CHANGES (M2), so the margin is about *who reports the timeout*, not safety.
# The backend fact (900s) is the SINGLE source of truth in ``lexora/client.py`` — imported here as
# ``LEXORA_BACKEND_TIMEOUT_SECONDS`` rather than re-hardcoded, so the two files cannot drift.
_CLIENT_TIMEOUT_MARGIN_SECONDS = 60.0
_DEFAULT_TIMEOUT_SECONDS = LEXORA_BACKEND_TIMEOUT_SECONDS + _CLIENT_TIMEOUT_MARGIN_SECONDS
# The REVIEWABILITY gate, not a context-capacity limit: this is the largest RAW diff the naysayer
# fully reviews and can therefore APPROVE. Beyond it the diff is truncated, and _resolve_verdict
# force-RCs a truncated review ("too big to review thoroughly in one shot — split the PR"). So the
# cap defines "small enough to review rigorously in a single pass", NOT "small enough to fit the
# model's context". The old 60_000 chars (~15-20k tokens) was below real PRs (e.g. #93 ~127k chars),
# so legitimate PRs got truncated → false RC. 150_000 chars covers the largest real PR seen so far
# (#93 ~127k) with margin while — given a fine task-splitting discipline (keep PRs small) — keeping
# the reviewability gate tight: a diff beyond this should have been split, so it truncates →
# force-RC. The truncate-then-never-APPROVE path is KEPT as the safety valve: a diff too big to see
# in one shot force-RCs rather than rubber-stamping on a partial view.
_MAX_DIFF_CHARS = 150_000

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
# verdict at all, and _parse_verdict defaults to REQUEST_CHANGES. A model that someday indents its
# verdict costs a red gate, not a false APPROVE.
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
# ---- Bold verdicts fail closed, deliberately (ruling: T-verdict-regex-space-prefix-injection) ----
#
# ``**VERDICT: APPROVE**`` does not match this pattern, so _parse_verdict returns REQUEST_CHANGES.
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
_VERDICT_RE = re.compile(
    r"^VERDICT:\s*(APPROVE|REQUEST[ _-]?CHANGES|COMMENT)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_PR_REVIEW_SYSTEM_PROMPT = """\
You are the independent naysayer performing adversarial CODE REVIEW of a pull \
request's diff in a Spirrow MindWire ChatRoom thread. You are a different model \
from the implementer. Assume the change is flawed until proven otherwise and \
find the real problems: correctness bugs, missing edge cases, security issues, \
broken invariants, untested behaviour, and regressions.

For every objection, quote the specific hunk/line you object to and explain the \
concrete flaw. Do not fabricate problems and do not pad with generic caveats. \
If, after a genuine search, you find no blocking problem, say so and name the \
single weakest remaining point.

End your reply with exactly one verdict line. It must begin at the start of the line and hold \
nothing else — no indentation, no bold or backticks, no trailing note. Write exactly one of \
these two lines, verbatim:

VERDICT: APPROVE

(use this one only if you found no blocking problem), or:

VERDICT: REQUEST_CHANGES

(use this one if you found at least one blocking problem).

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


def _parse_verdict(critique: str) -> ReviewEvent:
    """Extract the verdict; default-safe to REQUEST_CHANGES (never silent approve).

    Takes the **last** standalone ``VERDICT:`` line so an APPROVE quoted *earlier* cannot override
    the model's real verdict. Text injected via the reviewed diff is handled by the anchor rather
    than by this rule — see ``_VERDICT_RE``, and note that last-wins on its own is no defence when
    the quote comes after the verdict.
    """
    matches = _VERDICT_RE.findall(critique)
    if not matches:
        return ReviewEvent.REQUEST_CHANGES
    token = re.sub(r"[ _-]", "_", matches[-1].upper())
    if token == "APPROVE":
        return ReviewEvent.APPROVE
    # COMMENT / REQUEST_CHANGES / anything else → objection (gate stays closed).
    return ReviewEvent.REQUEST_CHANGES


def _resolve_verdict(critique: str, *, truncated: bool, finish_reason: str | None) -> ReviewEvent:
    """Verdict, but never APPROVE on a partial review (truncated diff / length cap)."""
    if truncated or finish_reason == "length":
        # The model did not see (or could not finish reviewing) the whole diff — approving a
        # partial review would be an unsafe gate. Force an objection.
        return ReviewEvent.REQUEST_CHANGES
    return _parse_verdict(critique)


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


def _truncate_diff(diff: str) -> str:
    if len(diff) > _MAX_DIFF_CHARS:
        return diff[:_MAX_DIFF_CHARS] + "\n\n[diff truncated]"
    return diff


def _build_messages(diff: str, pr_slug: str) -> list[ChatMessage]:
    """Pass-1 (verdict) messages — the SINGLE entry point for the pass-1 system prompt.

    Tests import this to construct the exact system message the driver sends, so any
    later drift between "what the test asserts on" and "what the driver actually sends"
    fails a test (T1 anti-tautology). ADR-17 D-1: the 5-principles SOT is injected
    verbatim via ``build_preamble()`` in the same single entry point the design-time
    agent uses, so a one-place edit to ``spec/NAYSAYER_PRINCIPLES.md`` propagates to
    both surfaces (fail-loud: a missing/blank SOT raises).
    """
    diff = _truncate_diff(diff)
    system = build_pr_review_pass1_system_prompt(verdict_task_prompt=_PR_REVIEW_SYSTEM_PROMPT)
    user = (
        f"Review the diff for pull request {pr_slug}. Critique it, quoting the "
        f"specific hunks you object to, and end with your VERDICT line.\n\n"
        f"```diff\n{diff}\n```"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


def _build_pass2_messages(diff: str, pr_slug: str) -> list[ChatMessage]:
    """Pass-2 (ADR-pointer) messages — the sibling entry point for the pass-2 prompt.

    Delegates to :func:`build_pr_review_pass2_messages` after applying the same diff
    truncation cap as pass 1: pass 2 sees the same evidence pass 1 sees, so a pointer
    emitted against a truncated hunk was at least judged from the same view.
    """
    return build_pr_review_pass2_messages(_truncate_diff(diff), pr_slug)


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
        truncated = len(diff) > _MAX_DIFF_CHARS

        # A-3 two-pass structure (msg-692 §1): run pass 1 (verdict) and pass 2 (ADR-pointer
        # collection) in parallel. Pass 1 = judge; pass 2 = index-injected hint collection whose
        # output cannot alter the verdict (structural guarantee — the driver never reads a
        # verdict token from pass 2's return value). Both fire against the SAME diff at the SAME
        # commit, so a reviewer can trust the ADR pointer section corresponds to the same
        # evidence the verdict was formed on.
        pass1_result, pass2_selection, pass2_raw = await self._run_two_passes(diff, pr.slug)

        if isinstance(pass1_result, LexoraTimeoutError):
            # M2 (T34): pass 1 did not finish within the client timeout. Degrade to fail-closed
            # REQUEST_CHANGES via the same post_critique + _submit_review path, rather than
            # letting the timeout propagate and crash the pipeline. Pass 2's own outcome
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
        # Never APPROVE a review the model could not fully see (truncated diff / token cap).
        verdict = _resolve_verdict(
            body, truncated=truncated, finish_reason=completion.finish_reason
        )
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
        self, diff: str, pr_slug: str
    ) -> tuple[Any, AdrPointerSelection, str]:
        """Execute pass 1 + pass 2 concurrently, return their (typed) outcomes.

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
            messages=_build_messages(diff, pr_slug),
            max_tokens=self._max_tokens,
        )
        pass2_task = self._collect_adr_pointers(diff, pr_slug)
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
        self, diff: str, pr_slug: str
    ) -> tuple[AdrPointerSelection, str]:
        """Run pass 2 → M1'/M2 pipeline. Returns (selection, raw_content).

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
                    messages=_build_pass2_messages(diff, pr_slug),
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
        """M2 (T34): a timed-out Lexora review → fail-closed REQUEST_CHANGES (never a silent pass).

        Mirrors the truncated-review path: post an explanatory critique, submit a REQUEST_CHANGES
        review, and return the :class:`PrReviewOutcome` with ``timed_out=True`` so the timeout is
        observable to the caller. The default verdict (REQUEST_CHANGES) keeps the gate on the same
        safe side as a length-capped / truncated review; whether a transient timeout should instead
        be a COMMENT-hold is the open question Q left for the naysayer / Tier-C (msg-503).

        The pass-2 selection (may be a real outcome if pass 2 finished before pass 1 timed out,
        or ``call-failed`` if it too failed) is stamped on the marker so the timeout-degrade
        body carries the marker like every other body — the marker invariant does not weaken
        on the safe-degrade path.
        """
        body = (
            f"The naysayer review for {pr.slug} exceeded the configured Lexora client timeout and "
            f"did not complete. A review that could not finish is treated as not-approved "
            f"(fail-closed), the same as a truncated/length-capped review: an unfinished review "
            f"must never APPROVE. Split the PR into smaller diffs or retry.\n\n"
            f"VERDICT: REQUEST_CHANGES"
        )
        selection = pass2_selection if pass2_selection is not None else _not_attempted_selection()
        _log_pass2(pr.slug, selection, pass2_raw)
        body = append_marker(body, selection)
        await post_critique(body)
        await self._submit_review(pr, ReviewEvent.REQUEST_CHANGES, body)
        return PrReviewOutcome(
            verdict=ReviewEvent.REQUEST_CHANGES,
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
        the formal verdict goes through. This COMMENT fallback remains a backstop for the window
        before that token is provisioned (the naysayer then shares the author identity and the
        verdict event 422s): we re-submit the same body as a COMMENT so the verdict (in the body)
        is still recorded, rather than fail-closed-halting on a credential-config issue.
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
