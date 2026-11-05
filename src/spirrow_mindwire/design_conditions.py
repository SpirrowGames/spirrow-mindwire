"""Conditional proceed, PR-gate side: which design conditions govern a PR, and were they met.

Thread T-naysayer-conditional-proceed-on-small-fixmin. The design was settled over Bohr msg-7950 →
msg-7960 and Einstein's go that followed; this module implements the PR-gate half of it (the
verification) and nothing of the naysayer's new exit, which lands separately and only after this
one, so that no conditional go can be issued while nothing checks it (msg-7954 split order).

Three inputs, each read here, none trusted from the implementer alone:

1. **The project setting** (``[naysayer_gating].conditional_proceed``, :func:`mode_for`). Absent →
   the gate does not build a check at all and the review is byte-identical to before. ``issuing``
   and ``verify_only`` both verify; any other value verifies too (fail-closed, msg-7960 Obj-5-1).
   ``verify_only`` exists so that issuing can be stopped without dropping the verification of
   conditions already issued — which is why a key, once written, must never be deleted: the gate
   cannot tell a deleted key from one never written (the stated limit of a stateless design).
2. **The design thread**, found from the PR body's ``Thread:`` line with the same parser the
   auto-gate uses (:func:`~.pr_event_advance.auto_gate.decide.resolve_thread_link`), then read
   straight from the chatroom. :func:`evaluate_thread` applies the 7-row table of msg-7960.
3. **The PR body's** ``## Design conditions`` section, checked against the canonical list by
   :func:`check_pr_body` — checks (i)-(iv) of msg-7954.

A *valid go to the implementer* (rows 3-6) is a message whose final ``NEXT:`` resolves to an
implementer of the roster and whose author may advance a design under the conductor's existing
rules: the naysayer with ``TIER-C-CHECK: none`` directly above the ``NEXT:`` (carve-out ③'s G2), or
the human identity (carve-out ①). The PR-gate relay posts ``NEXT: <implementer>`` on every
REQUEST_CHANGES, but under the relay author, so it is not a go and cannot reopen anything here.
Attestation (carve-out ③'s other half) is the conductor's to check at routing time; a go the
conductor refused to route never produced the work this gate is reviewing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .conductor.handoff import HandoffKind, declares_no_tier_c, resolve_handoff
from .magickit.client import MagickitMcpError, McpToolCaller
from .naysayer.conditions_check import (
    NOT_APPLICABLE_CHECK,
    ConditionsOutcome,
    ConditionsResolver,
    DesignCondition,
    DesignConditionsCheck,
)
from .naysayer.pr_review import ObjectionParse, parse_objections
from .pr_event_advance.auto_gate.decide import resolve_thread_link
from .value_objects import Role

logger = logging.getLogger(__name__)


class ConditionalProceedMode(StrEnum):
    """The three documented states of the per-project key, plus everything else (msg-7960)."""

    ABSENT = "absent"
    ISSUING = "issuing"
    VERIFY_ONLY = "verify_only"
    UNKNOWN = "unknown"

    @property
    def verifies(self) -> bool:
        """The PR gate checks conditions and refuses an unknown ``Thread:`` (all but ABSENT)."""
        return self is not ConditionalProceedMode.ABSENT

    @property
    def issues(self) -> bool:
        """The naysayer may be offered the conditional exit (ISSUING only, never UNKNOWN)."""
        return self is ConditionalProceedMode.ISSUING


def mode_for(modes: Mapping[str, str], project: str) -> ConditionalProceedMode:
    """Resolve ``project``'s setting. An unrecognised value is UNKNOWN: verify, never issue."""
    if project not in modes:
        return ConditionalProceedMode.ABSENT
    value = modes[project].strip().casefold()
    if value == ConditionalProceedMode.ISSUING.value:
        return ConditionalProceedMode.ISSUING
    if value == ConditionalProceedMode.VERIFY_ONLY.value:
        return ConditionalProceedMode.VERIFY_ONLY
    return ConditionalProceedMode.UNKNOWN


# ─── formats ──────────────────────────────────────────────────────────────────────────────────
#
# The go:            ``Conditions (k):`` on a line of its own, followed by k lines ``Cond-<r>-<n>``
#                    (ids only — the text is the ``fix_min`` of objection n in the same post's
#                    objections block, which is the single canonical source: msg-7952 Obj-1-4).
# The abort:         ``Conditions-aborted: <msg-id of the conditional go>`` on a line of its own.
# The PR body:       ``## Design conditions`` heading; under it one line per condition,
#                    ``Cond-<r>-<n>: <fix_min verbatim> — applied: <where>``.
#
# Every marker is anchored at the start of its line, so prose that mentions the format inline
# (in backticks, mid-sentence) is not read as the thing itself.

CONDITIONS_HEADING_RE = re.compile(r"^\s*Conditions \((?P<k>\d+)\):\s*$")
_GO_COND_ID_RE = re.compile(r"^\s*(?:[-*]\s+)?(?P<id>Cond-(?P<round>\d+)-(?P<n>\d+))\s*$")
CONDITIONS_ABORTED_RE = re.compile(r"^\s*Conditions-aborted:\s*(?P<target>\S+)\s*$")
_SECTION_HEADING_RE = re.compile(r"^##\s+Design conditions\s*$")
_NEXT_HEADING_RE = re.compile(r"^#{1,2}\s")
_PR_COND_ROW_RE = re.compile(r"^\s*(?:[-*]\s+)?Cond-\d+-\d+\b")
_PR_COND_APPLIED_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?P<id>Cond-\d+-\d+):\s*(?P<fix_min>.+)\s+—\s+applied:\s*(?P<where>\S.*)$"
)
_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


# ─── the thread ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ThreadPolicy:
    """Who is who in the design thread: the roster the conductor routes by, and the human."""

    roster: Mapping[str, Role]
    human_identity: str = "human"

    def role_of(self, author: str) -> Role | None:
        folded = author.casefold()
        for identity, role in self.roster.items():
            if identity.casefold() == folded:
                return role
        return None

    def is_naysayer(self, author: str) -> bool:
        return self.role_of(author) is Role.NAYSAYER

    def is_human(self, author: str) -> bool:
        return bool(self.human_identity) and author.casefold() == self.human_identity.casefold()

    def is_valid_go(self, author: str, content: str) -> bool:
        """A go to the implementer under the existing routing rules (module docstring)."""
        handoff = resolve_handoff(content, self.roster)
        if handoff.kind is not HandoffKind.ROLE or handoff.role is not Role.IMPLEMENTER:
            return False
        if self.is_human(author):
            return True
        return self.is_naysayer(author) and declares_no_tier_c(content)


@dataclass(frozen=True)
class ThreadMessage:
    msg_id: str
    author: str
    content: str


@dataclass(frozen=True)
class ThreadVerdict:
    """What the design thread says, before the PR body is looked at."""

    outcome: ConditionsOutcome  # NOT_APPLICABLE / VERIFY / HOLD — never FAIL
    reasons: tuple[str, ...] = ()
    go_msg_id: str = ""
    conditions: tuple[DesignCondition, ...] = ()


def _has_conditions_heading(content: str) -> bool:
    return any(CONDITIONS_HEADING_RE.match(line) for line in content.splitlines())


def canonical_conditions(content: str) -> tuple[tuple[DesignCondition, ...], str | None]:
    """The go's canonical condition list, or ``()`` and the reason it is unreadable ((v)).

    Read from the go alone: the ``Conditions (k):`` id lines say WHICH objections are conditions,
    and the same post's objections block supplies each one's ``fix_min``. Every blocking objection
    must be a condition — a blocking objection left out would be an objection the go silently
    dropped — and every condition must be a blocking objection with a non-empty ``fix_min``.
    """
    lines = content.splitlines()
    heads = [i for i, line in enumerate(lines) if CONDITIONS_HEADING_RE.match(line)]
    if len(heads) != 1:
        return (), f"the go carries {len(heads)} `Conditions (k):` lines (exactly 1 required)"
    start = heads[0]
    head_match = CONDITIONS_HEADING_RE.match(lines[start])
    assert head_match is not None
    k = int(head_match.group("k"))
    ids: list[tuple[str, int]] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            if ids:
                break
            continue
        m = _GO_COND_ID_RE.match(line)
        if m is None:
            break
        ids.append((m.group("id"), int(m.group("n"))))
    if k < 1 or len(ids) != k:
        return (), f"`Conditions ({k}):` is followed by {len(ids)} condition id line(s)"
    if len({cid for cid, _ in ids}) != len(ids) or len({n for _, n in ids}) != len(ids):
        return (), "the go lists a condition id or objection position twice"
    report = parse_objections(content)
    if report.status is ObjectionParse.MISSING:
        return (), "the go's objections block is unreadable (objections_unparsed)"
    objections = report.objections
    conditions: list[DesignCondition] = []
    for cid, n in ids:
        if not 1 <= n <= len(objections):
            return (), f"{cid} names objection {n}, but the block has {len(objections)}"
        objection = objections[n - 1]
        if not objection.blocks or not objection.fix_min.strip():
            return (), f"{cid} names objection {n}, which is not blocking with a `fix_min`"
        conditions.append(DesignCondition(cond_id=cid, fix_min=_normalise(objection.fix_min)))
    named = {n for _, n in ids}
    blocking = {i + 1 for i, o in enumerate(objections) if o.blocks}
    if blocking != named:
        missing = ", ".join(str(n) for n in sorted(blocking - named))
        return (), f"blocking objection(s) {missing} of the go are not among its conditions"
    return tuple(conditions), None


def evaluate_thread(messages: Sequence[ThreadMessage], policy: ThreadPolicy) -> ThreadVerdict:
    """The msg-7960 table, top to bottom, first matching row wins.

    1. two or more conditional gos                                    → HOLD (ambiguous)
    2. an invalid ``Conditions-aborted:`` line                        → HOLD (ambiguous)
    3. a valid abort, no valid go after it                            → HOLD (restart pending)
    4. a valid abort, a valid go after it                             → NOT_APPLICABLE
    5. no abort, a valid go after the conditional go                  → HOLD (ambiguous)
    6. no abort, one conditional go, nothing after it                 → VERIFY (canonical list)
    7. no conditional go                                              → NOT_APPLICABLE

    A conditional go is a valid naysayer go carrying a ``Conditions (k):`` line. An abort line is
    valid iff a naysayer wrote it, after the conditional go, naming that go's msg-id; any other
    abort line — another author, another id, before the go, or with no go at all — is row 2.
    """
    conditional = [
        i
        for i, m in enumerate(messages)
        if policy.is_naysayer(m.author)
        and _has_conditions_heading(m.content)
        and policy.is_valid_go(m.author, m.content)
    ]
    if len(conditional) >= 2:
        ids = ", ".join(messages[i].msg_id for i in conditional)
        return ThreadVerdict(
            ConditionsOutcome.HOLD,
            (f"the thread carries {len(conditional)} conditional gos ({ids}); one is allowed",),
        )
    go_index = conditional[0] if conditional else None
    go_id = messages[go_index].msg_id if go_index is not None else ""

    valid_aborts: list[int] = []
    for i, m in enumerate(messages):
        for line in m.content.splitlines():
            match = CONDITIONS_ABORTED_RE.match(line)
            if match is None:
                continue
            target = match.group("target")
            if (
                go_index is not None
                and i > go_index
                and policy.is_naysayer(m.author)
                and target == go_id
            ):
                valid_aborts.append(i)
                continue
            return ThreadVerdict(
                ConditionsOutcome.HOLD,
                (
                    f"{m.msg_id} by {m.author} carries `Conditions-aborted: {target}`, which "
                    "is not a naysayer's abort of this thread's conditional go posted after it",
                ),
            )

    if valid_aborts:
        abort_index = valid_aborts[0]
        restarted = any(
            policy.is_valid_go(m.author, m.content) for m in messages[abort_index + 1 :]
        )
        if not restarted:
            return ThreadVerdict(
                ConditionsOutcome.HOLD,
                (
                    f"the conditional go {go_id} was aborted in "
                    f"{messages[abort_index].msg_id} and no go has followed yet",
                ),
            )
        return ThreadVerdict(ConditionsOutcome.NOT_APPLICABLE)

    if go_index is None:
        return ThreadVerdict(ConditionsOutcome.NOT_APPLICABLE)

    later = [m.msg_id for m in messages[go_index + 1 :] if policy.is_valid_go(m.author, m.content)]
    if later:
        return ThreadVerdict(
            ConditionsOutcome.HOLD,
            (
                f"the conditional go {go_id} is followed by go(s) {', '.join(later)} with no "
                "`Conditions-aborted:` between them",
            ),
        )
    conditions, problem = canonical_conditions(messages[go_index].content)
    if problem is not None:
        return ThreadVerdict(ConditionsOutcome.HOLD, (f"{go_id}: {problem}",), go_msg_id=go_id)
    return ThreadVerdict(ConditionsOutcome.VERIFY, go_msg_id=go_id, conditions=conditions)


# ─── the PR body ──────────────────────────────────────────────────────────────────────────────


def design_conditions_section(body: str) -> str | None:
    """The ``## Design conditions`` section's lines (heading excluded), or ``None`` if absent.

    Cut deterministically: from the heading to the next level-1/2 heading or the end. Nothing
    else of the PR body reaches the model through this path (msg-7952 Obj-1-1).
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if _SECTION_HEADING_RE.match(line.strip()):
            out: list[str] = []
            for rest in lines[i + 1 :]:
                if _NEXT_HEADING_RE.match(rest):
                    break
                out.append(rest)
            return "\n".join(out).strip("\n")
    return None


def check_pr_body(verdict: ThreadVerdict, pr_body: str, *, thread_id: str) -> DesignConditionsCheck:
    """Apply checks (i)-(iv) of msg-7954 to a VERIFY thread verdict; pass the others through."""
    if verdict.outcome is not ConditionsOutcome.VERIFY:
        if verdict.outcome is ConditionsOutcome.NOT_APPLICABLE:
            return NOT_APPLICABLE_CHECK
        return DesignConditionsCheck(
            outcome=verdict.outcome,
            reasons=verdict.reasons,
            thread_id=thread_id,
            go_msg_id=verdict.go_msg_id,
        )
    section = design_conditions_section(pr_body)
    base = DesignConditionsCheck(
        outcome=ConditionsOutcome.VERIFY,
        thread_id=thread_id,
        go_msg_id=verdict.go_msg_id,
        conditions=verdict.conditions,
        declared=section or "",
    )
    k = len(verdict.conditions)
    if section is None:
        return _fail(base, f"(i) the PR body has no `## Design conditions` section ({k} required)")
    rows = [line for line in section.splitlines() if _PR_COND_ROW_RE.match(line)]
    declared: dict[str, str] = {}
    malformed: list[str] = []
    for row in rows:
        m = _PR_COND_APPLIED_RE.match(row)
        if m is None:
            malformed.append(row.strip())
            continue
        declared.setdefault(m.group("id"), _normalise(m.group("fix_min")))
    reasons: list[str] = []
    for cond in verdict.conditions:
        if cond.cond_id not in declared:
            reasons.append(f"(ii) {cond.cond_id} is not listed as applied")
        elif declared[cond.cond_id] != cond.fix_min:
            reasons.append(f"(iii) {cond.cond_id}'s `fix_min` does not match the design go")
    if len(rows) != k:
        reasons.append(f"(iv) the section has {len(rows)} condition row(s); the go has {k}")
    reasons.extend(f"row not in the `— applied: <where>` form: `{row}`" for row in malformed)
    if reasons:
        return _fail(base, *reasons)
    return base


def _fail(base: DesignConditionsCheck, *reasons: str) -> DesignConditionsCheck:
    return DesignConditionsCheck(
        outcome=ConditionsOutcome.FAIL,
        reasons=tuple(reasons),
        thread_id=base.thread_id,
        go_msg_id=base.go_msg_id,
        conditions=base.conditions,
        declared=base.declared,
    )


def _hold(reason: str, *, thread_id: str = "") -> DesignConditionsCheck:
    return DesignConditionsCheck(
        outcome=ConditionsOutcome.HOLD, reasons=(reason,), thread_id=thread_id
    )


# ─── the resolver the PR gate calls ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DesignConditionsPolicy:
    """Everything the PR gate needs to build a :data:`ConditionsResolver` for a project."""

    modes: Mapping[str, str] = field(default_factory=dict)
    roster: Mapping[str, Role] = field(default_factory=dict)
    human_identity: str = "human"

    def resolver(self, mcp: McpToolCaller, *, project: str) -> ConditionsResolver | None:
        """``None`` when ``project`` never enabled the setting — the gate then builds no check."""
        if not mode_for(self.modes, project).verifies:
            return None
        thread_policy = ThreadPolicy(roster=self.roster, human_identity=self.human_identity)

        async def resolve(pr_body: str | None) -> DesignConditionsCheck:
            return await resolve_design_conditions(
                mcp, project=project, pr_body=pr_body, policy=thread_policy
            )

        return resolve


def build_policy(
    modes: Mapping[str, str], roster: Mapping[str, Role], *, human_identity: str
) -> DesignConditionsPolicy | None:
    """The orchestrator's policy, or ``None`` when no project names the setting at all."""
    if not modes:
        return None
    return DesignConditionsPolicy(
        modes=dict(modes), roster=dict(roster), human_identity=human_identity
    )


async def resolve_design_conditions(
    mcp: McpToolCaller, *, project: str, pr_body: str | None, policy: ThreadPolicy
) -> DesignConditionsCheck:
    """PR body → ``Thread:`` → thread → table → (i)-(iv). Every unreadable step is a HOLD."""
    if pr_body is None:
        return _hold("the PR body could not be read, so its design thread is unknown")
    link = resolve_thread_link(pr_body)
    if link.thread_id is None:
        return _hold(
            "the PR body names no single design thread (`Thread:` line "
            f"{(link.noop.value if link.noop is not None else 'unresolved')}); this project "
            "verifies design conditions, so a PR whose thread is unknown cannot be approved"
        )
    thread_id = link.thread_id
    try:
        payload: Any = await mcp.call_tool(
            "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
        )
    except MagickitMcpError as exc:
        logger.warning("design conditions: cannot read %s/%s: %s", project, thread_id, exc)
        return _hold(f"the design thread {thread_id} could not be read", thread_id=thread_id)
    raw = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return _hold(f"the design thread {thread_id} could not be read", thread_id=thread_id)
    messages = [
        ThreadMessage(
            msg_id=str(m.get("msg_id") or ""),
            author=str(m.get("author") or ""),
            content=str(m.get("content") or ""),
        )
        for m in raw
        if isinstance(m, dict)
    ]
    check = check_pr_body(evaluate_thread(messages, policy), pr_body, thread_id=thread_id)
    logger.info(
        "design conditions %s/%s: outcome=%s go=%s conditions=%d reasons=%d",
        project,
        thread_id,
        check.outcome.value,
        check.go_msg_id or "-",
        len(check.conditions),
        len(check.reasons),
    )
    return check
