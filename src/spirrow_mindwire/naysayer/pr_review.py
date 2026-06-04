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
2. the **injection-safe verdict parsing** (last standalone ``VERDICT:`` line; never APPROVE on a
   truncated / length-capped review);
3. the **T22 GitHub-review submission** as the separate ``spirrowgames-ops`` identity, with the
   same-identity 422 → COMMENT fallback.

Only the adversarial *judgement* is delegated — to Lexora's ``naysayer`` (Gemini) tier via a
**one-shot** ``chat_completion`` (``transport != judge``: spinning up an Agent SDK loop to read a
static diff would be YAGNI — msg-430/432). The 5-principles SOT is injected verbatim via the SAME
:func:`~spirrow_mindwire.naysayer.principles.build_preamble` entry point the design-time agent
uses (ADR-17 D-1) — that single judging-behavior core unifies the two surfaces; the transport is
optimised per surface.

Fail modes (ADR-07 §2.6, fail-closed): Lexora **or** GitHub unreachable → the call raises (the
caller fail-loud); an empty Lexora reply is likewise fail-loud.
"""

from __future__ import annotations

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
    naysayer_github_token,
)
from ..lexora.client import ChatMessage, LexoraChatClient, LexoraClient
from .principles import NAYSAYER_MODEL_TIER, build_preamble, principles_version

_DEFAULT_MODEL = NAYSAYER_MODEL_TIER  # N-4: pinned in one place (naysayer.principles)
# Gemini 3.1 Pro (the post-T15 naysayer tier) is a reasoning model: its reasoning tokens count
# against the output budget (~4k observed), so the old 4096 was spent almost entirely on
# reasoning and the visible critique was truncated mid-sentence (finish_reason=length, which
# _resolve_verdict then forced to REQUEST_CHANGES). Size for reasoning + a full critique;
# matches the Lexora gateway's 16000. (The §A.3 floor of 1500 dates from the DeepSeek-V4-Flash era.)
_DEFAULT_MAX_TOKENS = 16000
_DEFAULT_TIMEOUT_SECONDS = 900.0
_MAX_DIFF_CHARS = 60_000  # truncate enormous diffs to stay within the model budget

# A verdict must be its own line (``^...$`` with MULTILINE). Diff hunk lines carry a +/-/space
# prefix, so a ``VERDICT: APPROVE`` *inside the reviewed diff* (prompt injection) never satisfies
# ``^\s*VERDICT:`` — only a real verdict line the model emits does. We take the LAST such line
# (the model's final verdict), so an APPROVE quoted earlier cannot flip the gate open.
_VERDICT_RE = re.compile(
    r"^\s*VERDICT:\s*(APPROVE|REQUEST[ _-]?CHANGES|COMMENT)\s*$",
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

End your reply with exactly one verdict line:
  VERDICT: APPROVE          (no blocking problems)
  VERDICT: REQUEST_CHANGES  (at least one blocking problem)

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


def _parse_verdict(critique: str) -> ReviewEvent:
    """Extract the verdict; default-safe to REQUEST_CHANGES (never silent approve).

    Takes the **last** standalone ``VERDICT:`` line so an APPROVE quoted earlier (or injected via
    the reviewed diff) cannot override the model's real verdict.
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


def _build_messages(diff: str, pr_slug: str) -> list[ChatMessage]:
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n\n[diff truncated]"
    # ADR-17 D-1: the 5-principles SOT is injected verbatim via the SAME single entry point
    # (``build_preamble()``) the design-time agent uses, so a one-place edit to
    # ``spec/NAYSAYER_PRINCIPLES.md`` propagates to BOTH surfaces and the principles are never
    # restated as a prompt literal here (fail-loud: a missing/blank SOT raises).
    system = f"{build_preamble()}\n\n{_PR_REVIEW_SYSTEM_PROMPT}"
    user = (
        f"Review the diff for pull request {pr_slug}. Critique it, quoting the "
        f"specific hunks you object to, and end with your VERDICT line.\n\n"
        f"```diff\n{diff}\n```"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


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
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
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
        """
        # L1 CI-gate (ADR-16 §D-2): the APPROVE must imply CI green for the reviewed head SHA.
        # Query CI BEFORE the (costly) content review and short-circuit when it is not green —
        # fail-closed: failure / pending / UNKNOWN never APPROVE (fetch_ci_status never raises).
        ci = await self._github.fetch_ci_status(pr)
        if ci.state is not CiState.SUCCESS:
            verdict, body = _ci_gate_response(ci, pr.slug)
            await post_critique(body)
            await self._submit_review(pr, verdict, body)  # no Lexora call — gate stands in
            return PrReviewOutcome(
                verdict=verdict, body=body, ci_state=ci.state, head_sha=ci.head_sha, ci_gated=True
            )

        # CI green → content review (Lexora). Fail-closed: an unreachable GitHub raises here.
        diff = await self._github.fetch_pr_diff(pr)
        truncated = len(diff) > _MAX_DIFF_CHARS
        completion = await self._lexora.chat_completion(
            model=self._model,
            messages=_build_messages(diff, pr.slug),
            max_tokens=self._max_tokens,
        )
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
        )

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

    async def health(self) -> dict[str, Any]:
        """Lexora reachability probe (observability)."""
        details: dict[str, Any] = {}
        try:
            lexora_health = await self._lexora.health()
        except Exception as exc:  # health probe never raises — record unreachable + continue
            details["lexora_health"] = f"unreachable: {exc}"
        else:
            details["lexora_health"] = lexora_health.get("status", lexora_health)
        return details

    async def aclose(self) -> None:
        """Close the shared Lexora + GitHub clients (driver teardown)."""
        await self._lexora.aclose()
        await self._github.aclose()


__all__ = [
    "NaysayerPrReviewDriver",
    "NaysayerPrReviewError",
    "PrReviewOutcome",
]
