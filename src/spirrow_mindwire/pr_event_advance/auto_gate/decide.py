"""The pure half of 1c: one open PR + its design thread -> fire the first gate, or why not.

Design: chatroom ``spirrow-mindwire/T-gate-fires-on-pr-open``. D1/D2/D4 are Bohr msg-7483 (with
queued-behind from msg-7485), the PR-body grammar is msg-7489 (preprocessing) + msg-7495 (v0.7
extraction), the bot rule is msg-7501, and "already fired" is msg-7737.

1c never fires the gate itself. It writes one ``NEXT: pr-review <ref>`` into the PR's design
thread, and the conductor does the rest (admission, ci-hold, R6, verdict relay).

Evaluation order, first match wins:

==  =======================================  ==============================================
#   condition                                result
==  =======================================  ==============================================
1   draft                                    Noop(draft)
2   bot author                               Noop(bot-author)
3   base not in ``gate_bases``               Noop(stacked)
4   ``Gate: hold`` line                      Noop(hold)
5   thread link missing / bad                Noop(no-thread / thread-line-unparsed / ambiguous)
5a  a bare id, PR project unresolved         (runner, msg-8032 §4 / msg-8034 §4)
    some clone origin unread                 skip pr-project-incomplete
    repo in no / several projects            skip pr-project-ambiguous
5b  link names another project:              (runner, msg-8022 §2)
    not in ``clones``                        skip thread-project-unknown
    in ``clones``, held out of this pass     Noop(thread-project-held)
6   design thread finished                   Noop(thread-finished)
7   fired before (marker or manual NEXT)     Noop(already-auto-fired), any head
8   a verdict on the CURRENT head            Noop(already-gated)
9   tail token is ``none`` / ``operator``    Post ``NEXT: pr-review <ref>``
10  any other tail token                     Noop(queued-behind)
==  =======================================  ==============================================

Row 7 before row 8 is msg-7737 §4: verdicts need reading only for a PR with no fire record. Row 8
is same-head on purpose: msg-7737 (approved in msg-7738) has a PR with a verdict only on an older
head fire once, which supersedes the head-agnostic row 4 of msg-7483.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ...conductor.handoff import (
    NONE_TOKEN,
    OPERATOR_TOKEN,
    PR_REVIEW_TOKEN,
    HandoffKind,
    parse_next_token,
    resolve_handoff,
    settles_by_terminal_form,
)
from ...github.client import GatePr
from ..decide import (
    PR_EVENT_RELAY_AUTHOR,
    THREAD_LINE_RE,
    THREAD_REF_RE,
    Post,
    body_lines,
    thread_refs,
)

#: msg-7489 O5. The lookahead stops ``holder`` / ``hold-out`` without ``\w``, which would also stop
#: ``hold待ち``. Nothing is extracted, so the whole pattern may ignore case.
GATE_HOLD_RE = re.compile(r"^gate:\s*hold(?![A-Za-z0-9_-]).*$", re.IGNORECASE)
#: msg-7737 §1. Whole-line, and read only from :data:`PR_EVENT_RELAY_AUTHOR`'s own posts.
_MARKER_RE = re.compile(r"^<!-- mindwire:auto-gate v1 (?P<payload>\{.*\}) -->$")
_HTML_COMMENT_RE = re.compile(r"^<!--.*-->$")


class AutoGateNoop(StrEnum):
    """Why a PR is not fired. Printed verbatim in the tick log."""

    DRAFT = "draft"
    BOT_AUTHOR = "bot-author"
    STACKED = "stacked"
    HOLD = "hold"
    NO_THREAD = "no-thread"
    THREAD_LINE_UNPARSED = "thread-line-unparsed"
    AMBIGUOUS = "ambiguous"
    THREAD_FINISHED = "thread-finished"
    ALREADY_AUTO_FIRED = "already-auto-fired"
    ALREADY_GATED = "already-gated"
    QUEUED_BEHIND = "queued-behind"
    #: msg-8022 Obj-2-2: the ``Thread:`` line names another project that the wrapper's control
    #: HOLD left out of this pass. Not :attr:`HOLD`, which is the PR body's own ``Gate: hold``.
    THREAD_PROJECT_HELD = "thread-project-held"


@dataclass(frozen=True)
class ThreadLink:
    """What the PR body says about its design thread."""

    thread_id: str | None = None
    #: The project the body named for the thread; ``None`` means the PR's own project.
    thread_project: str | None = None
    noop: AutoGateNoop | None = None
    ids: tuple[str, ...] = ()
    unparsed_lines: tuple[str, ...] = ()
    #: Some ``Thread:`` / ``Closes-thread:`` id carried no project, so it needs the PR's project.
    has_bare: bool = False


def has_gate_hold(body: str) -> bool:
    return any(GATE_HOLD_RE.match(line) for line in body_lines(body))


def is_bot_author(login: str, user_type: str) -> bool:
    return user_type == "Bot" or login.endswith("[bot]")


def _label(key: tuple[str | None, str], pr_project: str | None) -> str:
    """``T-x`` for the PR's own project (as before msg-8022), ``<project>/T-x`` otherwise."""
    project, thread_id = key
    return thread_id if project in (None, pr_project) else f"{project}/{thread_id}"


def resolve_thread_link(body: str, pr_project: str | None = None) -> ThreadLink:
    """Every ``Thread:`` and ``Closes-thread:`` line together (msg-7491 O7, msg-7495, msg-8024).

    Each declared ``[<project>/]T-...`` is resolved to a ``(project, id)`` pair first, an
    unqualified one taking ``pr_project`` (the project the PR's repo belongs to), and the pairs are
    what is de-duplicated. So ``Thread: p/T-1`` + ``Closes-thread: p/T-1`` is one link, and so is
    ``Thread: T-1`` + ``Closes-thread: p/T-1`` when ``p`` is the PR's own project; naming ``T-1``
    in two different projects is :attr:`AutoGateNoop.AMBIGUOUS` (msg-8025).

    The returned ``thread_project`` is ``None`` when the link is the PR's own project (or no
    ``pr_project`` was given and the id was unqualified); otherwise the project the body named.
    """
    refs = thread_refs(body)
    has_bare = any(ref.project is None for ref in (*refs.thread_lines, *refs.closes))
    keys: dict[tuple[str | None, str], None] = dict.fromkeys(
        ref.resolved(pr_project) for ref in (*refs.thread_lines, *refs.closes)
    )
    unique = tuple(keys)
    labels = tuple(_label(key, pr_project) for key in unique)
    unparsed = refs.unparsed_lines
    if len(unique) >= 2:
        return ThreadLink(
            noop=AutoGateNoop.AMBIGUOUS, ids=labels, unparsed_lines=unparsed, has_bare=has_bare
        )
    if unparsed:
        return ThreadLink(
            noop=AutoGateNoop.THREAD_LINE_UNPARSED,
            ids=labels,
            unparsed_lines=unparsed,
            has_bare=has_bare,
        )
    if not unique:
        return ThreadLink(noop=AutoGateNoop.NO_THREAD)
    project, thread_id = unique[0]
    return ThreadLink(
        thread_id=thread_id,
        thread_project=None if project == pr_project else project,
        ids=labels,
        has_bare=has_bare,
    )


def prefilter(
    pr: GatePr, gate_bases: Iterable[str], pr_project: str | None = None
) -> AutoGateNoop | ThreadLink:
    """Rows 1-5: everything decidable from the listing row alone.

    ``pr_project`` is the project the PR's repo belongs to; see :func:`resolve_thread_link`.
    """
    if pr.draft:
        return AutoGateNoop.DRAFT
    if is_bot_author(pr.author_login, pr.author_type):
        return AutoGateNoop.BOT_AUTHOR
    if pr.base_ref not in gate_bases:
        return AutoGateNoop.STACKED
    if has_gate_hold(pr.body):
        return AutoGateNoop.HOLD
    return resolve_thread_link(pr.body, pr_project)


def render_marker(*, repo: str, pr: int) -> str:
    payload = json.dumps({"repo": repo, "pr": pr}, ensure_ascii=True)
    return f"<!-- mindwire:auto-gate v1 {payload} -->"


def _marker_names(body: str, repo: str, number: int) -> bool:
    for line in body.splitlines():
        match = _MARKER_RE.match(line.strip())
        if match is None:
            continue
        try:
            payload = json.loads(match.group("payload"))
        except ValueError:
            continue
        if (
            isinstance(payload, dict)
            and str(payload.get("repo", "")).casefold() == repo.casefold()
            and payload.get("pr") == number
        ):
            return True
    return False


def _final_line(body: str) -> str | None:
    """The last line that is not blank and not an HTML comment.

    The loop appends ``<!-- source: ... -->`` lines after a post's handoff, so the literal last
    line of a stored message is often not the one its author ended on.
    """
    for line in reversed(body.splitlines()):
        stripped = line.strip()
        if stripped and not _HTML_COMMENT_RE.match(stripped):
            return stripped
    return None


def _manual_fire_names(body: str, slug: str) -> bool:
    last = _final_line(body)
    if last is None:
        return False
    handoff = resolve_handoff(last, {})
    return (
        handoff.kind is HandoffKind.PR_REVIEW
        and handoff.token is not None
        and handoff.token.casefold() == slug.casefold()
    )


def already_fired(messages: Iterable[Mapping[str, object]], *, repo: str, number: int) -> bool:
    """msg-7737 §2-3: the thread's own history is the record; heads are not compared."""
    slug = f"{repo}#{number}"
    for message in messages:
        body = str(message.get("content", ""))
        if message.get("author") == PR_EVENT_RELAY_AUTHOR and _marker_names(body, repo, number):
            return True
        if _manual_fire_names(body, slug):
            return True
    return False


def tail_decision(tail_body: str, *, repo: str, number: int) -> AutoGateNoop | Post:
    """Rows 9-10. One thread carries one token, so a later PR waits its turn (msg-7485 O1)."""
    # An accepted terminal form (``STOP: done`` / ``BLOCKED-ON:``) settles like ``NEXT: none``
    # (T-done-disposition-auto-close design v2.4): the thread is not waiting on anyone's turn.
    token = NONE_TOKEN if settles_by_terminal_form(tail_body) else parse_next_token(tail_body)
    if token is None or token.casefold() not in (NONE_TOKEN, OPERATOR_TOKEN):
        return AutoGateNoop.QUEUED_BEHIND
    slug = f"{repo}#{number}"
    return Post(
        body=(
            f"PR event (1c, {PR_EVENT_RELAY_AUTHOR}) — {slug}\n\n"
            f"PR {slug} は open で gate がまだ撃たれていないので、初回の gate を撃つ。"
        ),
        next_line=f"{render_marker(repo=repo, pr=number)}\nNEXT: {PR_REVIEW_TOKEN} {slug}",
    )


__all__ = [
    "GATE_HOLD_RE",
    "THREAD_LINE_RE",
    "THREAD_REF_RE",
    "AutoGateNoop",
    "ThreadLink",
    "already_fired",
    "has_gate_hold",
    "is_bot_author",
    "prefilter",
    "render_marker",
    "resolve_thread_link",
    "tail_decision",
]
