"""Stage 3 — ``NaysayerPrReviewAdapter`` (WIRING_ALLOWLIST_SPEC §A.3, T20).

Extends the independent naysayer from Stage 2's *design* critique to **PR diff
code review** on the develop→main PR (the Tier B gate, ADR-07 §2.2). On a
review-request message (carrying a PR ref) it:

1. parses the PR ref, fetches the unified diff from GitHub,
2. runs an adversarial code review through Lexora's ``model="naysayer"`` tier
   (DeepSeek V4-Flash, ``max_tokens >= 1500``) — independence inherited from
   ADR-05 §5 (a different model family from main),
3. posts the critique to the chatroom (``on_reply``) **and** submits a GitHub
   PR review with the verdict (``APPROVE`` / ``REQUEST_CHANGES``).

Verdict (§A.3): the model ends its critique with ``VERDICT: APPROVE`` or
``VERDICT: REQUEST_CHANGES``. An *objection* (request-changes) sends the work
back to the proposer↔implementer fix loop (not a human call); an *approve* is
the necessary condition for Takahito's merge GO (Tier C). Anything ambiguous
defaults to ``REQUEST_CHANGES`` — the naysayer never silently approves
(ADR-05 §5 / Stage 2 "disagree by default").

Fail modes (ADR-07 §2.6, fail-closed): Lexora **or** GitHub unreachable →
the delivery raises (session → ``FAILED``, dispatcher fail-loud) rather than
proceeding; an empty Lexora reply is likewise fail-loud (Stage 2 precedent).
A message with no PR ref is a no-op (ordinary thread chatter, not a request).

``capabilities`` carries ``NAYSAYER_QUALIFIED`` (independent model → may fill
the naysayer slot) and omits ``EXECUTE_CODE`` (review is advice + a PR-review
submission, no repo mutation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..exceptions import (
    AdapterDeliveryError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterSpawnError,
)
from ..github.client import (
    GitHubClient,
    GitHubHTTPError,
    GitHubReviewClient,
    PrRef,
    ReviewEvent,
    naysayer_github_token,
    parse_pr_ref,
)
from ..lexora.client import ChatMessage, LexoraChatClient, LexoraClient
from ..ports import SpawnContext
from ..ulid_util import new_ulid
from ..value_objects import (
    Capability,
    ChatroomEvent,
    ErrorInfo,
    EventType,
    HealthStatus,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_SHUTDOWN_STATES: frozenset[SessionState] = frozenset(
    {SessionState.HALTING, SessionState.HALTED, SessionState.FAILED}
)

_DEFAULT_MODEL = "naysayer"
_DEFAULT_MAX_TOKENS = 4096  # >= the reasoning-model 1500 floor (§A.3), with content headroom
_DEFAULT_TIMEOUT_SECONDS = 900.0
_MAX_DIFF_CHARS = 60_000  # truncate enormous diffs to stay within the model budget

# A verdict must be its own line (``^...$`` with MULTILINE). Diff hunk lines
# carry a +/-/space prefix, so a ``VERDICT: APPROVE`` *inside the reviewed diff*
# (prompt injection) never satisfies ``^\s*VERDICT:`` — only a real verdict line
# the model emits does. We take the LAST such line (the model's final verdict),
# so an APPROVE quoted earlier cannot flip the gate open.
_VERDICT_RE = re.compile(
    r"^\s*VERDICT:\s*(APPROVE|REQUEST[ _-]?CHANGES|COMMENT)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A message only triggers a review if it actually asks for one (least authority:
# a bare PR mention in the thread must not cause a GitHub review submission).
_REVIEW_REQUEST_RE = re.compile(r"\breview\b", re.IGNORECASE)

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


class NaysayerPrReviewSpawnError(AdapterSpawnError):
    """``spawn`` failure for the PR-review naysayer adapter (§3.4)."""


class NaysayerPrReviewDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the PR-review naysayer adapter (§3.4)."""


class NaysayerPrReviewHaltError(AdapterHaltError):
    """``halt`` failure for the PR-review naysayer adapter (§3.4)."""


class NaysayerPrReviewHealthError(AdapterHealthError):
    """``health`` failure for the PR-review naysayer adapter (§3.4)."""


@dataclass
class _Session:
    ctx: SpawnContext
    own_role: Role
    state: SessionState
    last_active_at: datetime
    target_pr: PrRef | None = None  # the PR this review thread is bound to
    error: ErrorInfo | None = None


def _parse_verdict(critique: str) -> ReviewEvent:
    """Extract the verdict; default-safe to REQUEST_CHANGES (never silent approve).

    Takes the **last** standalone ``VERDICT:`` line so an APPROVE quoted earlier
    (or injected via the reviewed diff) cannot override the model's real verdict.
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
        # The model did not see (or could not finish reviewing) the whole diff —
        # approving a partial review would be an unsafe gate. Force an objection.
        return ReviewEvent.REQUEST_CHANGES
    return _parse_verdict(critique)


def _build_messages(diff: str, pr_slug: str) -> list[ChatMessage]:
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n\n[diff truncated]"
    user = (
        f"Review the diff for pull request {pr_slug}. Critique it, quoting the "
        f"specific hunks you object to, and end with your VERDICT line.\n\n"
        f"```diff\n{diff}\n```"
    )
    return [
        ChatMessage(role="system", content=_PR_REVIEW_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user),
    ]


class NaysayerPrReviewAdapter:
    """RoleAdapter: independent PR-diff code review via Lexora + GitHub (T20)."""

    adapter_id: str = "naysayer-pr-review"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}
    )

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
        # Shared clients (one pool each) across sessions; tests inject fakes.
        self._lexora: LexoraChatClient = (
            lexora
            if lexora is not None
            else LexoraClient(lexora_url, timeout_seconds=timeout_seconds)
        )
        # T22: the naysayer authenticates as a SEPARATE GitHub identity
        # (spirrowgames-ops via MINDWIRE_NAYSAYER_GITHUB_TOKEN) so its
        # APPROVE / REQUEST_CHANGES is not "approving your own PR". An explicit
        # github_token arg wins (tests / overrides); otherwise resolve the
        # naysayer identity (which falls back to the shared token until the
        # distinct one is provisioned — see naysayer_github_token).
        self._github: GitHubReviewClient = (
            github
            if github is not None
            else GitHubClient(github_token if github_token is not None else naysayer_github_token())
        )
        self._sessions: dict[SessionHandle, _Session] = {}

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
        ctx: SpawnContext,
    ) -> SessionHandle:
        now = datetime.now(UTC)
        handle = SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=now,
        )
        self._sessions[handle] = _Session(
            ctx=ctx,
            own_role=role,
            state=SessionState.IDLE,
            last_active_at=now,
        )
        return handle

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        session = self._sessions.get(handle)
        if session is None:
            raise NaysayerPrReviewDeliveryError(f"unknown session {handle.session_id}")
        if session.state in _SHUTDOWN_STATES:
            raise NaysayerPrReviewDeliveryError(
                f"session {handle.session_id} is {session.state.value}; cannot deliver"
            )
        if event.event_type is not EventType.NEW_MESSAGE:
            return
        payload = event.payload
        if payload.author == handle.instance_id:
            # instance self-filter (Gap-2 (b), I3 v2.2): drop our own echoed post
            # (author == our instance_id, e.g. "naysayer-1"), not the bare role.
            return
        pr = parse_pr_ref(payload.body)
        if pr is None:
            return  # no PR ref → ordinary thread chatter, no-op
        if not _REVIEW_REQUEST_RE.search(payload.body):
            return  # a PR is mentioned but no review was requested → no-op
        if session.target_pr is not None and pr != session.target_pr:
            # Least authority: this review thread is bound to its first PR; a
            # different PR mentioned later must NOT trigger a review submission.
            return
        session.target_pr = pr

        session.state = SessionState.PROCESSING
        try:
            # Fail-closed: an unreachable GitHub raises here (ADR-07 §2.6).
            diff = await self._github.fetch_pr_diff(pr)
            truncated = len(diff) > _MAX_DIFF_CHARS
            completion = await self._lexora.chat_completion(
                model=self._model,
                messages=_build_messages(diff, pr.slug),
                max_tokens=self._max_tokens,
            )
            body = (completion.content or "").strip()
            if not body:
                raise NaysayerPrReviewDeliveryError(
                    f"naysayer returned empty review (finish_reason="
                    f"{completion.finish_reason!r}) for {pr.slug}; refusing to post/submit empty"
                )
            # Never APPROVE a review the model could not fully see (truncated diff
            # or hit the token cap) — force an objection.
            verdict = _resolve_verdict(
                body, truncated=truncated, finish_reason=completion.finish_reason
            )
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={
                        "adapter_id": self.adapter_id,
                        "model": completion.model or self._model,
                        "pr": pr.slug,
                        "verdict": verdict.value,
                        "truncated": truncated,
                        "finish_reason": completion.finish_reason,
                        "usage": completion.usage,
                    },
                )
            )
            # Fail-closed: an unreachable GitHub raises here too (after posting the
            # critique to the thread — the human still sees the review).
            await self._submit_review(pr, verdict, body)
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.delivery_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            if isinstance(exc, NaysayerPrReviewDeliveryError):
                raise
            raise NaysayerPrReviewDeliveryError(
                f"PR review failed for session {handle.session_id} ({pr.slug}): {exc}"
            ) from exc

        session.last_active_at = datetime.now(UTC)
        session.state = SessionState.IDLE

    async def _submit_review(self, pr: PrRef, verdict: ReviewEvent, body: str) -> None:
        """Submit the PR review, falling back to COMMENT on the same-identity 422.

        GitHub forbids a formal APPROVE / REQUEST_CHANGES on your *own* PR. T22
        provisions the naysayer a distinct identity
        (``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` = ``spirrowgames-ops``) so the
        formal verdict goes through. This COMMENT fallback remains a backstop for
        the window before that token is provisioned (the naysayer then shares the
        author identity and the verdict event 422s): we re-submit the same body as
        a COMMENT so the verdict (in the body) is still recorded, rather than
        fail-closed-halting on a credential-config issue.
        """
        try:
            await self._github.submit_review(pr, event=verdict, body=body)
        except GitHubHTTPError as exc:
            if exc.status_code == 422 and "own pull request" in str(exc).lower():
                await self._github.submit_review(pr, event=ReviewEvent.COMMENT, body=body)
            else:
                raise

    async def halt(
        self,
        handle: SessionHandle,
        *,
        grace: timedelta = timedelta(seconds=5),
    ) -> None:
        session = self._sessions.get(handle)
        if session is None or session.state in _SHUTDOWN_STATES:
            return
        # Stateless transport (shared clients closed via aclose, not here); halt
        # is a synchronous state transition. ``grace`` accepted for Port parity.
        session.state = SessionState.HALTED

    async def health(self, handle: SessionHandle) -> HealthStatus:
        session = self._sessions.get(handle)
        if session is None:
            raise NaysayerPrReviewHealthError(f"unknown session {handle.session_id}")
        details: dict[str, Any] = {"adapter_id": self.adapter_id, "session_id": handle.session_id}
        try:
            lexora_health = await self._lexora.health()
        except Exception as exc:
            details["lexora_health"] = f"unreachable: {exc}"
        else:
            details["lexora_health"] = lexora_health.get("status", lexora_health)
        return HealthStatus(
            state=session.state,
            last_active_at=session.last_active_at,
            error=session.error,
            details=details,
        )

    async def aclose(self) -> None:
        """Close the shared Lexora + GitHub clients (adapter teardown)."""
        await self._lexora.aclose()
        await self._github.aclose()


__all__ = [
    "NaysayerPrReviewAdapter",
    "NaysayerPrReviewDeliveryError",
    "NaysayerPrReviewHaltError",
    "NaysayerPrReviewHealthError",
    "NaysayerPrReviewSpawnError",
]
