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
    (an accepted terminal form reads as
    ``none`` only if the roster also accepts
    it, see :func:`settled_tail_refusal`)
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
from datetime import datetime
from enum import StrEnum

from ...conductor.handoff import (
    NONE_TOKEN,
    OPERATOR_TOKEN,
    PR_REVIEW_TOKEN,
    HandoffKind,
    HumanAsk,
    none_refused_after_cutoff,
    operator_refused_after_cutoff,
    parse_next_token,
    refused_by_terminal_form,
    resolve_handoff,
    settles_by_terminal_form,
)
from ...github.client import GatePr
from ...value_objects import Role
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


#: :func:`settled_tail_refusal` when 1c has no roster to read a settled tail with.
ROSTER_UNAVAILABLE = "roster-unavailable"
#: Prefix of :func:`settled_tail_refusal` when the roster (or the field) refuses the tail. The
#: same words the park-wake tick logs for a notice it could not post.
TERMINAL_FORM_REFUSED = "terminal-form-refused"


def settled_tail_refusal(
    tail_body: str,
    *,
    roster: Mapping[str, Role] | None,
    next_participant: str | None = None,
    posted_at: datetime | None = None,
) -> str | None:
    """Why a tail the sweep reads as settled must still not be treated as idle, or ``None``.

    ``terminal_form_view`` is roster-free, so a tail it calls SETTLES may still be refused once
    the roster and the ``next_participant`` field are applied (a ``BLOCKED-ON`` wake outside
    ``STOP_WAKE_ROLES``, a field that contradicts the body). The notice for such a head is the
    park-wake tick's (``park_wake/runner.py``); if 1c fired the gate on it first, the head would
    move and the next tick would never see the refused form, so it would never be reported
    (T-done-disposition-auto-close msg-8218 §2b-2). Read with the roster first, then:

    - no roster (``None`` or empty) → :data:`ROSTER_UNAVAILABLE`. Firing without the check could
      bury the head, so this side does not fire;
    - the roster or the field refuses it → ``"terminal-form-refused: <reason>"``;
    - accepted, or not a SETTLES tail at all → ``None``.
    """
    if not settles_by_terminal_form(tail_body):
        return None
    if not roster:
        return ROSTER_UNAVAILABLE
    handoff = resolve_handoff(
        tail_body, roster, next_participant=next_participant, posted_at=posted_at
    )
    if handoff.malformed is not None:
        return f"{TERMINAL_FORM_REFUSED}: {handoff.malformed.value}"
    return None


def _is_human_task_tail(tail_body: str) -> bool:
    """A tail in the ``HUMAN-TASK:`` form: idle exactly as a ``NEXT: operator`` tail is.

    The successor form asks a person for work by hand (``HumanAsk.OPERATOR_WORK``) under the
    ``human`` token. A plain ``NEXT: human`` -- a decision -- stays a live turn, as before.
    """
    resolved = resolve_handoff(tail_body, {})
    return resolved.kind is HandoffKind.HUMAN and resolved.human_ask is HumanAsk.OPERATOR_WORK


def tail_decision(
    tail_body: str,
    *,
    repo: str,
    number: int,
    roster: Mapping[str, Role] | None,
    next_participant: str | None = None,
    posted_at: datetime | None = None,
) -> AutoGateNoop | Post:
    """Rows 9-10. One thread carries one token, so a later PR waits its turn (msg-7485 O1).

    ``roster`` is the conductor's identity→role roster and ``next_participant`` the tail's
    structured field. Both matter only for a tail in an accepted terminal form
    (:func:`settled_tail_refusal`); ``roster`` is required so that no caller can leave it out.
    ``posted_at`` is the tail's posting time, for the boundary-5 ``NEXT: none`` cutoff.
    """
    # An accepted terminal form (``STOP: done`` / ``BLOCKED-ON:``) settles like ``NEXT: none``
    # (T-done-disposition-auto-close design v2.4): the thread is not waiting on anyone's turn.
    # A refused one is not an idle tail whatever its ``NEXT:`` token says (``BLOCKED-ON: pr a/b#1``
    # above ``NEXT: none``): firing the gate on it would bury the head the conductor is about to
    # answer with its MALFORMED notice (PR #531 gate msg-8201). It waits like a live turn.
    if refused_by_terminal_form(tail_body):
        return AutoGateNoop.QUEUED_BEHIND
    # Boundary 5: a legacy ``NEXT: none`` the cutoff refuses is not idle either. head_skip
    # LAUNCHes it and the conductor answers it with the MALFORMED notice; a gate fired first
    # would bury that head the same way.
    if none_refused_after_cutoff(tail_body, posted_at=posted_at):
        return AutoGateNoop.QUEUED_BEHIND
    # The same for a ``NEXT: operator`` the operator cutoff retires
    # (T-retire-operator-identity-and-scope-next-fermi): its ``operator_retired`` notice is the
    # conductor's to post.
    if operator_refused_after_cutoff(tail_body, posted_at=posted_at):
        return AutoGateNoop.QUEUED_BEHIND
    # The same holds for a form only the roster refuses: its notice is the park-wake tick's, and
    # a gate fired first would bury it (msg-8218 §2b-2). So does a settled tail 1c cannot check.
    if settled_tail_refusal(
        tail_body, roster=roster, next_participant=next_participant, posted_at=posted_at
    ):
        return AutoGateNoop.QUEUED_BEHIND
    token = NONE_TOKEN if settles_by_terminal_form(tail_body) else parse_next_token(tail_body)
    if token is None or not (
        token.casefold() in (NONE_TOKEN, OPERATOR_TOKEN) or _is_human_task_tail(tail_body)
    ):
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
    "ROSTER_UNAVAILABLE",
    "TERMINAL_FORM_REFUSED",
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
    "settled_tail_refusal",
    "tail_decision",
]
