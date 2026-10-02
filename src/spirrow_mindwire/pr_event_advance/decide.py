"""The pure half of 1b: one thread tail + one PR's facts → what to write, or why not.

Design: chatroom ``spirrow-mindwire/T-pr-event-advances-thread`` (Bohr msg-5901 v0.1 → msg-5910
v0.5, consolidated in msg-5912; Einstein msg-5911 / go). Behaviour source: ``docs/operator-board-
design.md`` §F.1.1.

What this decides
-----------------

The thread ↔ PR correspondence is derived from ONE message — the thread's tail — and nothing
else: no mapping table, no cache, no shadow DB. The tail must be authored by
:data:`~spirrow_mindwire.conductor.gate_records.RELAY_AUTHOR` and its first line must read as
:func:`~spirrow_mindwire.conductor.gate_records.render_relay_heading`. Only the first line and the
final ``NEXT:`` are read, so a critique that quotes a heading or a marker is not mistaken for one
(the ``verdict_heads`` discipline).

The table (v0.5 §2) is evaluated top to bottom and the first matching row wins:

==  ==================================  ==================================  =======================
#   tail                                PR                                  result
==  ==================================  ==================================  =======================
1   not a relay, or heading unreadable  —                                   Noop(NOT_RELAY_TAIL)
2   token = ``none``                    —                                   Noop(SETTLED)
3   relay                               merged                              Post: none / proposer
4   relay                               closed, not merged                  Post: proposer (always)
5   relay, no ci-hold                   open                                Noop(OPEN_NO_HOLD)
6   relay, ci-hold                      open, head moved                    Noop(HEAD_MOVED)
7   relay, ci-hold                      open, same head, CI not terminal    Noop(CI_PENDING)
8   relay, ci-hold                      open, same head, CI terminal        Post: pr-review <ref>
9   anything else (unexpected state)    —                                   Noop(UNKNOWN_STATE)
==  ==================================  ==================================  =======================

* Rows 3 and 4 come before rows 5-8, so a PR closed after a CI hold is reported as closed and a
  dead PR is never re-gated (§F.1.1 優先順位と排他).
* The token holder does not matter (Einstein msg-5903 B1): a REQUEST_CHANGES relay that sent
  the thread to the implementer still gets the closed/merged line. Only ``none`` — someone
  declared the thread finished — is left alone, so this never re-opens a settled thread.
* ``Closes-thread:`` counts only when it names THIS thread; any other value is "absent" and
  goes to the proposer — the failure direction never closes by mistake.
* A closed-unmerged PR ALWAYS goes to the proposer, whatever the PR body declares (§F.1.1).
* The proposer persona is consulted only on the rows that need it. A ``Closes-thread:`` merge
  and the (ii) re-fire never read the roster, so a broken roster cannot misroute them. When it
  IS needed and is ``None``, the fact is still written and the hand-off becomes the 3-line
  ``NEXT: operator`` form (msg-5904 B2) — silence would re-fail every tick unseen.
* Once a :class:`Post` lands, the tail is authored by :data:`PR_EVENT_RELAY_AUTHOR`, which row 1
  rejects: the second tick is a no-op without any counter (R5).

This module never closes a thread. ``NEXT: none`` is a conductor handoff token, not the
magickit ``chatroom_close_thread`` status change ``closeable_roles`` governs (msg-5906, R10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import assert_never

from ..conductor.gate_records import (
    RELAY_AUTHOR,
    ci_hold_head,
    normalize_sha,
    render_relay_heading,
)
from ..conductor.handoff import (
    NONE_TOKEN,
    OPERATOR_TASK_KEYWORD,
    OPERATOR_TOKEN,
    PR_REVIEW_TOKEN,
    TIER_C_CHECK_KEYWORD,
    TIER_C_CHECK_NONE,
    parse_next_token,
)
from ..github.client import CiState, parse_pr_ref

#: The author 1b writes under. Distinct from :data:`RELAY_AUTHOR` on purpose: the gate-records
#: readers (``verdict_heads`` / ``ci_route_heads`` / ``prior_advisory_approvals``) narrow to that
#: author, and 1b's posts are not gate records. Same reason ``conductor-relay`` is separate.
#: Registered in ``spec/identity/legitimate_roles.yaml`` as ``kind: machine`` /
#: ``legitimate: []`` (R3).
PR_EVENT_RELAY_AUTHOR = "pr-event-relay"

#: The prefix every verdict relay heading starts with, derived from the renderer so a change
#: to the heading text cannot silently desynchronise this reader.
_HEADING_PREFIX = render_relay_heading("", None)

#: ``Closes-thread: <thread_id>`` at the start of a line of the PR body. Several are allowed.
_CLOSES_THREAD_RE = re.compile(r"^Closes-thread:[ \t]*(T-\S+)[ \t]*\r?$", re.MULTILINE)

#: The CI states that count as "CI has reached a terminal state". ``UNKNOWN`` is not one: it is
#: the fail-closed value for a read that did not answer, so it waits like ``PENDING``.
_CI_TERMINAL = frozenset({CiState.SUCCESS, CiState.FAILURE})


class PrLifecycle(StrEnum):
    """The three PR states §F.1.1 names. Anything else is row 9."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class NoopReason(StrEnum):
    """Why nothing is written (v0.5 §1). Printed verbatim in the CLI output."""

    NOT_RELAY_TAIL = "not-relay-tail"
    SETTLED = "settled"
    OPEN_NO_HOLD = "open-no-hold"
    CI_PENDING = "ci-pending"
    HEAD_MOVED = "head-moved"
    UNKNOWN_STATE = "unknown-state"


@dataclass(frozen=True)
class Noop:
    reason: NoopReason


@dataclass(frozen=True)
class Post:
    """One message to write. ``next_line`` is the whole hand-off block (1 or 3 lines)."""

    body: str
    next_line: str

    def render(self) -> str:
        return f"{self.body}\n\n{self.next_line}"


Decision = Noop | Post


@dataclass(frozen=True)
class RelayTail:
    """What 1b reads off a verdict-relay tail: the PR, its NEXT token, and any ci-hold head."""

    pr_ref: str
    token: str | None
    hold_head: str | None


@dataclass(frozen=True)
class PrFacts:
    """The PR facts §F.1.1 needs, read fresh from GitHub on every tick.

    ``state`` is a plain string so an unexpected value reaches row 9 instead of failing to
    construct; :func:`lifecycle_of` maps it.
    """

    state: str
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    closed_at: datetime | None = None
    body: str = ""


def read_relay_tail(author: str, body: str) -> RelayTail | None:
    """The tail as a verdict relay, or ``None`` if it is not one (row 1)."""
    if author != RELAY_AUTHOR:
        return None
    stripped = body.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    if not first_line.startswith(_HEADING_PREFIX):
        return None
    rest = first_line[len(_HEADING_PREFIX) :]
    ref = rest.split(" @ ", 1)[0].strip()
    if parse_pr_ref(ref) is None:
        return None
    token = parse_next_token(body)
    return RelayTail(pr_ref=ref, token=token, hold_head=ci_hold_head(body))


def precheck(tail: RelayTail | None) -> Noop | None:
    """Rows 1 and 2 — the rows that need no GitHub read. ``None`` means "go read the PR"."""
    if tail is None:
        return Noop(NoopReason.NOT_RELAY_TAIL)
    if tail.token is not None and tail.token.lower() == NONE_TOKEN:
        return Noop(NoopReason.SETTLED)
    return None


def lifecycle_of(pr: PrFacts) -> PrLifecycle | None:
    try:
        return PrLifecycle(pr.state)
    except ValueError:
        return None


def needs_ci(tail: RelayTail | None, pr: PrFacts) -> bool:
    """True iff :func:`decide` would reach rows 7/8 — the only rows that read CI."""
    if precheck(tail) is not None or tail is None or tail.hold_head is None:
        return False
    return lifecycle_of(pr) is PrLifecycle.OPEN and _same_head(tail.hold_head, pr.head_sha)


def closes_thread(pr_body: str, thread_id: str) -> bool:
    """True iff the PR body declares ``Closes-thread: <thread_id>`` for THIS thread."""
    return any(match == thread_id for match in _CLOSES_THREAD_RE.findall(pr_body))


def operator_handoff(thread_id: str) -> str:
    """The 3-line ``NEXT: operator`` block used when the proposer cannot be resolved (B2)."""
    return (
        f"{OPERATOR_TASK_KEYWORD}: {thread_id} の proposer persona を roster で解決できない。"
        "roster を直し、このスレッドの proposer に手で渡す\n"
        f"{TIER_C_CHECK_KEYWORD}: {TIER_C_CHECK_NONE}\n"
        f"NEXT: {OPERATOR_TOKEN}"
    )


def _same_head(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and normalize_sha(a) == normalize_sha(b)


def _to_proposer(thread_id: str, proposer: str | None) -> str:
    return f"NEXT: {proposer}" if proposer else operator_handoff(thread_id)


def _heading(pr_ref: str) -> str:
    return f"PR event (1b, {PR_EVENT_RELAY_AUTHOR}) — {pr_ref}"


def decide(
    *,
    thread_id: str,
    tail: RelayTail | None,
    pr: PrFacts,
    ci: CiState | None,
    proposer: str | None,
) -> Decision:
    """The v0.5 §2 table. Total: every input returns a :class:`Noop` or a :class:`Post`.

    ``ci`` is the CI state of the PR's current head, or ``None`` when it was not read (the
    caller reads it only when :func:`needs_ci` says so). ``proposer`` is the roster persona for
    the proposer role, or ``None`` when the roster could not name exactly one.
    """
    early = precheck(tail)
    if early is not None:
        return early
    assert tail is not None  # precheck returned None, so the tail is a relay
    lifecycle = lifecycle_of(pr)
    if lifecycle is None:
        return Noop(NoopReason.UNKNOWN_STATE)  # row 9
    ref = tail.pr_ref
    match lifecycle:
        case PrLifecycle.MERGED:  # row 3
            sha = pr.merge_commit_sha or "merge commit 不明"
            body = f"{_heading(ref)}\n\nPR {ref} が merge された（{sha}）。"  # noqa: RUF001 (§F.1.1 wording)
            if closes_thread(pr.body, thread_id):
                return Post(body=body, next_line=f"NEXT: {NONE_TOKEN}")
            return Post(body=body, next_line=_to_proposer(thread_id, proposer))
        case PrLifecycle.CLOSED:  # row 4 — the Closes-thread declaration is ignored here
            closed_at = pr.closed_at.isoformat() if pr.closed_at else "closed_at 不明"
            head = pr.head_sha or "head 不明"
            body = (
                f"{_heading(ref)}\n\n"
                f"PR {ref} が merge されずに close された（{closed_at}、最終 head {head}）。"  # noqa: RUF001 (§F.1.1 wording)
                "本スレッドの作業はこの PR では landing していない。"
            )
            return Post(body=body, next_line=_to_proposer(thread_id, proposer))
        case PrLifecycle.OPEN:
            if tail.hold_head is None:
                return Noop(NoopReason.OPEN_NO_HOLD)  # row 5
            if not _same_head(tail.hold_head, pr.head_sha):
                return Noop(NoopReason.HEAD_MOVED)  # row 6
            if ci is None or ci not in _CI_TERMINAL:
                return Noop(NoopReason.CI_PENDING)  # row 7
            body = (  # row 8
                f"{_heading(ref)}\n\n"
                f"PR {ref} の CI が head {tail.hold_head} で終端した（{ci.value}）ので、"  # noqa: RUF001 (§F.1.1 wording)
                "gate を 1 回だけ撃ち直す。"
            )
            return Post(body=body, next_line=f"NEXT: {PR_REVIEW_TOKEN} {ref}")
        case _:
            assert_never(lifecycle)


__all__ = [
    "PR_EVENT_RELAY_AUTHOR",
    "Decision",
    "Noop",
    "NoopReason",
    "Post",
    "PrFacts",
    "PrLifecycle",
    "RelayTail",
    "closes_thread",
    "decide",
    "lifecycle_of",
    "needs_ci",
    "operator_handoff",
    "precheck",
    "read_relay_tail",
]
