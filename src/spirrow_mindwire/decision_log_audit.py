"""U4b: compare the decisions log against the decision lines in a thread's message bodies.

Spec: T-tier-c-admission-gate. U4b scope msg-5655, shared allowlist msg-5657; the comparison as
endorsed by Einstein msg-6257: imports and grammar msg-6244, both directions msg-6246, one role
resolver msg-6248, ``extra`` classification msg-6250 / msg-6254 / msg-6256, counts and no verdict
msg-6252.

What it compares, per thread:

* ``expected`` — a :class:`~collections.Counter` of :data:`DecisionKey` built from every message
  whose author's **current** role (:func:`~spirrow_mindwire.conductor.core.roster_role`, the
  conductor's own resolver) is in :data:`DECISION_LOG_AUTHOR_ROLES`, with
  :func:`scan_decision_lines` (the conductor's grammar) and :func:`decision_key`.
* ``logged`` — :func:`logged_decision_keys`, as merged in #418.
* ``missing = expected - logged`` — a decision line no log row matches. Each row carries the
  thread's status, so catch-up lag on an active thread can be told from an unexplained gap on a
  closed one (msg-6252). U4b cannot tell a never-run backfill from a promotion on a closed thread,
  and says so instead of guessing.
* ``extra = logged - expected``, split by Counter arithmetic (msg-6256) so a count is never lost:

  - ``roster_changed = extra & demoted_body``, where ``demoted_body`` is the unfiltered keys of
    messages whose author's current role is **not** allowed (``None`` included). The conductor
    writes only for allowed roles, so such a row was written before the role changed;
  - ``unmatched_log_row = extra - roster_changed`` — everything else: surplus copies, a ``msg_id``
    not in the thread, a key no body produces.

* ``malformed`` (allowed-role authors only, as the conductor counts it) and ``unattributed_author``
  (a role-less author whose body has decision lines) are reported on their own.

There is no pass/fail verdict (msg-6252): §3 asks only for numbers and nothing consumes a verdict.
"Clean" is a description — every count is zero. The log's own ``author_role`` column is never read
(msg-6254); the key does not carry it.

Reader: this module only returns data. The scan script prints/writes it; it posts nowhere.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .conductor.core import roster_role
from .tier_c_decisions_log import (
    DECISION_LOG_AUTHOR_ROLES,
    DecisionKey,
    decision_key,
    decision_log_entries,
    scan_decision_lines,
)
from .value_objects import Role

# Only used to build keys through the same helper the conductor uses; never written anywhere.
_KEY_ROLE: Role = next(iter(sorted(DECISION_LOG_AUTHOR_ROLES, key=lambda r: r.value)))
_KEY_NOW: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ThreadAudit:
    """The U4b counts for one thread. No verdict: see the module docstring."""

    thread: str
    status: str
    expected: Counter[DecisionKey]
    logged: Counter[DecisionKey]
    missing: Counter[DecisionKey]
    roster_changed: Counter[DecisionKey]
    unmatched_log_row: Counter[DecisionKey]
    malformed: Counter[str] = field(default_factory=Counter)
    unattributed_author: Counter[str] = field(default_factory=Counter)

    @property
    def clean(self) -> bool:
        """Every count is zero. A description, not a verdict (msg-6252)."""
        return not (
            self.missing
            or self.roster_changed
            or self.unmatched_log_row
            or self.malformed
            or self.unattributed_author
        )

    def counts(self) -> dict[str, int]:
        return {
            "expected": self.expected.total(),
            "logged": self.logged.total(),
            "missing": self.missing.total(),
            "roster_changed": self.roster_changed.total(),
            "unmatched_log_row": self.unmatched_log_row.total(),
            "malformed": self.malformed.total(),
            "unattributed_author": self.unattributed_author.total(),
        }

    def to_json(self) -> dict[str, Any]:
        """Counts plus the ``msg_id`` of each non-zero row; missing rows carry the thread status."""

        def rows(c: Counter[DecisionKey], **extra: str) -> list[dict[str, Any]]:
            return [
                {"msg_id": k[0], "kind": k[1], "what": k[2], "reason": k[3], "count": n, **extra}
                for k, n in sorted(c.items())
            ]

        return {
            "thread": self.thread,
            "status": self.status,
            "counts": self.counts(),
            "missing": rows(self.missing, thread_status=self.status),
            "roster_changed": rows(self.roster_changed),
            "unmatched_log_row": rows(self.unmatched_log_row),
            "malformed": dict(sorted(self.malformed.items())),
            "unattributed_author": dict(sorted(self.unattributed_author.items())),
        }


def _body_keys(msg_id: str, body: str) -> tuple[Counter[DecisionKey], int, bool]:
    """``(keys, malformed_count, has_lines)`` for one body, under the conductor's grammar."""
    scan = scan_decision_lines(body)
    entries = decision_log_entries(scan, author="", author_role=_KEY_ROLE, now=_KEY_NOW)
    return Counter(decision_key(msg_id, e) for e in entries), len(scan.malformed), not scan.empty


def audit_thread(
    *,
    thread: str,
    status: str,
    messages: Iterable[Mapping[str, Any]],
    logged: Counter[DecisionKey],
    roster: Mapping[str, Role],
) -> ThreadAudit:
    """Compare ``logged`` (from :func:`logged_decision_keys`) with ``messages`` of ``thread``.

    ``messages`` are chatroom message dicts (``msg_id`` / ``author`` / ``content``), as
    ``chatroom_get_thread`` returns them. ``logged`` is not modified.
    """
    expected: Counter[DecisionKey] = Counter()
    demoted_body: Counter[DecisionKey] = Counter()
    malformed: Counter[str] = Counter()
    unattributed: Counter[str] = Counter()
    for msg in messages:
        msg_id = str(msg.get("msg_id", ""))
        author = str(msg.get("author", ""))
        keys, n_malformed, has_lines = _body_keys(msg_id, str(msg.get("content", "")))
        if not has_lines:
            continue
        role = roster_role(roster, author)
        if role in DECISION_LOG_AUTHOR_ROLES:
            expected.update(keys)
            if n_malformed:
                malformed[msg_id] += n_malformed
            continue
        demoted_body.update(keys)
        if role is None:
            unattributed[msg_id] += 1
    logged_copy = Counter(logged)
    missing = expected - logged_copy
    extra = logged_copy - expected
    roster_changed = extra & demoted_body
    unmatched = extra - roster_changed
    return ThreadAudit(
        thread=thread,
        status=status,
        expected=expected,
        logged=logged_copy,
        missing=missing,
        roster_changed=roster_changed,
        unmatched_log_row=unmatched,
        malformed=malformed,
        unattributed_author=unattributed,
    )


__all__ = ["ThreadAudit", "audit_thread"]
