"""Summon: one thread the whole loop is called to (T-human-call-priority-interrupt, PR-2).

Design: Bohr msg-6743 (v1) as revised through msg-6755 (v1.6), msg-6800 (v1.7) and msg-6802
(v1.8); approved by Einstein msg-6756 / msg-6803. Takahito's requirement (msg-6741 / msg-6742):
a person's thread is handled first, and it stops the other work of the same project — (b).

A Summon is NOT a HOLD. HOLD is per project, set by a person, latched until cleared, and stops
everything; a Summon is per thread, cleared with the thread's resolve, and stops everything
**except** that thread. Precedence is HOLD → quarantine → Summon: a Summon bypasses neither.

This module is the conductor's half and the Python half of two rules the sweep (PowerShell,
``deploy/lib/Summon.ps1``) implements as well. Both halves are pinned to the same JSON fixtures
under ``tests/data/summon/`` so they cannot drift:

* :func:`parse_active_summon` — the ``active_summon`` contract of ``loop_control_get`` (v1.7).
  Absent or ``null`` = no Summon. A well-formed ``{thread_id, since, by}`` = a Summon. Anything
  else = malformed, and malformed fails safe to HOLD (v1, agreed in msg-6744 Q2).
* :func:`summon_yield_decision` — whether a thread yields to an active Summon (v1.1 指摘 2): a
  thread in a Summon's scope yields unless it is the Summon itself, and a summoned thread yields
  to another in-scope Summon only when that one comes first in the total order
  ``(since, thread_id)``. The oldest Summon yields to nobody, so there is no deadlock.

The resume notice (v1.4 single lifecycle) is the third piece: the sweep ALWAYS passes
``--resume-notice-summons`` / ``--resume-notice-main-range`` to a thread that has a
``summon_yield`` record, and the conductor reports ``resume_notice=delivered`` on its stop line
once the first role turn of the run was actually sent — whether or not the range was empty. That
first turn always carries a notice text; an empty range has its own wording (PR-3). The
conductor never reads or writes ``evaluated.json``; the sweep is that file's only writer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

#: The ``loop_control_get`` response key (v1, msg-6743 §1).
ACTIVE_SUMMON_KEY = "active_summon"

#: Placeholder for a SHA the sweep could not read. A range with an unknown end is never "empty",
#: so the notice is shown — the safe side of not knowing whether ``main`` moved.
UNKNOWN_SHA = "unknown"

# ISO 8601 with an explicit offset, at most 6 fractional digits. The PowerShell half uses the
# same pattern (``deploy/lib/Summon.ps1``): restricting the syntax to what both parsers read
# identically is what keeps a timestamp from being valid on one side and malformed on the other.
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


class SummonReadKind(StrEnum):
    """What one ``active_summon`` value said."""

    NONE = "none"
    VALID = "valid"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ActiveSummon:
    """A well-formed ``active_summon`` (v1.7 contract: ``{thread_id, since, by}``)."""

    thread_id: str
    since: datetime  # UTC
    by: str


@dataclass(frozen=True)
class SummonRead:
    """:func:`parse_active_summon`'s answer. ``summon`` is set only when ``kind`` is VALID."""

    kind: SummonReadKind
    summon: ActiveSummon | None = None


_NONE = SummonRead(SummonReadKind.NONE)
_MALFORMED = SummonRead(SummonReadKind.MALFORMED)


def parse_since(value: Any) -> datetime | None:
    """``since`` as a UTC datetime, or ``None`` when it is not in the contract's format."""
    if not isinstance(value, str) or not _SINCE_RE.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:  # unreachable through the regex; kept so the type is certain
        return None
    return parsed.astimezone(UTC)


def parse_active_summon_value(value: Any) -> SummonRead:
    """Classify the VALUE of ``active_summon`` (``None`` here means absent or ``null``)."""
    if value is None:
        return _NONE
    if not isinstance(value, dict):
        return _MALFORMED
    thread_id = value.get("thread_id")
    by = value.get("by")
    since = parse_since(value.get("since"))
    if not isinstance(thread_id, str) or not thread_id.strip():
        return _MALFORMED
    if not isinstance(by, str) or not by.strip():
        return _MALFORMED
    if since is None:
        return _MALFORMED
    return SummonRead(SummonReadKind.VALID, ActiveSummon(thread_id=thread_id, since=since, by=by))


def parse_active_summon(payload: Any) -> SummonRead:
    """Classify the ``active_summon`` of a whole ``loop_control_get`` payload.

    Missing key and ``null`` are the same fact (v1.7: a server may serialise ``None`` as
    ``null``) — no Summon, which is also what every magickit that predates the key says. A
    payload that is not a mapping is not this function's to judge: :func:`.control.
    parse_control_payload` already fails it safe to HOLD, so it is reported as NONE here.
    """
    if not isinstance(payload, dict):
        return _NONE
    return parse_active_summon_value(payload.get(ACTIVE_SUMMON_KEY))


@dataclass(frozen=True)
class _Scoped:
    project: str
    summon: ActiveSummon


def _order_key(project: str, summon: ActiveSummon) -> tuple[datetime, str, str]:
    # (since, thread_id) is the design's total order (v1.1); ``project`` is only the last
    # tie-break, for two Summons in different projects with the same since AND the same id.
    return (summon.since, summon.thread_id, project)


def summon_yield_decision(
    *,
    project: str,
    thread_id: str,
    own_summon: ActiveSummon | None,
    owner_project: str | None,
    owner_summon: ActiveSummon | None,
) -> str | None:
    """The thread id of the Summon this thread must yield to, or ``None`` to go on.

    ``own_summon`` is the active Summon of the thread's own project; ``owner_summon`` that of the
    project owning the repository the thread writes to (``owner_map``), considered only when that
    project differs from the thread's own. Those two are the whole scope (v1: same project, or the
    Summon's project owns the resource). A Summon never yields to itself. When several apply, the
    first in the total order is named, so both implementations name the same one.
    """
    in_scope: list[_Scoped] = []
    if own_summon is not None:
        in_scope.append(_Scoped(project, own_summon))
    if owner_summon is not None and owner_project and owner_project != project:
        in_scope.append(_Scoped(owner_project, owner_summon))
    me: _Scoped | None = None
    for s in in_scope:
        if s.project == project and s.summon.thread_id == thread_id:
            me = s
    blockers = [s for s in in_scope if s is not me]
    if me is not None:
        mine = _order_key(me.project, me.summon)
        blockers = [s for s in blockers if _order_key(s.project, s.summon) < mine]
    if not blockers:
        return None
    first = min(blockers, key=lambda s: _order_key(s.project, s.summon))
    return first.summon.thread_id


# --- resume notice (v1.4 single lifecycle) -------------------------------------------------------


@dataclass(frozen=True)
class ResumeNotice:
    """What the sweep told this run about the Summon(s) it had yielded to."""

    summons: tuple[str, ...]
    main_from: str
    main_to: str

    @property
    def empty_range(self) -> bool:
        """``main`` did not move while the thread was yielded (``a..a`` with a known ``a``)."""
        return self.main_from == self.main_to and self.main_from != UNKNOWN_SHA


def parse_resume_notice(summons: str | None, main_range: str | None) -> ResumeNotice | None:
    """Parse ``--resume-notice-summons`` / ``--resume-notice-main-range``.

    Both absent = this run was not yielded, ``None``. If the sweep passed either flag the run WAS
    yielded and must report delivery, so parsing is lenient: a missing or unparseable range reads
    as ``unknown..unknown`` (shown, never "empty"), and a missing summon list reads as empty.
    """
    if summons is None and main_range is None:
        return None
    names = tuple(s.strip() for s in (summons or "").split(",") if s.strip())
    lo, sep, hi = (main_range or "").strip().partition("..")
    main_from = lo.strip() if sep and lo.strip() else UNKNOWN_SHA
    main_to = hi.strip() if sep and hi.strip() else UNKNOWN_SHA
    return ResumeNotice(summons=names, main_from=main_from, main_to=main_to)


def render_resume_notice(notice: ResumeNotice) -> str:
    """The prompt paragraph. Always a text (PR-3, Bohr msg-6965 item 3).

    v1.4 keeps the notice on every run that has a ``summon_yield`` record. Until PR-3 an empty range
    rendered nothing; it now gets its own wording ("main did not change"), so the thread is still
    told it was stopped and why. The moved wording keeps v1.1 指摘 1: it does not claim the Summon
    caused the change, because ``main`` may have moved for other reasons too. A range with an
    unknown end is never empty (:attr:`ResumeNotice.empty_range`), so it takes the moved wording:
    that is the safe side of not knowing. ASCII parentheses: the repo's lint rejects full-width ones
    in literals.
    """
    who = ", ".join(notice.summons) if notice.summons else UNKNOWN_SHA
    head = f"[Summon 再開の注記] このスレッドは Summon {who} に譲って停止していた。"
    if notice.empty_range:
        return head + f"停止中に main は変わっていない ({notice.main_from})。取り込みは不要。"
    return head + (
        f"停止中に main が {notice.main_from}..{notice.main_to} 進んだ"
        f" (Summon 以外の変更を含みうる)。続ける前に取り込みを確認すること。"
    )


__all__ = [
    "ACTIVE_SUMMON_KEY",
    "UNKNOWN_SHA",
    "ActiveSummon",
    "ResumeNotice",
    "SummonRead",
    "SummonReadKind",
    "parse_active_summon",
    "parse_active_summon_value",
    "parse_resume_notice",
    "parse_since",
    "render_resume_notice",
    "summon_yield_decision",
]
