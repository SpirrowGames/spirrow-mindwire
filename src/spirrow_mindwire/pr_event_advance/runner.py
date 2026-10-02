"""The thin I/O half of 1b: enumerate threads, read tails and PRs, write what :func:`decide` says.

Stateless (gate_admission §A-2): every fact is read fresh from the chatroom and GitHub on each
tick and nothing is remembered between ticks. "Act once" comes from the thread itself — after a
post, the tail is :data:`~.decide.PR_EVENT_RELAY_AUTHOR`'s message, which :func:`decide` ignores.

Fail-closed rule (R4): when a fact could not be read — the thread, the PR state, the CI state —
nothing is written and the reason is reported. Writing on a guess is the one thing this must not
do; the next tick reads again. The one write that proceeds on a missing input is the roster: the
PR event is a fact already read, so it is written and the hand-off goes to ``NEXT: operator``.

Chatroom producer surface (OBL-CHATROOM-PRODUCER-READER-SURFACE)
----------------------------------------------------------------

Intended reader of every post: whoever the post's ``NEXT:`` names in that work thread — the
proposer (via the conductor), the PR-gate (``NEXT: pr-review``), or the operator. When the
thread is resolved by the time we write, :class:`ThreadResolvedError` is caught and the
disposition is **(1)**: the refusal is recorded in this tick's JSON output, which the sweep
wrapper writes to its durable tick log. Nothing is re-posted elsewhere: a resolved thread has
no work left to advance, so the record is for the operator reading the log, not a hand-off.

This module never calls (and does not import) ``chatroom_close_thread`` (R10): ``NEXT: none``
is a handoff token, and a status close is the human's / a closeable role's act.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, assert_never

from ..chatroom.status import FINISHED_STATUSES
from ..github.client import CiState, CiStatus, PrRef, PrResolution, PrState, parse_pr_ref
from ..magickit.client import MagickitMcpError, McpToolCaller, ThreadResolvedError
from .decide import (
    PR_EVENT_RELAY_AUTHOR,
    Noop,
    Post,
    PrFacts,
    PrLifecycle,
    decide,
    needs_ci,
    precheck,
    read_relay_tail,
)

logger = logging.getLogger(__name__)

#: Thread statuses 1b looks at: every status that is not finished. Positive enumeration, derived
#: from the shared finished set so a new finished status is excluded here automatically.
OPEN_STATUSES: tuple[str, ...] = tuple(
    s for s in ("active", "awaiting_reply", "parked") if s not in FINISHED_STATUSES
)

#: Chatroom tools this module calls. The R10 test pins that no close tool is among them.
TOOLS_USED: frozenset[str] = frozenset(
    {"chatroom_list_threads", "chatroom_get_thread", "chatroom_post_message"}
)


class PrSource(Protocol):
    """The two GitHub reads 1b needs (satisfied by :class:`~..github.client.GitHubClient`)."""

    async def fetch_pr_state(self, pr: PrRef) -> PrState: ...

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus: ...


@dataclass
class ThreadOutcome:
    """One thread's result, as printed in the CLI's JSON."""

    thread_id: str
    action: str  # "posted" / "noop" / "skipped"
    reason: str
    pr_ref: str | None = None
    next_line: str | None = None
    msg_id: str | None = None
    unregistered: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class TickReport:
    project: str
    proposer: str | None
    outcomes: list[ThreadOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        posted = [o for o in self.outcomes if o.action == "posted"]
        return {
            "project": self.project,
            "proposer": self.proposer,
            "threads": len(self.outcomes),
            "posted": len(posted),
            "outcomes": [o.as_dict() for o in self.outcomes if o.action != "noop"],
            "noop_counts": _noop_counts(self.outcomes),
        }


def _noop_counts(outcomes: Iterable[ThreadOutcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in outcomes:
        if o.action == "noop":
            counts[o.reason] = counts.get(o.reason, 0) + 1
    return counts


def pr_facts_of(state: PrState) -> PrFacts | None:
    """Map a GitHub read onto :class:`PrFacts`; ``None`` when GitHub did not answer."""
    match state.resolution:
        case PrResolution.UNRESOLVABLE:
            return None
        case PrResolution.OPEN:
            lifecycle = PrLifecycle.OPEN.value
        case PrResolution.CLOSED:
            lifecycle = PrLifecycle.MERGED.value if state.merged else PrLifecycle.CLOSED.value
        case PrResolution.NOT_FOUND:
            lifecycle = "not-found"  # a definite answer, but not one of §F.1.1's three → row 9
        case _:
            assert_never(state.resolution)
    return PrFacts(
        state=lifecycle,
        head_sha=state.head_sha,
        merge_commit_sha=state.merge_commit_sha,
        closed_at=state.closed_at,
        body=state.body or "",
    )


async def list_open_threads(mcp: McpToolCaller, project: str, limit: int = 1000) -> list[str]:
    """Every not-finished thread of ``project`` (one listing call, as ``thread_heads`` does)."""
    payload: Any = await mcp.call_tool(
        "chatroom_list_threads",
        {"project": project, "status_filter": list(OPEN_STATUSES), "limit": limit},
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise MagickitMcpError(f"chatroom_list_threads returned no item list: {payload!r}")
    return [
        item["thread_id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("thread_id"), str)
    ]


async def _read_tail(mcp: McpToolCaller, project: str, thread_id: str) -> dict[str, Any] | None:
    result = await mcp.call_tool(
        "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
    )
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return None
    rows = [m for m in messages if isinstance(m, dict)]
    return rows[-1] if rows else None


async def advance_thread(
    *,
    mcp: McpToolCaller,
    gh: PrSource,
    project: str,
    thread_id: str,
    proposer: str | None,
) -> ThreadOutcome:
    """Read one thread's tail and its PR, decide, and write at most one message."""
    try:
        tail_msg = await _read_tail(mcp, project, thread_id)
    except MagickitMcpError as exc:
        return ThreadOutcome(thread_id, "skipped", f"thread-unreadable: {exc}")
    if tail_msg is None:
        return ThreadOutcome(thread_id, "skipped", "thread-unreadable: no messages")
    tail = read_relay_tail(str(tail_msg.get("author", "")), str(tail_msg.get("content", "")))
    early = precheck(tail)
    if early is not None:
        return ThreadOutcome(thread_id, "noop", early.reason.value)
    assert tail is not None
    pr = parse_pr_ref(tail.pr_ref)
    if pr is None:  # read_relay_tail already validated the ref; defensive only
        return ThreadOutcome(thread_id, "noop", "not-relay-tail")
    facts = pr_facts_of(await gh.fetch_pr_state(pr))
    if facts is None:
        return ThreadOutcome(thread_id, "skipped", "gh-unresolvable", pr_ref=tail.pr_ref)
    ci: CiState | None = None
    if needs_ci(tail, facts):
        status = await gh.fetch_ci_status(pr)
        ci = status.state
        # The CI read is for the head GitHub reports now; if that moved between the two reads,
        # this tick's CI answer is about a different diff — wait for the next tick.
        if status.head_sha and facts.head_sha and status.head_sha.lower() != facts.head_sha.lower():
            ci = CiState.PENDING
    decision = decide(thread_id=thread_id, tail=tail, pr=facts, ci=ci, proposer=proposer)
    match decision:
        case Noop(reason=reason):
            return ThreadOutcome(thread_id, "noop", reason.value, pr_ref=tail.pr_ref)
        case Post():
            return await _post(mcp, project, thread_id, tail.pr_ref, decision)
        case _:
            assert_never(decision)


async def _post(
    mcp: McpToolCaller, project: str, thread_id: str, pr_ref: str, decision: Post
) -> ThreadOutcome:
    next_line = decision.next_line.splitlines()[-1]
    try:
        result = await mcp.call_tool(
            "chatroom_post_message",
            {
                "project": project,
                "thread_id": thread_id,
                "msg_type": "report",
                "author": PR_EVENT_RELAY_AUTHOR,
                "content": decision.render(),
                # No ``role``: a machine restating a GitHub fact holds none (I-6, R3).
            },
        )
    except ThreadResolvedError as exc:
        # Disposition (1) — the tick's JSON / wrapper log. See module docstring.
        logger.warning("pr-event post dropped (thread %r resolved): %s", thread_id, exc)
        return ThreadOutcome(
            thread_id, "skipped", "thread-resolved", pr_ref=pr_ref, next_line=next_line
        )
    except MagickitMcpError as exc:
        return ThreadOutcome(
            thread_id, "skipped", f"post-failed: {exc}", pr_ref=pr_ref, next_line=next_line
        )
    msg = result.get("msg") if isinstance(result, dict) else None
    msg_id = str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""
    return ThreadOutcome(
        thread_id, "posted", "posted", pr_ref=pr_ref, next_line=next_line, msg_id=msg_id
    )


async def run_tick(
    *,
    mcp: McpToolCaller,
    gh: PrSource,
    project: str,
    proposer: str | None,
    registered: frozenset[str] | None = None,
) -> TickReport:
    """One pass over every not-finished thread of ``project``.

    ``registered`` is the sweep's thread list for this project, or ``None`` when unknown. A post
    into a thread outside it is still made (the fact belongs in the thread), but the outcome is
    flagged ``unregistered`` because no conductor will act on its ``NEXT:`` until the thread is
    added to the sweep (§F.1 item 3).
    """
    report = TickReport(project=project, proposer=proposer)
    for thread_id in await list_open_threads(mcp, project):
        outcome = await advance_thread(
            mcp=mcp, gh=gh, project=project, thread_id=thread_id, proposer=proposer
        )
        if outcome.action == "posted" and registered is not None:
            outcome.unregistered = thread_id not in registered
        report.outcomes.append(outcome)
    return report


__all__ = [
    "OPEN_STATUSES",
    "TOOLS_USED",
    "PrSource",
    "ThreadOutcome",
    "TickReport",
    "advance_thread",
    "list_open_threads",
    "pr_facts_of",
    "run_tick",
]
