"""The retry notice: what a re-launched turn is told about the failed launch before it.

T-retry-once-before-quarantine (Bohr msg-5434 D-4, narrowed by msg-5441 and msg-5449; Einstein
approved in msg-5452). The sweep no longer quarantines a thread the first time it fails.
Instead it re-launches the thread once, on the next tick, through the ordinary decide →
commit-launch → spawn path. On that launch it passes ``--retry-of <error_code>@<first_failure_at>``.
The conductor parses that value here. On the run's first round only, and only when the
dispatched role is in :data:`RETRY_NOTICE_ROLES`, it attaches one fixed paragraph to the turn's
prompt (:attr:`~spirrow_mindwire.value_objects.ChatroomEvent.retry_notice`).

Why the notice exists: a failed launch can still have done real work. An implementer turn
that dies in ``turn_timeout`` / ``delivery_failed`` never reaches ``on_reply``, so the head does
not move, but its tools may already have committed, pushed, opened a PR or commented — and none
of those is idempotent. The re-launch fires the same prompt at the same head, so the turn has
to be told to look before it acts.

Why only some roles (msg-5441, closing Einstein's blocking finding in msg-5439): a role that
changes nothing outside the thread before its reply is posted has nothing to check. Telling it to
inspect a working tree or PRs it never touched invites hallucinated checks and pointless tool
calls. A softened, role-neutral wording would invite the same thing ("you must check
something"), so the notice is gated rather than diluted.
"""

from __future__ import annotations

from dataclasses import dataclass

# The roles whose turn can change external state with tools BEFORE its reply is posted
# (msg-5449 D-4 addendum). List ONLY such roles here — this frozenset is the single place that
# assumption lives, so it is a constant with a name rather than a string compare at the use site.
#
# Evidence for the current contents (this thread, msg-5434 D-4):
#   * implementer — ``turn_timeout`` / ``delivery_failed`` raise inside ``_drain_reply`` before
#     ``on_reply``; by then commit / push / ``gh pr create`` / PR comments may have run.
#   * naysayer — ``shutdown_failed`` raises in the ``finally`` after ``body_success=True`` but
#     before ``on_reply``; the reply is unposted and the turn's only side effect was reading.
#     A second launch at the same head just reviews again.
#   * proposer — its output is the design post itself; unposted means nothing happened.
#
# Adding a role: when a change gives another role (e.g. integrator) a tool side effect before its
# reply, add that role's value here in the same PR, and pin it with a test that the role's
# prompt now carries the notice.
RETRY_NOTICE_ROLES: frozenset[str] = frozenset({"implementer"})

# Placeholder for the timestamp half of a ``--retry-of`` value that carried none.
UNKNOWN_FIRST_FAILURE_AT = "unknown"


@dataclass(frozen=True)
class RetryOf:
    """The failed launch a retry stands in for, as the sweep reported it."""

    error_code: str
    first_failure_at: str


def parse_retry_of(value: str | None) -> RetryOf | None:
    """Parse the sweep's ``--retry-of <error_code>@<first_failure_at>`` value.

    ``None`` or blank means this launch is not a retry. Parsing is lenient on purpose. If the
    sweep passes the flag at all, the launch IS a retry, and the notice is the safe side of a
    malformed value. So a value without ``@`` keeps its whole text as the error code, and an
    empty half is replaced by a placeholder instead of being dropped. The split is at the LAST
    ``@`` because an ISO 8601 timestamp never contains one.
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    code, sep, at = text.rpartition("@")
    if not sep:
        code, at = text, ""
    return RetryOf(
        error_code=code.strip() or "unknown",
        first_failure_at=at.strip() or UNKNOWN_FIRST_FAILURE_AT,
    )


def render_retry_notice(retry_of: RetryOf) -> str:
    """The fixed notice paragraph (msg-5434 D-4 item 1).

    The wording is the design's, with ASCII parentheses (the repo's lint rejects full-width ones
    in string literals) and the failure time added so the turn can match it against its own logs.
    """
    return (
        f"[再試行の注記] 前回この同じメッセージへの起動は {retry_of.error_code} で失敗した"
        f" ({retry_of.first_failure_at})。作業ツリー・ブランチ・既存の PR の現状を先に確認し、"
        f"既に済んだ操作 (push、PR 作成、コメント) は繰り返すな。"
    )


def retry_notice_for(retry_of: RetryOf | None, role_value: str, *, rounds: int) -> str | None:
    """The notice for one dispatch, or ``None`` when this dispatch gets none.

    The notice is attached only when all three hold: the run is a retry, this is the run's first
    round (later rounds dispatch on heads this run itself moved), and the dispatched role is in
    :data:`RETRY_NOTICE_ROLES`. ``role_value`` is the role the session was spawned for, which the
    dispatcher contract makes equal to the adapter's ``own_role``.
    """
    if retry_of is None or rounds != 0 or role_value not in RETRY_NOTICE_ROLES:
        return None
    return render_retry_notice(retry_of)


__all__ = [
    "RETRY_NOTICE_ROLES",
    "UNKNOWN_FIRST_FAILURE_AT",
    "RetryOf",
    "parse_retry_of",
    "render_retry_notice",
    "retry_notice_for",
]
