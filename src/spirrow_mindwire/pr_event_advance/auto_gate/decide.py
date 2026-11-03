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
)
from ...github.client import GatePr
from ..decide import _CLOSES_THREAD_RE, PR_EVENT_RELAY_AUTHOR, Post

#: msg-7489 O5. The lookahead stops ``holder`` / ``hold-out`` without ``\w``, which would also stop
#: ``hold待ち``. Nothing is extracted, so the whole pattern may ignore case.
GATE_HOLD_RE = re.compile(r"^gate:\s*hold(?![A-Za-z0-9_-]).*$", re.IGNORECASE)
#: msg-7493 / msg-7495. Only the keyword ignores case, so ``t-foo`` stays unparsed.
THREAD_LINE_RE = re.compile(r"^(?i:thread):\s*(?P<rest>.*)$")
THREAD_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])T-[A-Za-z0-9_-]+(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])")
#: msg-7737 §1. Whole-line, and read only from :data:`PR_EVENT_RELAY_AUTHOR`'s own posts.
_MARKER_RE = re.compile(r"^<!-- mindwire:auto-gate v1 (?P<payload>\{.*\}) -->$")
_HTML_COMMENT_RE = re.compile(r"^<!--.*-->$")
_BOM = "\ufeff"


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


@dataclass(frozen=True)
class ThreadLink:
    """What the PR body says about its design thread."""

    thread_id: str | None = None
    noop: AutoGateNoop | None = None
    ids: tuple[str, ...] = ()
    unparsed_lines: tuple[str, ...] = ()


def _body_lines(body: str) -> list[str]:
    """msg-7489 O6: unify newlines, strip each line, drop quotes. Each line stands alone (O3).

    ``str.strip`` does not remove U+FEFF, so it is stripped explicitly (msg-7485 lists BOM).
    """
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = (line.strip().strip(_BOM).strip() for line in text.split("\n"))
    return [line for line in lines if not line.startswith(">")]


def has_gate_hold(body: str) -> bool:
    return any(GATE_HOLD_RE.match(line) for line in _body_lines(body))


def is_bot_author(login: str, user_type: str) -> bool:
    return user_type == "Bot" or login.endswith("[bot]")


def resolve_thread_link(body: str) -> ThreadLink:
    """Every ``Thread:`` and ``Closes-thread:`` line together (msg-7491 O7, msg-7495)."""
    ids: dict[str, None] = dict.fromkeys(_CLOSES_THREAD_RE.findall(body))
    unparsed: list[str] = []
    for line in _body_lines(body):
        match = THREAD_LINE_RE.match(line)
        if match is None:
            continue
        found = THREAD_ID_RE.findall(match.group("rest"))
        if not found:
            unparsed.append(line)
        ids.update(dict.fromkeys(found))
    unique = tuple(ids)
    if len(unique) >= 2:
        return ThreadLink(noop=AutoGateNoop.AMBIGUOUS, ids=unique, unparsed_lines=tuple(unparsed))
    if unparsed:
        return ThreadLink(
            noop=AutoGateNoop.THREAD_LINE_UNPARSED, ids=unique, unparsed_lines=tuple(unparsed)
        )
    if not unique:
        return ThreadLink(noop=AutoGateNoop.NO_THREAD)
    return ThreadLink(thread_id=unique[0], ids=unique)


def prefilter(pr: GatePr, gate_bases: Iterable[str]) -> AutoGateNoop | ThreadLink:
    """Rows 1-5: everything decidable from the listing row alone."""
    if pr.draft:
        return AutoGateNoop.DRAFT
    if is_bot_author(pr.author_login, pr.author_type):
        return AutoGateNoop.BOT_AUTHOR
    if pr.base_ref not in gate_bases:
        return AutoGateNoop.STACKED
    if has_gate_hold(pr.body):
        return AutoGateNoop.HOLD
    return resolve_thread_link(pr.body)


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
    token = parse_next_token(tail_body)
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
    "THREAD_ID_RE",
    "THREAD_LINE_RE",
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
