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
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ...chatroom.status import is_terminal_status
from ...conductor.gate_records import RELAY_AUTHOR, normalize_sha, verdict_heads
from ...conductor.handoff import parse_next_token
from ...github.client import GatePr, GatePrListing, OpenPrListingError, PrRef, ReviewInfo
from ...magickit.client import MagickitMcpError, McpToolCaller, ThreadResolvedError
from ...repo_projects import INCOMPLETE, PrProject, RepoProjectMap, RepoTarget, repo_key
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


def _cap_window(today: str) -> tuple[str, str]:
    """The JST dates whose cap the digest still shows: today and yesterday."""
    try:
        day = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return (today, today)
    return (today, (day - timedelta(days=1)).isoformat())


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
        # PR #512 review (msg-7820 #2): a cap day older than yesterday is one the daily digest has
        # already shown (cap_digest_line reads today and yesterday only), so it is dropped here and
        # the next save stops carrying it. Yesterday's is kept: the digest has yet to show it.
        if capped_on is not None and capped_on not in _cap_window(today):
            capped_on = None
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
    """One PR's judgement: exactly one of ``noop`` / ``post`` / ``skip`` is set.

    ``project`` is the project the design thread was read in, and so the one a post goes to: the
    PR's own, or the one its ``Thread:`` line names (msg-8022 §2).
    """

    thread_id: str | None = None
    project: str | None = None
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
    pr_project: PrProject,
    known_projects: Collection[str],
    pass_projects: Collection[str],
) -> _Judged:
    own = pr_project.project
    pre = prefilter(pr, config.gate_bases, own)
    if isinstance(pre, AutoGateNoop):
        return _Judged(noop=pre)
    link: ThreadLink = pre
    if link.has_bare and own is None:
        # msg-8032 §4 / msg-8034 §4: a bare id needs the PR's project, and the repo does not name
        # exactly one with every clone read. A qualified id below needs no such lookup. ``none``
        # (no clone names the repo) cannot arise for a target built from the same table, and is
        # reported with the multiple-candidate code: either way no single project is named.
        kind = (
            "pr-project-incomplete" if pr_project.reason == INCOMPLETE else "pr-project-ambiguous"
        )
        listed = ", ".join(pr_project.projects) or (pr_project.reason or "")
        return _Judged(
            skip=f"{kind}: {listed}",
            detail={"pr_project": pr_project.describe(), "ids": list(link.ids)},
        )
    if link.noop is not None:
        detail: dict[str, Any] | None = None
        if link.noop is not AutoGateNoop.NO_THREAD:
            detail = {"ids": list(link.ids), "lines": list(link.unparsed_lines)}
        return _Judged(noop=link.noop, detail=detail)
    thread_id = link.thread_id
    assert thread_id is not None  # resolve_thread_link sets exactly one of thread_id / noop
    # msg-8022 §2-3: a project is taken only when the body names it; nothing is searched for.
    project = link.thread_project or own
    assert project is not None  # no bare id when own is None, so the body named the project
    if project != own:
        cross = {"thread_project": project}
        if project not in known_projects:
            return _Judged(
                thread_id=thread_id,
                skip=f"thread-project-unknown: {project}",
                detail=cross,
            )
        if project not in pass_projects:
            return _Judged(
                thread_id=thread_id,
                project=project,
                noop=AutoGateNoop.THREAD_PROJECT_HELD,
                detail=cross,
            )
    judged = await _judge_thread(
        mcp=mcp,
        gh=gh,
        pr=pr,
        project=project,
        thread_id=thread_id,
        review_login=review_login,
    )
    if project != own:
        judged.detail = {**(judged.detail or {}), "thread_project": project}
    return judged


async def _judge_thread(
    *,
    mcp: McpToolCaller,
    gh: GatePrSource,
    pr: GatePr,
    project: str,
    thread_id: str,
    review_login: str,
) -> _Judged:
    """Rows 6-10, read in ``project`` — the design thread's project, not necessarily the PR's.

    Any read error, ``not found`` included, is ``thread-unreadable``: there is no fallback to
    another project (msg-8022 §3), so an outage is never taken for "it lives elsewhere".
    """
    try:
        result = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "full"},
        )
    except MagickitMcpError as exc:
        return _Judged(thread_id=thread_id, project=project, skip=f"thread-unreadable: {exc}")
    raw_messages = result.get("messages") if isinstance(result, dict) else None
    messages = (
        [m for m in raw_messages if isinstance(m, dict)] if isinstance(raw_messages, list) else []
    )
    if not messages:
        return _Judged(thread_id=thread_id, project=project, skip="thread-unreadable: no messages")
    thread_meta = result.get("thread") if isinstance(result, dict) else None
    status = thread_meta.get("status") if isinstance(thread_meta, dict) else None
    if isinstance(status, str) and is_terminal_status(status):
        return _Judged(thread_id=thread_id, project=project, noop=AutoGateNoop.THREAD_FINISHED)
    repo_slug = f"{pr.ref.owner}/{pr.ref.repo}"
    if already_fired(messages, repo=repo_slug, number=pr.ref.number):
        return _Judged(thread_id=thread_id, project=project, noop=AutoGateNoop.ALREADY_AUTO_FIRED)
    try:
        if await _has_current_head_verdict(gh, pr, messages, review_login):
            return _Judged(thread_id=thread_id, project=project, noop=AutoGateNoop.ALREADY_GATED)
    except Exception as exc:  # isolation boundary: fail closed for this PR, reported
        logger.warning("auto-gate: reviews of %s unreadable: %r", pr.ref.slug, exc)
        return _Judged(thread_id=thread_id, project=project, skip=f"gh-error: {exc!r}")
    tail = messages[-1]
    tail_body = str(tail.get("content", ""))
    decision = tail_decision(tail_body, repo=repo_slug, number=pr.ref.number)
    if isinstance(decision, Post):
        return _Judged(thread_id=thread_id, project=project, post=decision)
    return _Judged(
        thread_id=thread_id,
        project=project,
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
    row = {
        "pr_ref": ref,
        "result": result,
        "reason": reason,
        "thread_id": judged.thread_id,
        "thread_project": (judged.detail or {}).get("thread_project"),
    }
    return {k: v for k, v in row.items() if v is not None}


def _failure_row(ref: str, judged: _Judged, reason: str) -> dict[str, Any]:
    row: dict[str, Any] = {"pr_ref": ref, "thread_id": judged.thread_id, "reason": reason}
    for key in ("thread_project", "pr_project"):
        value = (judged.detail or {}).get(key)
        if value is not None:
            row[key] = value
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
    known_projects: Collection[str],
    repo_map: RepoProjectMap,
) -> PassReport:
    """One 1c pass. One repo's or one PR's failure is reported and costs only that row.

    ``known_projects`` is every project in ``sweep.json``'s ``clones``; ``targets`` are the ones
    the wrapper let through its control HOLD this tick. A ``Thread:`` line naming a project in
    the first but not the second is :attr:`AutoGateNoop.THREAD_PROJECT_HELD` (msg-8022 §2).

    ``repo_map`` gives each repo's own project for a bare id. Targets sharing one repo are judged
    once (msg-8032 §4): judging the same PR under two projects would read a bare id twice, as two
    different threads.
    """
    pass_projects = frozenset(target.project for target in targets)
    seen: set[str] = set()
    unique_targets: list[RepoTarget] = []
    for target in targets:
        key = repo_key(target.owner, target.repo)
        if key not in seen:
            seen.add(key)
            unique_targets.append(target)
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
    for target in unique_targets:
        pr_project = repo_map.resolve(target.owner, target.repo)
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
                mcp=mcp,
                gh=gh,
                target=target,
                pr=pr,
                config=config,
                review_login=review_login,
                pr_project=pr_project,
                known_projects=known_projects,
                pass_projects=pass_projects,
            )
            if not config.live:
                report.decisions.append(_decision_row(ref, judged))
            if judged.noop is not None:
                _record_noop(report, ref, judged)
            elif judged.skip is not None:
                report.failures.append(_failure_row(ref, judged, judged.skip))
            elif (
                config.live
                and judged.post is not None
                and judged.thread_id is not None
                and judged.project is not None
            ):
                # Into the thread's project, which is not the PR's when the body named another.
                msg_id, why = await _post(mcp, judged.project, judged.thread_id, judged.post)
                if why is not None:
                    report.failures.append(_failure_row(ref, judged, why))
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
