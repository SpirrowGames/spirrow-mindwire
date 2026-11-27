"""The pure half of the review-request trigger: a member asks the gate's login for a review.

Design: chatroom ``spirrow-mindwire/T-gate-fires-on-review-request`` — Fermi msg-10104 (D1-D8),
Bohr msg-10106 (v1), msg-10108 (v1.1: one dedicated receiving thread per PR) and msg-10110 (v1.2:
a transient read failure is noticed on its first tick), which Einstein approved with no objections.

A member who cannot write to the chatroom asks for the gate on GitHub alone, by adding the gate's
review login to the PR's reviewers (or pressing re-request). The listing row's
``requested_reviewers`` is the signal; the newest ``review_requested`` timeline event naming that
login is the request, and its id is what "one request fires once" (D5) is keyed by.

Like 1c, this never fires the gate itself: it writes ``NEXT: pr-review <ref>`` and the conductor
does the rest. Unlike 1c, the post always goes to the PR's own receiving thread
:func:`receiving_thread_id` (v1.1 §A), never to the thread a ``Thread:`` line names, so:

* a ``Thread:`` line that changes, or a design thread that ends, cannot move a request to a thread
  without its fire record (Obj-1-1);
* the receiving thread has no AI implementer, so no verdict on a member's PR can start a turn that
  pushes to it (Obj-1-2). The conductor settles such a relay instead of routing it to the human.

Evaluation order (v1 §2 as replaced by v1.1 §A), first match wins:

==  ============================================  =========================================
#   condition                                     result
==  ============================================  =========================================
M1  requester is a bot, or the review login       notice ``requester-bot``
M2  ``Gate: hold`` line                           notice ``hold``
M3  draft / base outside ``gate_bases`` /         not read: a person asked
    bot author
M4  receiving thread read; a marker for this      noop ``request-already-fired``
    ``(repo, pr, event)``
    the thread is resolved                        notice ``receiving-thread-closed``
M6  no slot left today / this tick                notice ``cap``
M7  the thread's tail is someone else's turn      wait (no notice)
M8  otherwise                                     fire
==  ============================================  =========================================
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from ...conductor.gate_records import RECEIVING_THREAD_PREFIX, is_receiving_thread
from ...conductor.handoff import PR_REVIEW_TOKEN
from ...github.client import GatePr, IssueCommentInfo, PrRef, ReviewInfo, ReviewRequestEvent
from ..decide import PR_EVENT_RELAY_AUTHOR, Post
from .decide import has_gate_hold, is_bot_author

#: The ``trigger`` field of the fire marker (v1 §4). 1c's own markers carry none.
TRIGGER_REVIEW_REQUEST = "review-request"
_FIRE_MARKER_RE = re.compile(r"^<!-- mindwire:auto-gate v1 (?P<payload>\{.*\}) -->$")
_NOTICE_MARKER_RE = re.compile(r"<!-- mindwire:gate-request-notice v1 (?P<payload>\{.*?\}) -->")
#: v1.1 §B: how long an unresolved PR project may stay silent once the request event is known.
PROJECT_WAIT = timedelta(hours=1)


class RequestNotice(StrEnum):
    """Why a seen request was not fired, written once to the PR (D6). Verbatim in the marker."""

    REQUESTER_BOT = "requester-bot"
    HOLD = "hold"
    RECEIVING_THREAD_CLOSED = "receiving-thread-closed"
    CAP = "cap"
    TIMELINE_TRANSIENT = "timeline-transient"
    REQUEST_EVENT_MISSING = "request-event-missing"
    PR_PROJECT_INCOMPLETE = "pr-project-incomplete"
    PR_PROJECT_AMBIGUOUS = "pr-project-ambiguous"


#: Noop: this request has fired already (M4). Nothing is written.
REQUEST_ALREADY_FIRED = "request-already-fired"

_NOTICE_TEXT: Mapping[str, str] = {
    RequestNotice.REQUESTER_BOT: (
        "bot (または gate 自身のアカウント) からのレビュー依頼なので、gate は起動しません。"
        "人が Reviewers 欄から依頼し直してください。"
    ),
    RequestNotice.HOLD: (
        "PR 本文に `Gate: hold` 行があるので、gate は起動しません。"
        "起動するには、その行を消してからレビューを再リクエストしてください。"
    ),
    RequestNotice.RECEIVING_THREAD_CLOSED: (
        "この PR の gate 受け皿スレッドが既に閉じているので、gate は起動しません。"
        "loop の担当者に連絡してください。"
    ),
    RequestNotice.CAP: (
        "gate の 1 日の起動上限に達したので、まだ起動していません。"
        "依頼はそのまま残しておけば、明日 (JST 0 時以降) 自動で拾われます。"
    ),
    RequestNotice.TIMELINE_TRANSIENT: (
        "GitHub の読み取りが一時的に失敗したため、まだ起動していません。"
        "自動で再試行し、起動できればそのまま gate が走ります。"
    ),
    RequestNotice.REQUEST_EVENT_MISSING: (
        "レビュー依頼の記録 (timeline の review_requested) が見つからないため、"
        "gate は起動しません。"
        "レビューを再リクエストしてください。"
    ),
    RequestNotice.PR_PROJECT_INCOMPLETE: (
        "この PR がどのプロジェクトに属するか確かめられないため、gate はまだ起動していません。"
        "loop の担当者に連絡してください。"
    ),
    RequestNotice.PR_PROJECT_AMBIGUOUS: (
        "このリポジトリが複数のプロジェクトに登録されているため、gate は起動しません。"
        "loop の担当者に連絡してください。"
    ),
}


def receiving_thread_id(pr: PrRef) -> str:
    """``T-gate-request-<repo>-<n>``, built the way the ledger id is (repo lower-cased).

    Not ``T-pr-review-``: that is the gate's ledger, closed by ``pr_review_sweep`` and the target
    of its own critique posts, so a receiving thread there would relay to itself (v1 §3).
    """
    return f"{RECEIVING_THREAD_PREFIX}{pr.repo.lower()}-{pr.number}"


def requests_gate(pr: GatePr, review_login: str) -> bool:
    """D1: the gate's own login is among the PR's requested (user) reviewers."""
    want = review_login.casefold()
    return bool(want) and any(login.casefold() == want for login in pr.requested_logins)


def latest_request(
    events: Iterable[ReviewRequestEvent], review_login: str
) -> ReviewRequestEvent | None:
    """The newest ``review_requested`` event naming ``review_login`` (timeline order), or None."""
    want = review_login.casefold()
    found: ReviewRequestEvent | None = None
    for event in events:
        if event.requested_login.casefold() == want:
            found = event
    return found


def timeline_failure_reason(*, transient: bool, code: str) -> str:
    """The notice reason for a timeline read that failed (v1.2 §B): ``timeline-<code>``."""
    return RequestNotice.TIMELINE_TRANSIENT.value if transient else f"timeline-{code}"


def request_prefilter(
    pr: GatePr, event: ReviewRequestEvent, review_login: str
) -> RequestNotice | None:
    """M1-M2; M3 (draft, stacked, bot author) is deliberately not read: a person asked."""
    if is_bot_author(event.actor_login, event.actor_type) or (
        event.actor_login.casefold() == review_login.casefold()
    ):
        return RequestNotice.REQUESTER_BOT
    if has_gate_hold(pr.body):
        return RequestNotice.HOLD
    return None


def project_wait_expired(event: ReviewRequestEvent, now: datetime) -> bool:
    """v1.1 §B: an unresolved PR project turns permanent an hour after the request event.

    Measured from the event so nothing is stored. An event without a time is taken as expired:
    the wait has no start, and staying silent forever is what D6 forbids.
    """
    return event.created_at is None or now - event.created_at >= PROJECT_WAIT


def render_request_marker(*, repo: str, pr: int, event: int, requester: str) -> str:
    """The 1c marker (v1) with three fields added; 1c's own reader still matches it."""
    payload = json.dumps(
        {
            "repo": repo,
            "pr": pr,
            "trigger": TRIGGER_REVIEW_REQUEST,
            "event": event,
            "requester": requester,
        },
        ensure_ascii=True,
    )
    return f"<!-- mindwire:auto-gate v1 {payload} -->"


def request_already_fired(
    messages: Iterable[Mapping[str, object]], *, repo: str, number: int, event: int
) -> bool:
    """M4: a relay-authored review-request marker with this ``(repo, pr, event)``."""
    for message in messages:
        if message.get("author") != PR_EVENT_RELAY_AUTHOR:
            continue
        for line in str(message.get("content", "")).splitlines():
            match = _FIRE_MARKER_RE.match(line.strip())
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
                and payload.get("trigger") == TRIGGER_REVIEW_REQUEST
                and payload.get("event") == event
            ):
                return True
    return False


def request_post(
    *, repo: str, number: int, event: ReviewRequestEvent, thread_refs: Sequence[str] = ()
) -> Post:
    """M8: the fire post. Also the receiving thread's ``propose_content`` when it is opened.

    ``thread_refs`` are the ids the PR body's ``Thread:`` lines name, copied for the reader only
    (v1.1 Obj-1-2): nothing is posted there.
    """
    slug = f"{repo}#{number}"
    lines = [
        f"PR event (review-request, {PR_EVENT_RELAY_AUTHOR}) — {slug}",
        "",
        f"{event.actor_login} が GitHub で gate のレビューを依頼したので、gate を撃つ。"
        " このスレッドには implementer がいないので、verdict の後は誰も起動されない。",
    ]
    if thread_refs:
        lines += ["", "参照 (PR 本文の Thread: 行) : " + ", ".join(thread_refs)]
    marker = render_request_marker(
        repo=repo, pr=number, event=event.event_id, requester=event.actor_login
    )
    return Post(body="\n".join(lines), next_line=f"{marker}\nNEXT: {PR_REVIEW_TOKEN} {slug}")


def render_notice_marker(*, event: int | None, reason: str) -> str:
    payload = json.dumps({"event": event, "reason": reason}, ensure_ascii=True)
    return f"<!-- mindwire:gate-request-notice v1 {payload} -->"


def render_notice(*, event: int | None, reason: str) -> str:
    """The PR comment for ``reason`` (D6): one or two lines, then its de-duplication marker."""
    text = _NOTICE_TEXT.get(reason) or (
        f"gate を起動できませんでした (理由: `{reason}`)。loop の担当者に連絡してください。"
    )
    return f"**naysayer gate**: {text}\n\n{render_notice_marker(event=event, reason=reason)}"


def notice_window_start(reviews: Iterable[ReviewInfo], review_login: str) -> datetime | None:
    """v1.2 §B: when ``review_login`` last submitted a review on the PR, or ``None`` (none yet).

    A submitted review clears the request on GitHub, so it closes one request's notices: a
    ``(null, reason)`` failure after it is a new incident and may be noticed again.
    """
    want = review_login.casefold()
    newest: datetime | None = None
    for review in reviews:
        if review.login.casefold() != want or not review.submitted_at:
            continue
        try:
            at = datetime.fromisoformat(review.submitted_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if newest is None or at > newest:
            newest = at
    return newest


def notice_already_posted(
    comments: Iterable[IssueCommentInfo],
    *,
    event: int | None,
    reason: str,
    review_login: str,
    since: datetime | None,
) -> bool:
    """Whether the review login already wrote this ``(event, reason)`` notice in the window.

    ``since`` is :func:`notice_window_start`; ``None`` means the whole PR (no review yet, or the
    review list could not be read — the direction that keeps a notice to one, v1.2 §B). A comment
    without a time counts as inside the window for the same reason. Only the review login's own
    comments count, so a pasted marker cannot silence a notice.
    """
    want = review_login.casefold()
    for comment in comments:
        if comment.author_login.casefold() != want:
            continue
        if since is not None and comment.created_at is not None and comment.created_at <= since:
            continue
        for match in _NOTICE_MARKER_RE.finditer(comment.body):
            try:
                payload = json.loads(match.group("payload"))
            except ValueError:
                continue
            if (
                isinstance(payload, dict)
                and payload.get("event") == event
                and payload.get("reason") == reason
            ):
                return True
    return False


__all__ = [
    "PROJECT_WAIT",
    "RECEIVING_THREAD_PREFIX",
    "REQUEST_ALREADY_FIRED",
    "TRIGGER_REVIEW_REQUEST",
    "RequestNotice",
    "is_receiving_thread",
    "latest_request",
    "notice_already_posted",
    "notice_window_start",
    "project_wait_expired",
    "receiving_thread_id",
    "render_notice",
    "render_notice_marker",
    "render_request_marker",
    "request_already_fired",
    "request_post",
    "request_prefilter",
    "requests_gate",
    "timeline_failure_reason",
]
