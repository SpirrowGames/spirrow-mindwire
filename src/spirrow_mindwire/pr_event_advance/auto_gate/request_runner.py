"""The I/O half of the review-request trigger (T-gate-fires-on-review-request v1.1 §A / v1.2 §B).

Run by :func:`.runner.run_pass` before 1c's own loop (v1 §5: a person is waiting), sharing 1c's
daily counter and per-tick slots (D7). The per-PR order is v1.1 §A with v1.1 §B's swap (the
timeline is read before the project is resolved):

1. the timeline -> the newest request naming the review login (failure: notice, v1.2 §B);
2. the PR's own project (unresolved: wait an hour from the request, then notice);
3. M1 / M2 -> notice;
4. the receiving thread ``T-gate-request-<repo>-<n>``: a marker for this request -> noop;
   resolved -> notice ``receiving-thread-closed``;
5. M6: the day's cap -> notice ``cap``. A tick whose slots ran out waits silently: the next tick
   (minutes later) takes it, and a "daily cap" notice would be false (D6: a wait is not noticed);
6. M7: the thread's tail is someone else's turn -> wait;
7. fire: open the thread with the fire post as its ``propose_content``, or post into it. An
   "already exists" on open re-reads once and goes back to 4.

Chatroom producer surface (OBL-CHATROOM-PRODUCER-READER-SURFACE)
----------------------------------------------------------------

Intended reader of the fire post: the conductor, through its final ``NEXT: pr-review`` line.
A receiving thread found resolved is never written: step 4 answers it with the PR comment
``receiving-thread-closed``, which reaches the member who asked. A thread resolved between the
read and the write raises :class:`ThreadResolvedError`; it is caught and the disposition is
**(1)**: the refusal goes into this tick's JSON (the sweep's durable tick log), and the next
tick reads the thread again and notices it on the PR.

PR comments (D6) are the member-facing surface. One that cannot be written is recorded as
``notice-undeliverable`` in the tick JSON and summed into the digest line (v1.1 §B).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ...chatroom.status import is_terminal_status
from ...conductor.handoff import posted_at_of
from ...github.client import (
    GatePr,
    GateRequestReadError,
    GitHubHTTPError,
    IssueCommentInfo,
    PrRef,
    ReviewInfo,
    ReviewRequestEvent,
)
from ...magickit.client import MagickitMcpError, McpToolCaller, ThreadResolvedError
from ...repo_projects import INCOMPLETE, PrProject
from ...value_objects import Role
from ..decide import PR_EVENT_RELAY_AUTHOR, Post, thread_refs
from .decide import tail_decision
from .request import (
    REQUEST_ALREADY_FIRED,
    RequestNotice,
    latest_request,
    notice_already_posted,
    notice_window_start,
    project_wait_expired,
    receiving_thread_id,
    render_notice,
    request_already_fired,
    request_post,
    request_prefilter,
    requests_gate,
    timeline_failure_reason,
)

logger = logging.getLogger(__name__)

#: The thread tags a receiving thread is opened with. None of them is a sweep-exclude tag.
RECEIVING_TAGS: tuple[str, ...] = ("gate-request", "naysayer")
#: A request waiting for the next tick's slots, or behind someone else's turn (no notice).
WAIT_TICK_SLOTS = "tick-slots"
WAIT_QUEUED_BEHIND = "queued-behind"
WAIT_PROJECT = "pr-project-incomplete-waiting"


class RequestSource(Protocol):
    """The GitHub calls the review-request pass makes (:class:`~...github.client.GitHubClient`)."""

    async def fetch_review_request_events(self, pr: PrRef) -> list[ReviewRequestEvent]: ...

    async def list_issue_comments(self, pr: PrRef) -> list[IssueCommentInfo]: ...

    async def post_issue_comment(self, pr: PrRef, body: str) -> None: ...

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]: ...


class FireBudget(Protocol):
    """1c's counter and slots, shared (D7). Implemented by :class:`.runner._Budget`."""

    def daily_exhausted(self) -> bool: ...

    def tick_exhausted(self) -> bool: ...

    def spend(self, ref: str) -> None: ...


@dataclass
class RequestPassResult:
    """What the pass did, merged into the tick's :class:`.runner.PassReport` by the caller."""

    #: Every PR carrying the request signal: 1c's loop skips them as ``review-requested``.
    signalled: set[str] = field(default_factory=set)
    examined: int = 0
    posted: list[dict[str, Any]] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    noops: dict[str, list[str]] = field(default_factory=dict)
    waits: dict[str, list[str]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    #: PR refs whose notice could not be written (v1.1 §B): the digest line counts them.
    undeliverable: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RequestTarget:
    """One listed PR with its repo's project resolution."""

    pr: GatePr
    pr_project: PrProject


@dataclass
class _Ctx:
    mcp: McpToolCaller
    gh: RequestSource
    budget: FireBudget
    review_login: str
    now: datetime
    live: bool
    roster: Mapping[str, Role] | None
    result: RequestPassResult


async def run_request_pass(
    *,
    mcp: McpToolCaller,
    gh: RequestSource,
    targets: Sequence[RequestTarget],
    budget: FireBudget,
    review_login: str,
    now: datetime,
    live: bool,
    roster: Mapping[str, Role] | None,
) -> RequestPassResult:
    """Judge (and in live mode act on) every PR whose reviewers name ``review_login``."""
    result = RequestPassResult()
    ctx = _Ctx(mcp, gh, budget, review_login, now, live, roster, result)
    for target in targets:
        if not requests_gate(target.pr, review_login):
            continue
        result.signalled.add(target.pr.ref.slug)
        result.examined += 1
        try:
            await _handle(ctx, target)
        except Exception as exc:  # isolation boundary: one PR's fault costs that PR only
            logger.warning("gate-request: %s failed: %r", target.pr.ref.slug, exc)
            _skip(ctx, target.pr, f"error: {exc!r}")
    return result


def _decision(ctx: _Ctx, pr: GatePr, result: str, reason: str | None = None) -> None:
    if not ctx.live:
        row = {"pr_ref": pr.ref.slug, "trigger": "review-request", "result": result}
        if reason is not None:
            row["reason"] = reason
        ctx.result.decisions.append(row)


def _skip(ctx: _Ctx, pr: GatePr, why: str) -> None:
    ctx.result.failures.append({"pr_ref": pr.ref.slug, "trigger": "review-request", "reason": why})
    _decision(ctx, pr, "skipped", why)


def _wait(ctx: _Ctx, pr: GatePr, why: str) -> None:
    ctx.result.waits.setdefault(why, []).append(pr.ref.slug)
    _decision(ctx, pr, "wait", why)


async def _handle(ctx: _Ctx, target: RequestTarget) -> None:
    pr = target.pr
    try:
        events = await ctx.gh.fetch_review_request_events(pr.ref)
    except GateRequestReadError as exc:
        reason = timeline_failure_reason(transient=exc.transient, code=exc.code)
        await _notice(ctx, pr, None, reason, detail=str(exc))
        return
    event = latest_request(events, ctx.review_login)
    if event is None:
        await _notice(ctx, pr, None, RequestNotice.REQUEST_EVENT_MISSING.value)
        return
    project = target.pr_project.project
    if project is None:
        if target.pr_project.reason == INCOMPLETE:
            if not project_wait_expired(event, ctx.now):
                _wait(ctx, pr, WAIT_PROJECT)
                return
            reason = RequestNotice.PR_PROJECT_INCOMPLETE.value
        else:
            reason = RequestNotice.PR_PROJECT_AMBIGUOUS.value
        await _notice(ctx, pr, event.event_id, reason, detail=target.pr_project.describe())
        return
    refused = request_prefilter(pr, event, ctx.review_login)
    if refused is not None:
        await _notice(ctx, pr, event.event_id, refused.value)
        return
    thread_id = receiving_thread_id(pr.ref)
    for attempt in range(2):
        outcome = await _judge_and_fire(ctx, pr, event, project, thread_id)
        if outcome != "exists" or attempt == 1:
            if outcome == "exists":
                _skip(ctx, pr, "thread-open-raced: still not readable as this PR's")
            return


async def _read_thread(
    ctx: _Ctx, project: str, thread_id: str
) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    """``(messages, status, None)``; ``(None, None, None)`` when absent; ``(.., .., why)``."""
    try:
        result = await ctx.mcp.call_tool(
            "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
        )
    except MagickitMcpError as exc:
        if "not found" in str(exc).lower():
            return None, None, None
        return None, None, f"thread-unreadable: {exc}"
    raw = result.get("messages") if isinstance(result, dict) else None
    messages = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    meta = result.get("thread") if isinstance(result, dict) else None
    status = meta.get("status") if isinstance(meta, dict) else None
    return messages, status if isinstance(status, str) else None, None


async def _judge_and_fire(
    ctx: _Ctx, pr: GatePr, event: ReviewRequestEvent, project: str, thread_id: str
) -> str:
    """Steps 4-7 once. ``"exists"`` asks the caller to re-read after an open race."""
    messages, status, why = await _read_thread(ctx, project, thread_id)
    if why is not None:
        _skip(ctx, pr, why)
        return "done"
    repo = f"{pr.ref.owner}/{pr.ref.repo}"
    number = pr.ref.number
    if messages is not None and request_already_fired(
        messages, repo=repo, number=number, event=event.event_id, trust_opener=True
    ):
        ctx.result.noops.setdefault(REQUEST_ALREADY_FIRED, []).append(pr.ref.slug)
        _decision(ctx, pr, "noop", REQUEST_ALREADY_FIRED)
        return "done"
    if messages is not None and status is not None and is_terminal_status(status):
        await _notice(ctx, pr, event.event_id, RequestNotice.RECEIVING_THREAD_CLOSED.value)
        return "done"
    if ctx.budget.daily_exhausted():
        await _notice(ctx, pr, event.event_id, RequestNotice.CAP.value)
        return "done"
    if ctx.live and ctx.budget.tick_exhausted():
        _wait(ctx, pr, WAIT_TICK_SLOTS)
        return "done"
    if messages:
        tail = messages[-1]
        raw_np = tail.get("next_participant")
        decided = tail_decision(
            str(tail.get("content", "")),
            repo=repo,
            number=number,
            roster=ctx.roster,
            next_participant=raw_np if isinstance(raw_np, str) and raw_np else None,
            posted_at=posted_at_of(tail),
        )
        if not isinstance(decided, Post):
            _wait(ctx, pr, WAIT_QUEUED_BEHIND)
            return "done"
    refs = tuple(
        f"{r.project}/{r.thread_id}" if r.project else r.thread_id
        for r in thread_refs(pr.body).thread_lines
    )
    post = request_post(repo=repo, number=number, event=event, thread_refs=refs)
    if not ctx.live:
        _decision(ctx, pr, "would-fire", thread_id)
        return "done"
    if messages is None:
        opened = await _open(ctx, pr, project, thread_id, post)
        if opened != "ok":
            return opened
        msg_id = ""
    else:
        msg_id_or_none, failure = await _post(ctx, project, thread_id, post)
        if failure is not None:
            _skip(ctx, pr, failure)
            return "done"
        msg_id = msg_id_or_none or ""
    ctx.budget.spend(pr.ref.slug)
    ctx.result.posted.append(
        {
            "pr_ref": pr.ref.slug,
            "trigger": "review-request",
            "thread_id": thread_id,
            "event": event.event_id,
            "msg_id": msg_id,
        }
    )
    return "done"


async def _open(ctx: _Ctx, pr: GatePr, project: str, thread_id: str, post: Post) -> str:
    try:
        await ctx.mcp.call_tool(
            "chatroom_open_thread",
            {
                "project": project,
                "thread_id": thread_id,
                "title": f"gate request: {pr.ref.slug}",
                "owner": PR_EVENT_RELAY_AUTHOR,
                "propose_content": post.render(),
                "tags": list(RECEIVING_TAGS),
            },
        )
    except MagickitMcpError as exc:
        if "already exists" in str(exc).lower():
            return "exists"  # opened by someone else meanwhile: re-read (v1.1 §A step 7)
        _skip(ctx, pr, f"open-failed: {exc}")
        return "done"
    return "ok"


async def _post(
    ctx: _Ctx, project: str, thread_id: str, post: Post
) -> tuple[str | None, str | None]:
    try:
        result = await ctx.mcp.call_tool(
            "chatroom_post_message",
            {
                "project": project,
                "thread_id": thread_id,
                "msg_type": "report",
                "author": PR_EVENT_RELAY_AUTHOR,
                "content": post.render(),
            },
        )
    except ThreadResolvedError as exc:  # disposition (1), see the module docstring
        logger.warning("gate-request post dropped (thread %r resolved): %s", thread_id, exc)
        return None, "thread-resolved"
    except MagickitMcpError as exc:
        return None, f"post-failed: {exc}"
    msg = result.get("msg") if isinstance(result, dict) else None
    return (str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""), None


async def _notice(
    ctx: _Ctx, pr: GatePr, event: int | None, reason: str, *, detail: str | None = None
) -> None:
    """Write the D6 notice once per ``(event, reason)`` in the window (v1.2 §B)."""
    row: dict[str, Any] = {"pr_ref": pr.ref.slug, "event": event, "reason": reason}
    if detail:
        row["detail"] = detail
    if not ctx.live:
        _decision(ctx, pr, "would-notice", reason)
        return
    try:
        since = notice_window_start(await ctx.gh.fetch_pr_reviews_strict(pr.ref), ctx.review_login)
    except Exception:  # the review list unread -> the whole PR (keeps it to one notice)
        since = None
    try:
        comments = await ctx.gh.list_issue_comments(pr.ref)
    except GateRequestReadError as exc:
        # Without the comments there is no de-duplication, and a notice posted blind would
        # repeat every tick. Recorded as undeliverable instead, so it still surfaces.
        _undeliverable(ctx, pr, row, f"comments-unreadable: {exc.code}")
        return
    if notice_already_posted(
        comments, event=event, reason=reason, review_login=ctx.review_login, since=since
    ):
        ctx.result.noops.setdefault("notice-already-posted", []).append(pr.ref.slug)
        return
    try:
        await ctx.gh.post_issue_comment(pr.ref, render_notice(event=event, reason=reason))
    except GitHubHTTPError as exc:
        _undeliverable(ctx, pr, row, str(exc.status_code or "transport"))
        return
    ctx.result.notices.append(row)


def _undeliverable(ctx: _Ctx, pr: GatePr, row: dict[str, Any], status: str) -> None:
    ctx.result.failures.append({**row, "trigger": "review-request", "failure": "notice"})
    ctx.result.failures[-1]["reason"] = f"notice-undeliverable: {status} ({row['reason']})"
    ctx.result.undeliverable.append(pr.ref.slug)


def undeliverable_digest_line(refs: Collection[str]) -> str | None:
    """The digest line for notices that could not be written (v1.1 §B), or ``None``."""
    if not refs:
        return None
    unique = sorted(set(refs))
    return f"gate-request: PR に通知できなかった依頼 {len(unique)} 件: {', '.join(unique)}"


__all__ = [
    "RECEIVING_TAGS",
    "WAIT_QUEUED_BEHIND",
    "WAIT_TICK_SLOTS",
    "FireBudget",
    "RequestPassResult",
    "RequestSource",
    "RequestTarget",
    "run_request_pass",
    "undeliverable_digest_line",
]
