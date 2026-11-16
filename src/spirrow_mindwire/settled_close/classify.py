"""Sort one open thread's head into the class magickit would answer for it (pure, no I/O).

T-done-disposition-auto-close, boundary 6 (Bohr msg-8455 §2 6-1 / 6-2). **magickit is the
authority** on whether a thread may be closed under carve-out (4) ``settled_disposition``
(``magickit/mcp/settled_disposition.py`` + ``chatroom._enforce_close_policies``). Everything
here is a *prediction* of that answer, made on the same structured fields, in the same order, and
named after magickit's own ``details.reason`` codes, so that a dry-run table can be read against
what an enabled run would get back. Where the two disagree, magickit wins: an enabled close that
the client predicted wrongly is refused by the server and reported as such, never widened.

Order of the checks (magickit's, so the first failing check is the one it would name):

1. a PR-gate ledger thread (owner ``orchestrator`` + tag ``pr-review``) belongs to the
   ``pr_review_sweep`` phase 2, never to this one;
2. the head must store ``disposition.kind == "done"``. A head with no stored disposition is a
   legacy head (posted before the gateway sent one): with ``include_legacy`` it is sorted into
   ``legacy-done`` / ``legacy-blocked`` / ``unclassified`` by the same resolver the conductor
   uses (msg-8172 §4); the server refuses every one of them (``not_done``);
3. the body must resolve to DONE through the conductor's resolver as well (msg-8177 §B: the
   resolver is the necessary condition; the stored field alone is not trusted to mean the body
   says so). A disagreement is ``not-eligible`` / ``body-disagrees``;
4. ``next_participant`` must be null (``names_successor``);
5. a ``gate:naysayer`` thread is ``gate-pending``. magickit runs the gate before the carve-out,
   and the proposer's done is always newer than the naysayer review, so the gate answers
   ``NaysayerReviewStaleError`` (msg-8270 §3, msg-8328 §1);
6. a closer that owns the thread needs no carve-out (``owner_is_author``);
7. only a head posted with the verified role ``proposer`` settles a thread (msg-8185 Obj-5-1);
   any other is ``not-eligible`` with ``<role>-done`` or ``role-unknown`` (msg-8185);
8. the head must be at least ``grace`` old (``grace_pending`` -> ``deferred``);
9. the owner must be a registered identity that is not the human (``owner_missing`` /
   ``owner_human`` / ``owner_unresolved`` / ``owner_lookup_unavailable`` -> ``owner-pending``).

What this cannot predict and so does not try to: whether the closer is configured on the
magickit side (``closer_not_configured``, which is every close today, since
``settled_close.closers`` ships empty) and whether it is registered (``closer_unregistered``).
The report says so next to ``would_close``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from ..conductor.disposition import stop_line_of
from ..conductor.handoff import StopStatus, posted_at_of
from ..identity.normalize import normalize_identity_key
from ..value_objects import Role

#: Must equal magickit's ``SETTLED_DISPOSITION_ROLE`` (and Conclair's). A drift is fail-closed:
#: the server refuses (``role_not_eligible``), it never closes more.
SETTLED_DISPOSITION_ROLE = "proposer"
#: magickit ``pr_gate_ledger.PR_GATE_THREAD_OWNER`` / ``PR_GATE_THREAD_TAG``.
PR_GATE_THREAD_OWNER = "orchestrator"
PR_GATE_THREAD_TAG = "pr-review"
#: magickit's default naysayer gate tag.
GATE_TAG = "gate:naysayer"
#: magickit ``HUMAN_IDENTITY_NAMES``.
HUMAN_IDENTITY_NAMES = frozenset({"human"})
HUMAN_ROLE = "human"


class SettledClass(StrEnum):
    """One class per open thread. The values are what the report and the table print."""

    WOULD_CLOSE = "would_close"
    DEFERRED = "deferred"
    GATE_PENDING = "gate-pending"
    OWNER_PENDING = "owner-pending"
    OWNER_CAN_CLOSE = "owner-can-close"
    NOT_ELIGIBLE = "not-eligible"
    LEGACY_DONE = "legacy-done"
    LEGACY_BLOCKED = "legacy-blocked"
    UNCLASSIFIED = "unclassified"
    #: The head is not a done at all (a handoff, a wait, a human ask). Counted, not listed.
    NOT_SETTLED = "not-settled"
    #: A PR-gate ledger: phase 2 of the PR-review sweep owns it. Counted, not listed.
    PR_GATE_LEDGER = "pr-gate-ledger"
    UNREADABLE = "unreadable"
    # Outcomes an enabled tick adds after it called close (read back, never inferred).
    CLOSED = "closed"
    HEAD_MOVED = "head-moved"
    REFUSED = "refused"


#: Classes the table lists thread by thread. The rest are only counted.
LISTED_CLASSES = (
    SettledClass.WOULD_CLOSE,
    SettledClass.DEFERRED,
    SettledClass.GATE_PENDING,
    SettledClass.OWNER_PENDING,
    SettledClass.OWNER_CAN_CLOSE,
    SettledClass.NOT_ELIGIBLE,
    SettledClass.LEGACY_DONE,
    SettledClass.LEGACY_BLOCKED,
    SettledClass.UNCLASSIFIED,
    SettledClass.UNREADABLE,
    SettledClass.CLOSED,
    SettledClass.HEAD_MOVED,
    SettledClass.REFUSED,
)


class OwnerKind(StrEnum):
    """What the identity registry says about a thread's owner (magickit's step 6)."""

    AGENT = "agent"  # registered, without the human role
    HUMAN = "human"  # the human by name, or registered with the human role
    UNRESOLVED = "unresolved"  # not registered: cannot be shown not to be the human
    UNAVAILABLE = "unavailable"  # the lookup did not answer
    MISSING = "missing"  # the thread has no owner


_OWNER_REASON = {
    OwnerKind.HUMAN: "owner_human",
    OwnerKind.UNRESOLVED: "owner_unresolved",
    OwnerKind.UNAVAILABLE: "owner_lookup_unavailable",
    OwnerKind.MISSING: "owner_missing",
}


def is_human_name(owner: str) -> bool:
    return owner in HUMAN_IDENTITY_NAMES or normalize_identity_key(owner) in HUMAN_IDENTITY_NAMES


@dataclass(frozen=True)
class ThreadHead:
    """One open thread and its newest message, as read this run."""

    project: str
    thread_id: str
    owner: str
    tags: tuple[str, ...]
    #: The newest message, as ``chatroom_get_thread`` returned it.
    head: Mapping[str, Any]

    @property
    def head_msg_id(self) -> str:
        value = self.head.get("msg_id")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Verdict:
    cls: SettledClass
    #: magickit's ``details.reason`` (or ``error_type``) this prediction stands for, or the
    #: sub-kind of a class (``human-done``, ``body-disagrees``, ...). Empty when none applies.
    reason: str = ""
    #: ``deferred`` only: when the grace runs out.
    due_at: datetime | None = None


def parse_timestamp(raw: Any) -> datetime | None:
    """A timezone-aware timestamp, else ``None`` (magickit's ``parse_timestamp``)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _legacy(head: ThreadHead, roster: Mapping[str, Role], include_legacy: bool) -> Verdict:
    body = head.head.get("content")
    nxt = head.head.get("next_participant")
    stop = stop_line_of(
        body if isinstance(body, str) else "",
        roster,
        next_participant=nxt if isinstance(nxt, str) and nxt else None,
        posted_at=posted_at_of(head.head),
    )
    if not include_legacy or stop is None:
        return Verdict(SettledClass.NOT_SETTLED, "legacy" if stop is not None else "")
    if stop.status is StopStatus.DONE:
        return Verdict(SettledClass.LEGACY_DONE, "not_done")
    if stop.status is StopStatus.BLOCKED_ON:
        return Verdict(SettledClass.LEGACY_BLOCKED, "not_done")
    return Verdict(SettledClass.UNCLASSIFIED, "not_done")


def classify(
    head: ThreadHead,
    *,
    closer: str,
    now: datetime,
    grace: timedelta,
    roster: Mapping[str, Role],
    owner_kind: OwnerKind,
    include_legacy: bool = False,
) -> Verdict:
    """The class magickit would answer for ``head`` (see the module docstring for the order)."""
    if not head.head_msg_id:
        return Verdict(SettledClass.UNREADABLE, "head_unknown")
    if head.owner == PR_GATE_THREAD_OWNER and PR_GATE_THREAD_TAG in head.tags:
        return Verdict(SettledClass.PR_GATE_LEDGER, "pr_gate_ledger")

    disposition = head.head.get("disposition")
    if not isinstance(disposition, dict):
        return _legacy(head, roster, include_legacy)
    if disposition.get("kind") != "done":
        return Verdict(SettledClass.NOT_SETTLED, "not_done")

    body = head.head.get("content")
    stop = stop_line_of(
        body if isinstance(body, str) else "",
        roster,
        posted_at=posted_at_of(head.head),
    )
    if stop is None or stop.status is not StopStatus.DONE:
        return Verdict(SettledClass.NOT_ELIGIBLE, "body-disagrees")
    if head.head.get("next_participant") is not None:
        return Verdict(SettledClass.NOT_ELIGIBLE, "names_successor")
    if GATE_TAG in head.tags:
        return Verdict(SettledClass.GATE_PENDING, "NaysayerReviewStaleError")
    if head.owner and head.owner == closer:
        return Verdict(SettledClass.OWNER_CAN_CLOSE, "owner_is_author")

    role = head.head.get("role")
    if role != SETTLED_DISPOSITION_ROLE:
        kind = f"{role}-done" if isinstance(role, str) and role else "role-unknown"
        return Verdict(SettledClass.NOT_ELIGIBLE, kind)

    posted = parse_timestamp(head.head.get("timestamp"))
    if posted is None:
        return Verdict(SettledClass.NOT_ELIGIBLE, "timestamp_unreadable")
    if now - posted < grace:
        return Verdict(SettledClass.DEFERRED, "grace_pending", due_at=posted + grace)

    if owner_kind is not OwnerKind.AGENT:
        return Verdict(SettledClass.OWNER_PENDING, _OWNER_REASON[owner_kind])
    return Verdict(SettledClass.WOULD_CLOSE)


#: How an enabled close's refusal maps back onto a class. Keyed by magickit's
#: ``details.reason`` first, then by ``error_type``. Anything else is ``refused``, verbatim.
REFUSAL_REASON_CLASS: Mapping[str, SettledClass] = {
    "head_moved": SettledClass.HEAD_MOVED,
    "grace_pending": SettledClass.DEFERRED,
    "owner_human": SettledClass.OWNER_PENDING,
    "owner_unresolved": SettledClass.OWNER_PENDING,
    "owner_missing": SettledClass.OWNER_PENDING,
    "owner_lookup_unavailable": SettledClass.OWNER_PENDING,
    "owner_is_author": SettledClass.OWNER_CAN_CLOSE,
    "gate_disabled_on_gated_thread": SettledClass.GATE_PENDING,
    "not_done": SettledClass.NOT_ELIGIBLE,
    "names_successor": SettledClass.NOT_ELIGIBLE,
    "role_not_eligible": SettledClass.NOT_ELIGIBLE,
    "timestamp_unreadable": SettledClass.NOT_ELIGIBLE,
    "pr_gate_ledger": SettledClass.PR_GATE_LEDGER,
}
REFUSAL_ERROR_TYPE_CLASS: Mapping[str, SettledClass] = {
    # msg-8328 §1: the gate runs first and a proposer's done is newer than any review.
    "NaysayerReviewStaleError": SettledClass.GATE_PENDING,
    "NaysayerReviewRequiredError": SettledClass.GATE_PENDING,
    "NaysayerChangesRequestedError": SettledClass.GATE_PENDING,
}


def refusal_class(error_type: str | None, details_reason: str | None) -> SettledClass:
    if details_reason and details_reason in REFUSAL_REASON_CLASS:
        return REFUSAL_REASON_CLASS[details_reason]
    if error_type and error_type in REFUSAL_ERROR_TYPE_CLASS:
        return REFUSAL_ERROR_TYPE_CLASS[error_type]
    return SettledClass.REFUSED


__all__ = [
    "GATE_TAG",
    "LISTED_CLASSES",
    "REFUSAL_ERROR_TYPE_CLASS",
    "REFUSAL_REASON_CLASS",
    "SETTLED_DISPOSITION_ROLE",
    "OwnerKind",
    "SettledClass",
    "ThreadHead",
    "Verdict",
    "classify",
    "is_human_name",
    "parse_timestamp",
    "refusal_class",
]
