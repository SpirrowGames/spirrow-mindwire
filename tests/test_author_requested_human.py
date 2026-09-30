"""``Handoff.author_requested_human`` — a positive "the author named the human" fact.

T-reconcile-field-mismatch-flag-overloaded msg-4861 / msg-4864 U1: ``core.py`` used to pass
``author_wrote_next_human=handoff.mismatch_reason is None`` to the Tier-C hook, deriving a
positive intent from the absence of a mismatch. That held only while every ``HUMAN`` producer
other than the author's own was a mismatch escalation. These tests pin the positive predicate
at the resolver, the construction-time rejection of contradictory combinations, and the
conductor wiring — plus the ``STOP:`` head, which never reaches the hook: a STOP-only head has
no final ``NEXT:`` and resolves ABSENT, and the #363 parser reads ``STOP:`` only as an
annotation above a final ``NEXT: none`` (a NONE handoff).
"""

from __future__ import annotations

from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

import spirrow_mindwire.conductor.core as core_mod
from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.conductor.handoff import (
    Handoff,
    HandoffKind,
    MismatchReason,
    resolve_handoff,
)
from spirrow_mindwire.value_objects import Role

_R = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}


# --------------------------------------------------------------------------- resolver


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("done\n\nNEXT: human", None),  # body NEXT: human (field absent)
        ("done\n\nNEXT: Human", None),  # case-folded sentinel
        ("done", "human"),  # field human, body silent (row 3)
        ("done\n\nNEXT: human", "human"),  # field human, body agrees (row 4)
    ],
)
def test_author_named_human_is_true(body: str, field: str | None) -> None:
    h = resolve_handoff(body, _R, next_participant=field)
    assert h.kind is HandoffKind.HUMAN
    assert h.mismatch_reason is None
    assert h.author_requested_human is True


def test_target_divergence_is_not_an_author_request() -> None:
    h = resolve_handoff("done\n\nNEXT: human", _R, next_participant="Bohr")
    assert h.kind is HandoffKind.HUMAN
    assert h.mismatch_reason is MismatchReason.TARGET_DIVERGENCE
    assert h.author_requested_human is False


@pytest.mark.parametrize("body", ["done\n\nNEXT: human", "done\n\nNEXT: Bohr", "done"])
def test_field_unresolvable_is_not_an_author_request(body: str) -> None:
    # Even when the body says ``NEXT: human``, an unresolvable field is a conductor safety
    # valve, not the author asking for the human.
    h = resolve_handoff(body, _R, next_participant="Schrodinger")
    assert h.kind is HandoffKind.HUMAN
    assert h.mismatch_reason is MismatchReason.FIELD_UNRESOLVABLE
    assert h.author_requested_human is False


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("d\n\nNEXT: Bohr", None),
        ("d\n\nNEXT: none", None),
        ("d\n\nNEXT: pr-review acme/widgets#7", None),
        ("d\n\nNEXT: Nobody", None),
        ("d", None),
        ("d", "Bohr"),
        ("d", "none"),
    ],
)
def test_non_human_handoffs_are_not_author_requests(body: str, field: str | None) -> None:
    h = resolve_handoff(body, _R, next_participant=field)
    assert h.kind is not HandoffKind.HUMAN
    assert h.author_requested_human is False


def test_stop_line_head_resolves_absent_on_this_tree() -> None:
    # msg-4864 U1 pre-check, recorded as a pin. The branch was cut before the ``STOP:`` parser
    # (#363) was on main; after merging it, a STOP-only head still has no final ``NEXT:`` and
    # resolves ABSENT. If a later change makes a STOP line resolve differently, this test is
    # meant to go red so the author of that change decides what ``author_requested_human``
    # should be for it.
    h = resolve_handoff("halted: gate red\n\nSTOP: gate_red", _R)
    assert h.kind is HandoffKind.ABSENT
    assert h.mismatch_reason is None
    assert h.author_requested_human is False


def test_stop_line_above_next_none_is_not_an_author_request() -> None:
    # The #363 form: ``STOP:`` annotates a final ``NEXT: none``. It resolves NONE (never HUMAN),
    # so it can never read as the author naming the human, whatever the STOP line says.
    h = resolve_handoff("x\n\nSTOP: blocked-on human wake:human\nNEXT: none", _R)
    assert h.kind is HandoffKind.NONE
    assert h.stop_line is not None
    assert h.author_requested_human is False


# --------------------------------------------------------------------------- construction


@pytest.mark.parametrize(
    "kind",
    [HandoffKind.ROLE, HandoffKind.NONE, HandoffKind.PR_REVIEW, HandoffKind.ABSENT],
)
def test_author_request_on_non_human_kind_is_rejected(kind: HandoffKind) -> None:
    with pytest.raises(ValueError, match="requires kind=HUMAN"):
        Handoff(kind, author_requested_human=True)


@pytest.mark.parametrize("reason", list(MismatchReason))
def test_author_request_with_mismatch_reason_is_rejected(reason: MismatchReason) -> None:
    with pytest.raises(ValueError, match="incompatible with a conductor escalation"):
        Handoff(HandoffKind.HUMAN, mismatch_reason=reason, author_requested_human=True)


def test_default_is_false_and_valid_for_every_kind() -> None:
    for kind in HandoffKind:
        assert Handoff(kind).author_requested_human is False


# --------------------------------------------------------------------------- conductor wiring


class _Stub:
    tierc_mode = "shadow"

    def is_target(self, state: Any) -> bool:
        return True

    async def evaluate(self, state: Any) -> None:
        return None


async def _hook_calls(
    monkeypatch: pytest.MonkeyPatch, *, content: str, next_participant: str | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _spy(decider: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(core_mod, "run_tierc_hook", _spy)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=content, next_participant=next_participant)
    await Conductor(
        mcp=mcp,
        dispatcher=_ScriptedDispatcher(mcp, {}),
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        decider=_Stub(),
    ).run()
    return calls


@pytest.mark.parametrize(
    ("content", "field", "expected"),
    [
        ("revised\n\nNEXT: human", None, True),
        ("revised", "human", True),
        ("revised\n\nNEXT: human", "Heisenberg", False),  # TARGET_DIVERGENCE
        ("revised\n\nNEXT: human", "Schrodinger", False),  # FIELD_UNRESOLVABLE
    ],
)
@pytest.mark.anyio
async def test_conductor_passes_the_positive_fact_to_the_hook(
    monkeypatch: pytest.MonkeyPatch, content: str, field: str | None, expected: bool
) -> None:
    calls = await _hook_calls(monkeypatch, content=content, next_participant=field)
    assert calls, "a HUMAN head must reach the hook"
    assert calls[0]["author_requested_human"] is expected


@pytest.mark.anyio
async def test_conductor_stop_line_head_never_reaches_the_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await _hook_calls(monkeypatch, content="halted: gate red\n\nSTOP: gate_red")
    assert calls == []


@pytest.mark.anyio
async def test_conductor_does_not_read_a_non_mismatch_escalation_as_an_author_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure mode msg-4861 named: a HUMAN handoff produced by something other than the
    # author and other than a mismatch (``mismatch_reason is None``). The old negated predicate
    # answered True here; the positive fact answers False. This is the regression pin for L745.
    def _escalate(*args: Any, **kwargs: Any) -> Handoff:
        return Handoff(HandoffKind.HUMAN, token="human")

    monkeypatch.setattr(core_mod, "resolve_handoff", _escalate)
    calls = await _hook_calls(monkeypatch, content="revised\n\nNEXT: Heisenberg")
    assert calls, "a HUMAN head must reach the hook"
    assert calls[0]["author_requested_human"] is False
