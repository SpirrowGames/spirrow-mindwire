# ruff: noqa: RUF001, RUF002 - posted Japanese text: fullwidth punctuation and wide lines on purpose
"""The operator report: its format, its single validator, and the ``run_id`` marker (D3, D2⁵ - D2⁷).

Format (D3). Every report the lane posts — the runner's, a conductor re-post, a quarantine — is::

    ## operator report（lane run <run_id>）
    対象: <head_msg_id> の OPERATOR-TASK: <task>
    <!-- operator-lane run_id=<run_id> kind=report|quarantine -->
    ### 測った事実
    ### 実行したこと
    ### 残っていること
    NEXT: <requester | human | operator>

:func:`validate_report` is the ONE format check (D2⁷). The runner calls it before it saves the
report and again before it posts; the conductor calls it before a re-post. A test pins that all
three call this function, so the format cannot drift between the three gates.

:func:`posted` is the ONE answer to "did a post of this run land?" (D2⁵). Since D2⁶ it never decides
completion — the lock's ``result`` does — it only guards a re-post or a quarantine post against
posting a second time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..conductor.handoff import (
    HUMAN_TOKEN,
    OPERATOR_TOKEN,
    HumanAsk,
    parse_next_token,
    resolve_handoff,
)

#: The three sections every report carries, in this order (D3).
SECTION_FACTS = "### 測った事実"
SECTION_DONE = "### 実行したこと"
SECTION_REMAINING = "### 残っていること"
SECTIONS = (SECTION_FACTS, SECTION_DONE, SECTION_REMAINING)

#: Prefix of the lane's own control lines. A head carrying one is never picked up (D4 / D5): it is
#: the lane's own needs-hands / quarantine / audit-hit stop, and picking it up would loop.
LANE_LINE_PREFIX = "OPERATOR-LANE:"

KIND_REPORT = "report"
KIND_QUARANTINE = "quarantine"

_MARKER_RE = re.compile(
    r"<!--\s*operator-lane\s+run_id=(?P<run_id>[A-Za-z0-9_-]+)\s+kind=(?P<kind>report|quarantine)\s*-->"
)
_LANE_LINE_RE = re.compile(rf"^\s*{re.escape(LANE_LINE_PREFIX)}", re.MULTILINE)


def marker(run_id: str, kind: str) -> str:
    """The run marker (D2⁵): ``<!-- operator-lane run_id=<run_id> kind=<kind> -->``."""
    if kind not in (KIND_REPORT, KIND_QUARANTINE):
        raise ValueError(f"unknown marker kind {kind!r}")
    return f"<!-- operator-lane run_id={run_id} kind={kind} -->"


def marker_run_ids(text: str) -> list[str]:
    """Every ``run_id`` a body's markers name, in order."""
    return [m.group("run_id") for m in _MARKER_RE.finditer(text)]


def has_lane_line(text: str) -> bool:
    """True when a body carries an ``OPERATOR-LANE:`` line (the lane never picks such a head up)."""
    return _LANE_LINE_RE.search(text) is not None


def header(run_id: str, head_msg_id: str, operator_task: str) -> str:
    return (
        f"## operator report（lane run {run_id}）\n"
        f"対象: {head_msg_id} の OPERATOR-TASK: {operator_task}"
    )


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def validate_report(text: str, *, run_id: str, requester: str) -> list[str]:
    """Return what is wrong with ``text`` as a lane report; an empty list means it is valid.

    The checks (D2⁷): the three sections are present in order; the marker of THIS ``run_id`` is
    present and sits above the final line; the final non-blank line is a ``NEXT:`` that names the
    requester, ``human`` or ``operator``. Two more follow from D4: a ``NEXT: operator`` must be the
    3-line form the conductor accepts, and must carry an ``OPERATOR-LANE:`` line — otherwise the
    lane would pick its own report up again.
    """
    problems: list[str] = []
    pos = -1
    for section in SECTIONS:
        idx = text.find(section, pos + 1)
        if idx < 0:
            problems.append(f"missing section {section!r}")
        else:
            pos = idx
    last = _last_nonblank_line(text)
    last_line_start = text.rstrip().rfind("\n") + 1
    ids = marker_run_ids(text)
    if run_id not in ids:
        problems.append(f"missing marker for run_id={run_id}")
    elif all(
        m.start() >= last_line_start
        for m in _MARKER_RE.finditer(text)
        if m.group("run_id") == run_id
    ):
        problems.append("marker is not above the final NEXT: line")
    if any(other != run_id for other in ids):
        problems.append(f"a marker names another run (expected run_id={run_id})")
    if not last.startswith("NEXT:"):
        problems.append("last line is not a NEXT: line")
        return problems
    token = parse_next_token(text)
    if token is None:
        problems.append("NEXT: line names no participant")
        return problems
    folded = token.casefold()
    if folded not in {requester.casefold(), HUMAN_TOKEN, OPERATOR_TOKEN}:
        problems.append(
            f"NEXT: {token} is neither the requester ({requester}), {HUMAN_TOKEN} nor "
            f"{OPERATOR_TOKEN}"
        )
    if folded == OPERATOR_TOKEN:
        if resolve_handoff(text, {}).human_ask is not HumanAsk.OPERATOR_WORK:
            problems.append("NEXT: operator is not in its 3-line form")
        if not has_lane_line(text):
            problems.append("NEXT: operator report carries no OPERATOR-LANE: line")
    return problems


def late_report_line(head_msg_id: str, n_after: int) -> str:
    """D2⁵: the first line of a report that lands after other posts on the task."""
    return f"{LANE_LINE_PREFIX} late-report（{head_msg_id} の後に {n_after} 件の投稿あり）"


def with_late_report(text: str, head_msg_id: str, n_after: int) -> str:
    """Prefix ``text`` with the late-report line when ``n_after`` > 0 (idempotent)."""
    if n_after <= 0 or text.startswith(f"{LANE_LINE_PREFIX} late-report"):
        return text
    return late_report_line(head_msg_id, n_after) + "\n\n" + text


def compose_report(
    session_text: str,
    *,
    run_id: str,
    head_msg_id: str,
    operator_task: str,
    audit_hits: Sequence[str] = (),
) -> str:
    """Wrap the session's output into a report: header, marker, then the session's body.

    On an audit hit (D2' (c)) the session's own final handoff is replaced: the report says what
    was found and stops on the operator form, so a person looks before anything moves on.
    """
    body = session_text.strip()
    parts = [header(run_id, head_msg_id, operator_task), marker(run_id, KIND_REPORT)]
    if audit_hits:
        parts.append(f"{LANE_LINE_PREFIX} audit-hit {', '.join(audit_hits)}")
        body = (
            _strip_final_handoff(body)
            + "\n\nOPERATOR-TASK: operator lane の事後監査に引っかかった操作を確認する（"
            + ", ".join(audit_hits)
            + "）\nTIER-C-CHECK: none\n"
            + f"NEXT: {OPERATOR_TOKEN}"
        )
    parts.append(body)
    return "\n".join(parts).rstrip() + "\n"


_TRAILER_KEYS = ("NEXT:", "STOP:", "ASK:", "TIER-C:", "TIER-C-CHECK:", "OPERATOR-TASK:")


def _strip_final_handoff(body: str) -> str:
    lines = body.rstrip().splitlines()
    while lines and (not lines[-1].strip() or lines[-1].strip().startswith(_TRAILER_KEYS)):
        lines.pop()
    return "\n".join(lines)


def render_quarantine(
    *,
    run_id: str,
    head_msg_id: str,
    operator_task: str,
    facts: Iterable[str],
) -> str:
    """The lane's own quarantine report (D5): no session output is trusted, a person takes over."""
    fact_lines = "\n".join(f"- {f}" for f in facts) or "- （記録なし）"
    return (
        f"{header(run_id, head_msg_id, operator_task)}\n"
        f"{marker(run_id, KIND_QUARANTINE)}\n"
        f"{LANE_LINE_PREFIX} needs-hands quarantined\n\n"
        f"{SECTION_FACTS}\n{fact_lines}\n\n"
        f"{SECTION_DONE}\n"
        "- lane はこの OPERATOR-TASK を自動では完了できませんでした。セッションが途中まで何かを"
        "実行した可能性があり、作業が冪等である保証もないため、再試行していません。\n\n"
        f"{SECTION_REMAINING}\n"
        "- 手動の operator が、上の事実とログを見て状態を確かめ、作業を引き取ってください。\n\n"
        f"OPERATOR-TASK: {head_msg_id} の OPERATOR-TASK を手で確認・実行する（lane run {run_id} は"
        "quarantine）\n"
        "TIER-C-CHECK: none\n"
        f"NEXT: {OPERATOR_TOKEN}\n"
    )


def _content(msg: Mapping[str, Any]) -> str:
    return str(msg.get("content") or msg.get("body") or "")


def messages_after(messages: Sequence[Mapping[str, Any]], head_msg_id: str) -> list[Any]:
    """The messages after ``head_msg_id``; every message when the head is not in the list."""
    for i, msg in enumerate(messages):
        if str(msg.get("msg_id") or "") == head_msg_id:
            return list(messages[i + 1 :])
    return list(messages)


def posted(
    messages: Sequence[Mapping[str, Any]],
    *,
    head_msg_id: str,
    run_id: str,
    operator_author: str,
) -> bool:
    """D2⁵: has a post of ``run_id`` (any kind) landed after the task, under the operator's name?"""
    for msg in messages_after(messages, head_msg_id):
        if str(msg.get("author") or "").casefold() != operator_author.casefold():
            continue
        if run_id in marker_run_ids(_content(msg)):
            return True
    return False


__all__ = [
    "KIND_QUARANTINE",
    "KIND_REPORT",
    "LANE_LINE_PREFIX",
    "SECTIONS",
    "compose_report",
    "has_lane_line",
    "header",
    "late_report_line",
    "marker",
    "marker_run_ids",
    "messages_after",
    "posted",
    "render_quarantine",
    "validate_report",
    "with_late_report",
]
