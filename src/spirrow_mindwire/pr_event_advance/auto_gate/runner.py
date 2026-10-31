"""The I/O half of 1c: one pass over the open PRs of the given repos (T-gate-fires-on-pr-open).

GitHub is the queue: each tick re-lists what is open, so a PR not fired today simply appears again
tomorrow and nothing waits in a local store (msg-7619). The only state is the daily counter.

The pass (msg-7735):

* ``fired >= cap`` in live mode: no listing at all, one log line, done.
* The pass may fire ``min(per_tick, cap - fired)`` PRs. Only a successful post uses a slot, so a
  permanent Noop never starves the PRs behind it.
* Dry-run judges every PR, posts nothing and never writes the counter.

A counter write that fails costs no double fire, because the post itself is the fire record
(msg-7737); the day's count merely runs low. A counter that cannot be READ stops a live pass: it is
the cost ceiling Takahito accepted (msg-7501), so it fails closed.

Chatroom producer surface (OBL-CHATROOM-PRODUCER-READER-SURFACE)
----------------------------------------------------------------

Intended reader of every post: the conductor, through the final ``NEXT: pr-review`` line. When
the thread is resolved at write time, :class:`ThreadResolvedError` is caught and the disposition
is **(1)**: the refusal goes into this tick's JSON, which the sweep wrapper writes to its durable
tick log, and the PR is judged again next tick.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ...chatroom.status import is_terminal_status
from ...conductor.gate_records import RELAY_AUTHOR, normalize_sha, verdict_heads
from ...conductor.handoff import parse_next_token
from ...github.client import GatePr, GatePrListing, OpenPrListingError, PrRef, ReviewInfo
from ...magickit.client import MagickitMcpError, McpToolCaller, ThreadResolvedError
from ..decide import PR_EVENT_RELAY_AUTHOR, Post
from .decide import AutoGateNoop, ThreadLink, already_fired, prefilter, tail_decision

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
DEFAULT_DAILY_CAP = 30
DEFAULT_GATE_BASES: tuple[str, ...] = ("main", "develop")
MAX_FIRES_PER_TICK = 3
#: The review states that are a gate verdict. DISMISSED and PENDING are not.
_VERDICT_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})


class GatePrSource(Protocol):
    """The GitHub reads 1c needs (satisfied by :class:`~...github.client.GitHubClient`)."""

    async def list_open_gate_prs(self, owner: str, repo: str) -> GatePrListing: ...

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]: ...


class CounterUnreadableError(Exception):
    """``state/auto-gate-daily.json`` exists but does not hold a counter."""


def jst_date(now: datetime) -> str:
    return now.astimezone(JST).date().isoformat()


@dataclass
class DailyCounter:
    date_jst: str
    fired: int = 0
    capped_on: str | None = None


class DailyCounterStore:
    """``<data_dir>/state/auto-gate-daily.json``, replaced atomically on every write."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, today: str) -> DailyCounter:
        """Today's counter. A stored count from another JST date reads as zero."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return DailyCounter(today)
        except (OSError, ValueError) as exc:
            raise CounterUnreadableError(f"{self.path}: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("date_jst"), str)
            or not isinstance(raw.get("fired"), int)
            or isinstance(raw.get("fired"), bool)
        ):
            raise CounterUnreadableError(f"{self.path}: unexpected shape")
        capped = raw.get("capped_on")
        capped_on = capped if isinstance(capped, str) else None
        fired = raw["fired"] if raw["date_jst"] == today else 0
        return DailyCounter(today, fired, capped_on)

    def save(self, counter: DailyCounter) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        payload = {
            "date_jst": counter.date_jst,
            "fired": counter.fired,
            "capped_on": counter.capped_on,
        }
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass(frozen=True)
class RepoTarget:
    project: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class AutoGateConfig:
    live: bool = False
    daily_cap: int = DEFAULT_DAILY_CAP
    gate_bases: tuple[str, ...] = DEFAULT_GATE_BASES


@dataclass
class PassReport:
    status: str = "ran"  # "ran" / "cap_reached" / "counter_unreadable"
    mode: str = "dry-run"
    date_jst: str = ""
    fired: int = 0
    cap: int = 0
    message: str | None = None
    capped_on: str | None = None
    examined: int = 0
    posted: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    noops: dict[str, list[str]] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    list_errors: list[dict[str, Any]] = field(default_factory=list)
    unrecognized: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        if self.mode != "dry-run":
            out.pop("decisions")
        return {k: v for k, v in out.items() if v not in (None, [], {})}


def _waiting_since(pr: GatePr, tail_timestamp: object) -> str:
    """The newer of the PR's creation and the tail's timestamp (msg-7485 ``waiting_since``).

    The design names ``ready_for_review``, which the listing does not carry; the creation time
    stands in rather than adding a timeline read per queued PR on every tick.
    """
    since = pr.created_at
    if isinstance(tail_timestamp, str):
        try:
            parsed = datetime.fromisoformat(tail_timestamp.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            since = max(since, aware)
    return since.isoformat()


async def _has_current_head_verdict(
    gh: GatePrSource,
    pr: GatePr,
    messages: Sequence[dict[str, Any]],
    review_login: str,
) -> bool:
    bodies = [str(m.get("content", "")) for m in messages if m.get("author") == RELAY_AUTHOR]
    head = normalize_sha(pr.head_sha)
    if head in verdict_heads(bodies):
        return True
    return any(
        review.login == review_login
        and review.state in _VERDICT_REVIEW_STATES
        and review.commit_id is not None
        and normalize_sha(review.commit_id) == head
        for review in await gh.fetch_pr_reviews_strict(pr.ref)
    )


@dataclass
class _Judged:
    """One PR's judgement: exactly one of ``noop`` / ``post`` / ``skip`` is set."""

    thread_id: str | None = None
    noop: AutoGateNoop | None = None
    post: Post | None = None
    skip: str | None = None
    detail: dict[str, Any] | None = None


async def _judge(
    *,
    mcp: McpToolCaller,
    gh: GatePrSource,
    target: RepoTarget,
    pr: GatePr,
    config: AutoGateConfig,
    review_login: str,
) -> _Judged:
    pre = prefilter(pr, config.gate_bases)
    if isinstance(pre, AutoGateNoop):
        return _Judged(noop=pre)
    link: ThreadLink = pre
    if link.noop is not None:
        detail: dict[str, Any] | None = None
        if link.noop is not AutoGateNoop.NO_THREAD:
            detail = {"ids": list(link.ids), "lines": list(link.unparsed_lines)}
        return _Judged(noop=link.noop, detail=detail)
    thread_id = link.thread_id
    assert thread_id is not None  # resolve_thread_link sets exactly one of thread_id / noop
    try:
        result = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": target.project, "thread_id": thread_id, "mode": "full"},
        )
    except MagickitMcpError as exc:
        return _Judged(thread_id=thread_id, skip=f"thread-unreadable: {exc}")
    raw_messages = result.get("messages") if isinstance(result, dict) else None
    messages = (
        [m for m in raw_messages if isinstance(m, dict)] if isinstance(raw_messages, list) else []
    )
    if not messages:
        return _Judged(thread_id=thread_id, skip="thread-unreadable: no messages")
    thread_meta = result.get("thread") if isinstance(result, dict) else None
    status = thread_meta.get("status") if isinstance(thread_meta, dict) else None
    if isinstance(status, str) and is_terminal_status(status):
        return _Judged(thread_id=thread_id, noop=AutoGateNoop.THREAD_FINISHED)
    repo_slug = f"{pr.ref.owner}/{pr.ref.repo}"
    if already_fired(messages, repo=repo_slug, number=pr.ref.number):
        return _Judged(thread_id=thread_id, noop=AutoGateNoop.ALREADY_AUTO_FIRED)
    try:
        if await _has_current_head_verdict(gh, pr, messages, review_login):
            return _Judged(thread_id=thread_id, noop=AutoGateNoop.ALREADY_GATED)
    except Exception as exc:  # isolation boundary: fail closed for this PR, reported
        logger.warning("auto-gate: reviews of %s unreadable: %r", pr.ref.slug, exc)
        return _Judged(thread_id=thread_id, skip=f"gh-error: {exc!r}")
    tail = messages[-1]
    tail_body = str(tail.get("content", ""))
    decision = tail_decision(tail_body, repo=repo_slug, number=pr.ref.number)
    if isinstance(decision, Post):
        return _Judged(thread_id=thread_id, post=decision)
    return _Judged(
        thread_id=thread_id,
        noop=decision,
        detail={
            "thread_id": thread_id,
            "tail_next": parse_next_token(tail_body),
            "tail_msg_id": tail.get("msg_id"),
            "waiting_since": _waiting_since(pr, tail.get("timestamp")),
        },
    )


async def _post(
    mcp: McpToolCaller, project: str, thread_id: str, decision: Post
) -> tuple[str | None, str | None]:
    """``(msg_id, None)`` on success, ``(None, why)`` on failure."""
    try:
        result = await mcp.call_tool(
            "chatroom_post_message",
            {
                "project": project,
                "thread_id": thread_id,
                "msg_type": "report",
                "author": PR_EVENT_RELAY_AUTHOR,
                "content": decision.render(),
                # No ``role``: a machine restating a GitHub fact holds none, as in 1b.
            },
        )
    except ThreadResolvedError as exc:  # disposition (1), see the module docstring
        logger.warning("auto-gate post dropped (thread %r resolved): %s", thread_id, exc)
        return None, "thread-resolved"
    except MagickitMcpError as exc:
        return None, f"post-failed: {exc}"
    msg = result.get("msg") if isinstance(result, dict) else None
    return (str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""), None


def _record_noop(report: PassReport, ref: str, judged: _Judged) -> None:
    assert judged.noop is not None
    report.noops.setdefault(judged.noop.value, []).append(ref)
    if judged.detail is not None:
        report.details.append({"pr_ref": ref, "reason": judged.noop.value, **judged.detail})


def _decision_row(ref: str, judged: _Judged) -> dict[str, Any]:
    if judged.post is not None:
        result, reason = "would-fire", None
    elif judged.noop is not None:
        result, reason = "noop", judged.noop.value
    else:
        result, reason = "skipped", judged.skip
    row = {"pr_ref": ref, "result": result, "reason": reason, "thread_id": judged.thread_id}
    return {k: v for k, v in row.items() if v is not None}


async def run_pass(
    *,
    mcp: McpToolCaller,
    gh: GatePrSource,
    targets: Sequence[RepoTarget],
    config: AutoGateConfig,
    store: DailyCounterStore,
    review_login: str,
    now: datetime,
) -> PassReport:
    """One 1c pass. One repo's or one PR's failure is reported and costs only that row."""
    today = jst_date(now)
    report = PassReport(
        mode="live" if config.live else "dry-run", date_jst=today, cap=config.daily_cap
    )
    try:
        counter = store.load(today)
    except CounterUnreadableError as exc:
        if config.live:
            report.status = "counter_unreadable"
            report.message = f"auto-gate: counter unreadable, pass skipped ({exc})"
            return report
        counter = DailyCounter(today)
    report.fired = counter.fired
    report.capped_on = counter.capped_on
    if config.live and counter.fired >= config.daily_cap:
        report.status = "cap_reached"
        report.message = (
            f"auto-gate: daily cap reached ({counter.fired}/{config.daily_cap}), pass skipped"
        )
        return report
    slots = min(MAX_FIRES_PER_TICK, config.daily_cap - counter.fired)
    for target in targets:
        try:
            listing = await gh.list_open_gate_prs(target.owner, target.repo)
        except OpenPrListingError as exc:
            report.list_errors.append(
                {"repo": target.slug, "outcome": exc.outcome, "error": str(exc)}
            )
            continue
        report.unrecognized += listing.unrecognized
        for pr in sorted(listing.prs, key=lambda row: row.ref.number):
            if config.live and slots <= 0:
                return report
            report.examined += 1
            ref = pr.ref.slug
            judged = await _judge(
                mcp=mcp, gh=gh, target=target, pr=pr, config=config, review_login=review_login
            )
            if not config.live:
                report.decisions.append(_decision_row(ref, judged))
            if judged.noop is not None:
                _record_noop(report, ref, judged)
            elif judged.skip is not None:
                report.failures.append(
                    {"pr_ref": ref, "thread_id": judged.thread_id, "reason": judged.skip}
                )
            elif config.live and judged.post is not None and judged.thread_id is not None:
                msg_id, why = await _post(mcp, target.project, judged.thread_id, judged.post)
                if why is not None:
                    report.failures.append(
                        {"pr_ref": ref, "thread_id": judged.thread_id, "reason": why}
                    )
                    continue
                slots -= 1
                counter.fired += 1
                report.fired = counter.fired
                if counter.fired >= config.daily_cap:
                    counter.capped_on = today
                    report.capped_on = today
                report.posted.append(
                    {"pr_ref": ref, "thread_id": judged.thread_id, "msg_id": msg_id}
                )
                try:
                    store.save(counter)
                except OSError as exc:
                    # The post is the fire record, so this cannot double-fire (msg-7737).
                    logger.warning("auto-gate: counter write failed: %r", exc)
                    report.failures.append({"pr_ref": ref, "reason": f"counter-write: {exc}"})
    return report


__all__ = [
    "DEFAULT_DAILY_CAP",
    "DEFAULT_GATE_BASES",
    "MAX_FIRES_PER_TICK",
    "AutoGateConfig",
    "CounterUnreadableError",
    "DailyCounter",
    "DailyCounterStore",
    "GatePrSource",
    "PassReport",
    "RepoTarget",
    "jst_date",
    "run_pass",
]
