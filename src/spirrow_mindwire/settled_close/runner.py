"""One settled_close pass: read every open thread, classify it, and close only when enabled.

T-done-disposition-auto-close boundary 6 (Bohr msg-8455 §2). The shape is
:mod:`spirrow_mindwire.pr_review_sweep.phase2`'s (msg-8177 §A):

* **Disabled (the default) makes no write and no close.** The dry-run reads (listing, one
  ``mode="full"`` thread read per open thread, one identity lookup per distinct owner) and prints
  what it would try. A *tick* that is disabled does not even read: the caller returns before
  any MCP client exists (:mod:`.__main__`), so a disabled sweep costs nothing.
* **Enabled** sends ``chatroom_close_thread(..., settled_head_msg_id=<the head judged>)`` for
  each ``would_close`` thread, up to ``max_attempts`` per pass, and stops after ``breaker``
  consecutive refusals it cannot place. The outcome is decided by reading the thread back
  (``status == "resolved"`` is ``closed``), never by reading the error. A refusal is placed by
  magickit's ``details.reason`` / ``error_type`` as the server typed them
  (:func:`~.classify.refusal_class`); the raw values are kept in the report.

Enabling it is not this module's decision: it is the separate Tier-C request (enable, register
the closer in Prismind, add it to magickit ``settled_close.closers``), filed with the dry-run
table this module prints (msg-8455 §2 6-4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..identity.normalize import normalize_identity_key
from ..magickit.client import MagickitMcpError
from ..pr_event_advance.runner import list_open_threads
from ..value_objects import Role
from .classify import (
    HUMAN_ROLE,
    LISTED_CLASSES,
    OwnerKind,
    SettledClass,
    ThreadHead,
    Verdict,
    classify,
    is_human_name,
    refusal_class,
)
from .config import SettledCloseConfig

#: Every tool a dry-run calls. All reads.
READ_TOOLS = frozenset({"chatroom_list_threads", "chatroom_get_thread", "get_identity"})
#: An enabled pass adds exactly one write.
CLOSE_TOOLS = READ_TOOLS | {"chatroom_close_thread"}
#: ``chatroom_close_thread`` emits a ``decide``, so an embodiment is required (phase 2's value).
CLOSE_EMBODIMENT = "unknown"
_RESOLVED = "resolved"

AUTHORITY_NOTE = (
    "magickit decides (settled_disposition.py); this table is the client's prediction. "
    "would_close is refused today with closer_not_configured: magickit settled_close.closers "
    "ships empty, and adding a closer is a separate Tier-C decision."
)


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class OwnerLookup:
    """magickit's owner check (``_settled_identity_refusal``), one registry read per owner."""

    def __init__(self, mcp: ToolCaller) -> None:
        self._mcp = mcp
        self._cache: dict[str, OwnerKind] = {}

    async def kind(self, owner: str) -> OwnerKind:
        if not owner.strip():
            return OwnerKind.MISSING
        if is_human_name(owner):
            return OwnerKind.HUMAN
        if owner not in self._cache:
            self._cache[owner] = await self._lookup(owner)
        return self._cache[owner]

    async def _lookup(self, owner: str) -> OwnerKind:
        # magickit looks the name up as given, then by its ADR-11 key.
        names = [owner]
        key = normalize_identity_key(owner)
        if key and key != owner:
            names.append(key)
        for name in names:
            try:
                result = await self._mcp.call_tool("get_identity", {"identity_name": name})
            except MagickitMcpError:
                return OwnerKind.UNAVAILABLE
            status = result.get("status") if isinstance(result, dict) else None
            if status == "not_found":
                continue
            if status != "found":
                return OwnerKind.UNAVAILABLE
            identity = result.get("identity")
            roles = identity.get("allowed_roles") if isinstance(identity, dict) else None
            if not isinstance(roles, list):
                return OwnerKind.UNAVAILABLE
            return OwnerKind.HUMAN if HUMAN_ROLE in roles else OwnerKind.AGENT
        return OwnerKind.UNRESOLVED


@dataclass(frozen=True)
class Row:
    project: str
    thread_id: str
    owner: str
    head_msg_id: str
    verdict: Verdict
    #: Enabled only: what the close call raised, verbatim (never branched on but for the class).
    error_type: str | None = None
    details_reason: str | None = None
    observed_status: str | None = None
    #: True once a close call was sent for this row (enabled passes only).
    attempted: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "project": self.project,
            "thread_id": self.thread_id,
            "owner": self.owner,
            "head_msg_id": self.head_msg_id,
            "class": self.verdict.cls.value,
            "reason": self.verdict.reason,
        }
        if self.verdict.due_at is not None:
            out["due_at"] = self.verdict.due_at.isoformat()
        if self.attempted:
            out["error_type"] = self.error_type
            out["details_reason"] = self.details_reason
            out["observed_status"] = self.observed_status
        return out


@dataclass
class PassReport:
    enabled: bool
    include_legacy: bool
    now: datetime
    projects: list[str]
    rows: list[Row] = field(default_factory=list)
    #: ``would_close`` rows an enabled pass did not reach (cap or breaker).
    not_reached: list[tuple[Row, str]] = field(default_factory=list)
    breaker_tripped: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def close_calls(self) -> int:
        return sum(1 for r in self.rows if r.attempted)

    def by_class(self, cls: SettledClass) -> list[Row]:
        return [r for r in self.rows if r.verdict.cls is cls]

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": "settled_close",
            "authority": AUTHORITY_NOTE,
            "enabled": self.enabled,
            "include_legacy": self.include_legacy,
            "now": self.now.isoformat(),
            "projects": self.projects,
            "close_calls": self.close_calls,
            "counts": {c.value: len(self.by_class(c)) for c in SettledClass},
            "threads": {
                c.value: [r.as_dict() for r in self.by_class(c)]
                for c in LISTED_CLASSES
                if self.by_class(c)
            },
            "not_reached": [r.as_dict() | {"why": why} for r, why in self.not_reached],
            "breaker_tripped": self.breaker_tripped,
            "errors": self.errors,
        }


def close_summary(head: ThreadHead) -> str:
    """The ``decide`` body of an enabled close. Fixed text, no LLM (msg-8172 §3)."""
    return (
        f"settled_close: closed because its head {head.head_msg_id} is a proposer's "
        "`STOP: done` (disposition done) with no later message for the grace period "
        "(T-done-disposition-auto-close, carve-out settled_disposition)."
    )


async def read_heads(
    mcp: ToolCaller, project: str
) -> tuple[list[ThreadHead], list[dict[str, str]]]:
    """Every open thread of ``project`` with its newest message. A listing failure raises."""
    heads: list[ThreadHead] = []
    errors: list[dict[str, str]] = []
    for thread_id in await list_open_threads(mcp, project):
        try:
            payload = await mcp.call_tool(
                "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
            )
        except MagickitMcpError as exc:
            errors.append({"project": project, "thread_id": thread_id, "error": str(exc)})
            continue
        thread = payload.get("thread") if isinstance(payload, dict) else None
        messages = payload.get("messages") if isinstance(payload, dict) else None
        rows = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
        if not isinstance(thread, dict) or not rows:
            errors.append({"project": project, "thread_id": thread_id, "error": "no messages"})
            continue
        tags = thread.get("tags")
        owner = thread.get("owner")
        heads.append(
            ThreadHead(
                project=project,
                thread_id=thread_id,
                owner=owner if isinstance(owner, str) else "",
                tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
                head=rows[-1],
            )
        )
    return heads, errors


async def read_status(mcp: ToolCaller, project: str, thread_id: str) -> str | None:
    try:
        payload = await mcp.call_tool(
            "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "summary"}
        )
    except MagickitMcpError:
        return None
    thread = payload.get("thread") if isinstance(payload, dict) else None
    status = thread.get("status") if isinstance(thread, dict) else None
    return status if isinstance(status, str) else None


async def attempt_close(mcp: ToolCaller, head: ThreadHead, config: SettledCloseConfig) -> Row:
    """Send one close for ``head``, then decide the outcome from the read-back alone."""
    error_type: str | None = None
    details_reason: str | None = None
    try:
        await mcp.call_tool(
            "chatroom_close_thread",
            {
                "project": head.project,
                "thread_id": head.thread_id,
                "author": config.author,
                "role": config.role,
                "embodiment": CLOSE_EMBODIMENT,
                "summary_content": close_summary(head),
                "settled_head_msg_id": head.head_msg_id,
            },
        )
    except MagickitMcpError as exc:
        error_type = exc.error_type
        details_reason = exc.details_reason
    status = await read_status(mcp, head.project, head.thread_id)
    if status == _RESOLVED:
        verdict = Verdict(SettledClass.CLOSED)
    else:
        verdict = Verdict(
            refusal_class(error_type, details_reason), details_reason or error_type or ""
        )
    return Row(
        head.project,
        head.thread_id,
        head.owner,
        head.head_msg_id,
        verdict,
        error_type=error_type,
        details_reason=details_reason,
        observed_status=status,
        attempted=True,
    )


async def run_pass(
    mcp: ToolCaller,
    *,
    projects: Iterable[str],
    config: SettledCloseConfig,
    enabled: bool,
    include_legacy: bool,
    roster: Mapping[str, Role],
    now: datetime,
) -> PassReport:
    """Classify every open thread of ``projects``; close the ``would_close`` ones if enabled."""
    if now.tzinfo is None:
        raise ValueError("run_pass needs an aware 'now'")
    report = PassReport(
        enabled=enabled, include_legacy=include_legacy, now=now, projects=sorted(set(projects))
    )
    owners = OwnerLookup(mcp)
    grace = timedelta(seconds=config.grace_seconds)
    candidates: list[tuple[ThreadHead, Row]] = []
    for project in report.projects:
        try:
            heads, errors = await read_heads(mcp, project)
        except MagickitMcpError as exc:
            report.errors.append({"project": project, "error": f"listing failed: {exc}"})
            continue
        report.errors.extend(errors)
        for head in heads:
            verdict = classify(
                head,
                closer=config.author,
                now=now,
                grace=grace,
                roster=roster,
                owner_kind=await owners.kind(head.owner),
                include_legacy=include_legacy,
            )
            row = Row(project, head.thread_id, head.owner, head.head_msg_id, verdict)
            if verdict.cls is SettledClass.WOULD_CLOSE:
                candidates.append((head, row))
            report.rows.append(row)

    if not enabled:
        return report

    streak = 0
    for index, (head, predicted) in enumerate(candidates):
        if index >= config.max_attempts:
            report.not_reached.extend((r, "attempt-cap") for _, r in candidates[index:])
            break
        if streak >= config.breaker:
            report.not_reached.extend((r, "breaker") for _, r in candidates[index:])
            break
        outcome = await attempt_close(mcp, head, config)
        report.rows[report.rows.index(predicted)] = outcome
        streak = streak + 1 if outcome.verdict.cls is SettledClass.REFUSED else 0
    report.breaker_tripped = streak >= config.breaker
    return report


def render_table(report: PassReport) -> str:
    """The dry-run as Markdown, for pasting into the thread (msg-8455 §2 6-4)."""
    lines = [
        "## settled_close dry-run" if not report.enabled else "## settled_close pass",
        "",
        f"**{AUTHORITY_NOTE}**",
        "",
        f"- at: {report.now.isoformat()}",
        f"- projects: {', '.join(report.projects) or '(none)'}",
        f"- include_legacy: {str(report.include_legacy).lower()}",
        f"- close calls: {report.close_calls}",
        "",
        "| class | count |",
        "|---|---|",
    ]
    lines += [f"| {c.value} | {len(report.by_class(c))} |" for c in SettledClass]
    for cls in LISTED_CLASSES:
        rows = report.by_class(cls)
        if not rows:
            continue
        lines += ["", f"### {cls.value} ({len(rows)})", "", "| thread | owner | head | reason |"]
        lines.append("|---|---|---|---|")
        for r in sorted(rows, key=lambda r: (r.project, r.thread_id)):
            reason = r.verdict.reason
            if r.verdict.due_at is not None:
                reason = f"{reason} (due {r.verdict.due_at.isoformat()})"
            lines.append(
                f"| {r.project}/{r.thread_id} | {r.owner or '-'} | {r.head_msg_id} "
                f"| {reason or '-'} |"
            )
    if report.errors:
        lines += ["", f"### errors ({len(report.errors)})", ""]
        lines += [f"- {e}" for e in report.errors]
    return "\n".join(lines) + "\n"


__all__ = [
    "AUTHORITY_NOTE",
    "CLOSE_EMBODIMENT",
    "CLOSE_TOOLS",
    "READ_TOOLS",
    "OwnerLookup",
    "PassReport",
    "Row",
    "attempt_close",
    "close_summary",
    "read_heads",
    "read_status",
    "render_table",
    "run_pass",
]
