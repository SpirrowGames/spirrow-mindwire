"""The thin I/O half of the park-wake tick: read every open thread, classify, wake, report.

One pass per project per sweep tick (``python -m spirrow_mindwire.park_wake``, called by
``deploy/run-conductor-scheduled.ps1`` next to the 1b tick). Two jobs on the same read:

1. **Classify every ``NEXT: none`` head** (magickit msg-1015 v11 §1, moved to mindwire from
   magickit): ``done`` / ``blocked_on`` from a valid ``STOP:`` line; otherwise ``unclassified``
   for an agent and ``human_close`` for an author whose registered roles include ``human``. Every
   open thread of the project is read — not only the sweep's candidates — so a thread nobody
   drives through the conductor (msg-2014 §2 (4), the magickit n=1 threads) is classified too.
2. **Wake fired parks** (D-7): :func:`~.decide.select_wakes` over the facts read this tick, then
   one post per woken park.

Stateless, like 1b (:mod:`~spirrow_mindwire.pr_event_advance.runner`): every fact is read fresh
and nothing is remembered between ticks. Fail closed: a fact that could not be read writes
nothing (the park is held as ``unknown`` and reported); an author whose role could not be read is
reported and left out of the counts rather than guessed into one.

Chatroom producer surface (OBL-CHATROOM-PRODUCER-READER-SURFACE)
----------------------------------------------------------------

Intended reader of a wake post: the persona its ``NEXT:`` names, through the conductor, which
the sweep launches on the moved head. When the thread was resolved between the read and the
write, :class:`ThreadResolvedError` is caught and the disposition is **(1)**: the refusal is in
this tick's JSON, which the sweep wrapper writes to its durable tick log. Nothing is re-posted
elsewhere — a resolved thread has nobody left to wake.

This module never calls ``chatroom_close_thread``: ``STOP: done`` is recorded, not acted on. The
close stays the owner's act.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, assert_never

from ..conductor.disposition import StopClass, classify_stop, stop_line_of
from ..conductor.handoff import HandoffKind, StopStatus, resolve_handoff
from ..github.client import PrRef, PrResolution, PrState, parse_pr_ref
from ..magickit.client import MagickitMcpError, McpToolCaller, ThreadResolvedError
from ..pr_event_advance.runner import list_open_threads
from ..value_objects import Role
from .decide import (
    DEPLOY_TERMINAL_STATUSES,
    PARK_WAKE_RELAY_AUTHOR,
    Fact,
    Park,
    QueueTarget,
    ThreadTarget,
    Trigger,
    TriggerArm,
    park_of,
    render_wake,
    select_wakes,
)

logger = logging.getLogger(__name__)

#: Chatroom tools this module calls. A test pins that no close tool is among them.
TOOLS_USED: frozenset[str] = frozenset(
    {
        "chatroom_list_threads",
        "chatroom_get_thread",
        "chatroom_post_message",
        "deploy_status",
        "get_identity",
    }
)

#: The registered role that makes an author's bare ``NEXT: none`` a ``human_close`` (v11 §1).
HUMAN_ROLE = "human"


class PrStateSource(Protocol):
    """The one GitHub read the ``pr:`` arm needs (satisfied by ``GitHubClient``)."""

    async def fetch_pr_state(self, pr: PrRef) -> PrState: ...


@dataclass(frozen=True)
class Head:
    """The tail message of one open thread, as read this tick."""

    thread_id: str
    msg_id: str
    author: str
    content: str
    next_participant: str | None


@dataclass
class TickReport:
    project: str
    threads: int = 0
    classes: dict[str, list[str]] = field(default_factory=dict)
    woken: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def classify(self, cls: StopClass, thread_id: str) -> None:
        self.classes.setdefault(cls.value, []).append(thread_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "threads": self.threads,
            "counts": {c.value: len(self.classes.get(c.value, [])) for c in StopClass},
            "unclassified": self.classes.get(StopClass.UNCLASSIFIED.value, []),
            "woken": self.woken,
            "held": self.held,
            "errors": self.errors,
        }


async def read_heads(mcp: McpToolCaller, project: str) -> tuple[list[Head], list[dict[str, str]]]:
    """The tail of every open thread of ``project``, and a row per thread that could not be read.

    The listing itself failing raises (:class:`MagickitMcpError`): with no thread list there is
    nothing to classify and nothing safe to wake, so the tick fails as a whole.
    """
    heads: list[Head] = []
    errors: list[dict[str, str]] = []
    for thread_id in await list_open_threads(mcp, project):
        try:
            result = await mcp.call_tool(
                "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
            )
        except MagickitMcpError as exc:
            errors.append({"thread_id": thread_id, "reason": f"thread-unreadable: {exc}"})
            continue
        messages = result.get("messages") if isinstance(result, dict) else None
        rows = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
        if not rows:
            errors.append({"thread_id": thread_id, "reason": "thread-unreadable: no messages"})
            continue
        tail = rows[-1]
        field_value = tail.get("next_participant")
        heads.append(
            Head(
                thread_id=thread_id,
                msg_id=str(tail.get("msg_id", "")),
                author=str(tail.get("author", "")),
                content=str(tail.get("content", "")),
                next_participant=field_value if isinstance(field_value, str) else None,
            )
        )
    return heads, errors


class RoleLookup:
    """``Is this author registered with the human role?`` — one registry read per author per tick.

    ``True`` / ``False`` are answers (``found`` with or without the role; ``not_found`` is an
    unregistered author, who is not the human). ``None`` is "could not tell" (lookup failed,
    contract violation, transport error) and is never guessed into either answer.
    """

    def __init__(self, mcp: McpToolCaller) -> None:
        self._mcp = mcp
        self._cache: dict[str, bool | None] = {}

    async def is_human(self, author: str) -> bool | None:
        if author not in self._cache:
            self._cache[author] = await self._lookup(author)
        return self._cache[author]

    async def _lookup(self, author: str) -> bool | None:
        try:
            result = await self._mcp.call_tool("get_identity", {"identity_name": author})
        except MagickitMcpError:
            return None
        status = result.get("status") if isinstance(result, dict) else None
        if status == "not_found":
            return False
        if status != "found":
            return None
        identity = result.get("identity")
        roles = identity.get("allowed_roles") if isinstance(identity, dict) else None
        if not isinstance(roles, list):
            return None
        return HUMAN_ROLE in roles


def live_roles(heads: list[Head], roster: Mapping[str, Role]) -> list[Role | None] | None:
    """The role each open thread hands to, for ``queue-empty``; ``None`` entries hand to nobody.

    A thread counts as live for a role when its head resolves (as the conductor resolves it) to a
    participant holding that role.
    """
    out: list[Role | None] = []
    for head in heads:
        handoff = resolve_handoff(head.content, roster, next_participant=head.next_participant)
        out.append(handoff.role if handoff.kind is HandoffKind.ROLE else None)
    return out


class FactReader:
    """Reads each trigger key's fact once per tick (the tick's committed state, D-7)."""

    def __init__(
        self,
        *,
        mcp: McpToolCaller,
        gh: PrStateSource,
        roster: Mapping[str, Role],
        project: str,
        own_heads: list[Head],
        own_heads_complete: bool,
    ) -> None:
        self._mcp = mcp
        self._gh = gh
        self._roster = roster
        self._queues: dict[str, list[Role | None] | None] = {
            project: live_roles(own_heads, roster) if own_heads_complete else None
        }

    async def fact(self, trigger: Trigger) -> tuple[Fact, str]:
        """The fact for ``trigger`` and a short reason (what was read)."""
        match trigger.arm:
            case TriggerArm.THREAD:
                assert isinstance(trigger.target, ThreadTarget)
                return await self._thread(trigger.target)
            case TriggerArm.PR:
                assert isinstance(trigger.target, str)
                return await self._pr(trigger.target)
            case TriggerArm.DEPLOY:
                assert isinstance(trigger.target, str)
                return await self._deploy(trigger.target)
            case TriggerArm.QUEUE_EMPTY:
                assert isinstance(trigger.target, QueueTarget)
                return await self._queue(trigger.target)
            case _:
                assert_never(trigger.arm)

    async def _thread(self, target: ThreadTarget) -> tuple[Fact, str]:
        try:
            result = await self._mcp.call_tool(
                "chatroom_get_thread",
                {"project": target.project, "thread_id": target.thread_id, "mode": "summary"},
            )
        except MagickitMcpError as exc:
            return Fact.UNKNOWN, f"thread-unreadable: {exc}"
        thread = result.get("thread") if isinstance(result, dict) else None
        status = thread.get("status") if isinstance(thread, dict) else None
        if not isinstance(status, str) or not status:
            return Fact.UNKNOWN, "thread-status-missing"
        return (Fact.FIRED if status == "resolved" else Fact.NOT_FIRED), f"status={status}"

    async def _pr(self, ref: str) -> tuple[Fact, str]:
        pr = parse_pr_ref(ref)
        if pr is None:  # parse_trigger already validated it; defensive only
            return Fact.UNKNOWN, "pr-ref-unreadable"
        try:
            state = await self._gh.fetch_pr_state(pr)
        except Exception as exc:  # isolation boundary, as in 1b: costs this key only
            logger.warning("park-wake: fetch_pr_state(%s) raised: %r", ref, exc)
            return Fact.UNKNOWN, f"gh-error: {exc!r}"
        match state.resolution:
            case PrResolution.CLOSED:
                return Fact.FIRED, "merged" if state.merged else "closed"
            case PrResolution.OPEN:
                return Fact.NOT_FIRED, "open"
            case PrResolution.NOT_FOUND:
                return Fact.UNKNOWN, "pr-not-found"
            case PrResolution.UNRESOLVABLE:
                return Fact.UNKNOWN, "gh-unresolvable"
            case _:
                assert_never(state.resolution)

    async def _deploy(self, request_id: str) -> tuple[Fact, str]:
        try:
            result = await self._mcp.call_tool("deploy_status", {"request_id": request_id})
        except MagickitMcpError as exc:
            return Fact.UNKNOWN, f"deploy-unreadable: {exc}"
        request = result.get("request") if isinstance(result, dict) else None
        status = request.get("status") if isinstance(request, dict) else None
        if not isinstance(status, str) or not status:
            return Fact.UNKNOWN, "deploy-status-missing"
        fired = status in DEPLOY_TERMINAL_STATUSES
        return (Fact.FIRED if fired else Fact.NOT_FIRED), f"status={status}"

    async def _queue(self, target: QueueTarget) -> tuple[Fact, str]:
        if target.project not in self._queues:
            try:
                heads, errors = await read_heads(self._mcp, target.project)
            except MagickitMcpError:
                heads, errors = [], [{"thread_id": "__listing__", "reason": "unreadable"}]
            self._queues[target.project] = live_roles(heads, self._roster) if not errors else None
        roles = self._queues[target.project]
        if roles is None:
            return Fact.UNKNOWN, "queue-unreadable"
        live = [r for r in roles if r is not None and (target.role is None or r is target.role)]
        if live:
            return Fact.NOT_FIRED, f"live={len(live)}"
        return Fact.FIRED, "live=0"


async def run_tick(
    *,
    mcp: McpToolCaller,
    gh: PrStateSource,
    project: str,
    roster: Mapping[str, Role],
    registered: frozenset[str] | None = None,
) -> TickReport:
    """One pass over every open thread of ``project``: classify, read facts, wake.

    ``registered`` is the sweep's thread list for this project, or ``None`` when unknown. A wake
    into a thread outside it is still posted (the trigger fired for that thread), but flagged
    ``unregistered``: no conductor acts on it until the thread is added to the sweep (as 1b).
    """
    report = TickReport(project=project)
    heads, read_errors = await read_heads(mcp, project)
    report.threads = len(heads) + len(read_errors)
    report.errors.extend(read_errors)
    roles = RoleLookup(mcp)
    parks: list[Park] = []
    for head in heads:
        stop = stop_line_of(head.content, roster, next_participant=head.next_participant)
        if stop is None:
            continue
        # The author matters only when there is no valid line (classify_stop), so only then is
        # the registry asked.
        human: bool | None = False
        if stop.status not in (StopStatus.DONE, StopStatus.BLOCKED_ON):
            human = await roles.is_human(head.author)
            if human is None:
                reason = f"author-role-unreadable: {head.author}"
                report.errors.append({"thread_id": head.thread_id, "reason": reason})
                continue
        report.classify(classify_stop(stop, author_is_human=bool(human)), head.thread_id)
        if stop.status is StopStatus.BLOCKED_ON:
            park = park_of(stop, thread_id=head.thread_id, head_msg_id=head.msg_id, project=project)
            if park is None:
                report.errors.append(
                    {"thread_id": head.thread_id, "reason": f"trigger-unreadable: {stop.raw}"}
                )
            else:
                parks.append(park)

    reader = FactReader(
        mcp=mcp,
        gh=gh,
        roster=roster,
        project=project,
        own_heads=heads,
        own_heads_complete=not read_errors,
    )
    facts: dict[str, Fact] = {}
    reasons: dict[str, str] = {}
    for park in parks:
        key = park.trigger.key
        if key not in facts:
            facts[key], reasons[key] = await reader.fact(park.trigger)

    selection = select_wakes(parks, facts)
    for park, why in selection.held:
        report.held.append(
            {
                "thread_id": park.thread_id,
                "trigger": park.trigger.key,
                "reason": why,
                "fact": reasons.get(park.trigger.key, ""),
            }
        )
    for park in selection.wake:
        outcome = await _post_wake(mcp, project, park)
        outcome["fact"] = reasons.get(park.trigger.key, "")
        if outcome.get("action") == "posted" and registered is not None:
            outcome["unregistered"] = park.thread_id not in registered
        report.woken.append(outcome)
    return report


async def _post_wake(mcp: McpToolCaller, project: str, park: Park) -> dict[str, Any]:
    base: dict[str, Any] = {
        "thread_id": park.thread_id,
        "trigger": park.trigger.key,
        "wake": park.wake,
        "parked_msg": park.head_msg_id,
    }
    try:
        result = await mcp.call_tool(
            "chatroom_post_message",
            {
                "project": project,
                "thread_id": park.thread_id,
                "msg_type": "report",
                "author": PARK_WAKE_RELAY_AUTHOR,
                "content": render_wake(park),
                # No ``role``: a machine restating a trigger fact holds none (I-6).
            },
        )
    except ThreadResolvedError as exc:
        # Disposition (1) — the tick's JSON / wrapper log. See module docstring.
        logger.warning("park wake dropped (thread %r resolved): %s", park.thread_id, exc)
        return {**base, "action": "skipped", "reason": "thread-resolved"}
    except MagickitMcpError as exc:
        return {**base, "action": "skipped", "reason": f"post-failed: {exc}"}
    msg = result.get("msg") if isinstance(result, dict) else None
    msg_id = str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""
    return {**base, "action": "posted", "msg_id": msg_id}


__all__ = [
    "HUMAN_ROLE",
    "TOOLS_USED",
    "FactReader",
    "Head",
    "PrStateSource",
    "RoleLookup",
    "TickReport",
    "live_roles",
    "read_heads",
    "run_tick",
]
