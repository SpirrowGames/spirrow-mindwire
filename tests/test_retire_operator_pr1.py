"""T-retire-operator-identity-and-scope-next-fermi PR-1: the HUMAN-TASK form, the operator cutoff
(shipped ``None``) and the scope of ``NEXT: Fermi``.

Design: Bohr msg-8874 (v1) / msg-8899 (v2) / msg-8935 (v3) / msg-8941 (v4) / msg-8943 (v5) /
msg-8947 (v6) / msg-8952 (v7) / msg-8954 (v8); approved by Einstein msg-8955. The opener is
Fermi msg-8871 (Takahito 承認済み 2026-11-10). What is pinned here:

- the new form ``HUMAN-TASK: <work>`` / ``TIER-C-CHECK: none`` / ``NEXT: human``: accepted as
  operator work, refused (conflict / no task / no check) exactly as the operator form was;
- ``OPERATOR_MALFORMED_AFTER`` ships ``None`` and changes no reader; with a cutoff set, a
  ``NEXT: operator`` (body or field-only) is ``operator_retired`` from the cutoff on and keeps its
  old reading before it, decided ahead of ``NO_TASK`` (msg-8941 §2), and only for the retired
  ``operator`` token -- never for ``HUMAN-TASK:`` or a plain ``NEXT: human`` (msg-8943 §2);
- head_skip switches on the same constant, and the two-tick path launches a retired head once,
  the conductor posts one notice, and the next tick parks on it (msg-8941 §3);
- ``NEXT: Fermi`` only on a thread whose owner is Fermi (D2): otherwise -- including an owner
  that cannot be read, and a field-only ``next_participant=Fermi`` -- ``fermi_outside_own_thread``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor import handoff as handoff_mod
from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.handoff import (
    HUMAN_TASK_FORM_EXAMPLE,
    OPERATOR_TOKEN,
    Handoff,
    HandoffKind,
    HumanAsk,
    MalformedHandoff,
    OperatorFault,
    build_handoff_protocol_block,
    build_roster_notice,
    human_task_refused,
    malformed_notice,
    operator_refused_after_cutoff,
    resolve_handoff,
    scope_fermi_handoff,
)
from spirrow_mindwire.conductor.head_skip import (
    BODY_DEPENDENT_STOP_TOKENS,
    CACHED_TOKEN_POSTED_AT,
    Decision,
    commit_launch,
    decide,
    parse_head_token,
    stage1_skips,
)
from spirrow_mindwire.pr_event_advance.auto_gate.decide import AutoGateNoop, tail_decision
from spirrow_mindwire.pr_event_advance.decide import Post, operator_handoff
from spirrow_mindwire.value_objects import Role

CUTOFF = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
JUST_BEFORE = CUTOFF - timedelta(microseconds=1)
AFTER = CUTOFF + timedelta(hours=1)
NAIVE = datetime(2026, 11, 13, 0, 0)

HUMAN_TASK = "x\n\nHUMAN-TASK: restart the web service\nTIER-C-CHECK: none\nNEXT: human"
PLAIN_HUMAN = "x\n\nNEXT: human"
BODY_OPERATOR = "x\n\nOPERATOR-TASK: restart the web service\nTIER-C-CHECK: none\nNEXT: operator"
BARE_OPERATOR = "x\n\nNEXT: operator"  # the retired word without its form (NO_TASK before C)
FIELD_ONLY_BODY = "x\n\nno handoff line here"


def _resolve(body: str, **kw: Any) -> Handoff:
    return resolve_handoff(body, _ROSTER, operator_malformed_after=CUTOFF, **kw)


# ------------------------------------------------------------------------------- the new form


def test_the_new_form_is_operator_work() -> None:
    got = resolve_handoff(HUMAN_TASK, _ROSTER)
    assert got.kind is HandoffKind.HUMAN
    assert got.human_ask is HumanAsk.OPERATOR_WORK
    assert got.operator_task == "restart the web service"
    assert got.token == "human"
    # Not a decision: the Decider / Tier-C admission entry fact stays off, as for the old form.
    assert got.author_requested_human is False


def test_the_taught_example_is_a_form_the_parser_accepts() -> None:
    got = resolve_handoff(f"notes\n\n{HUMAN_TASK_FORM_EXAMPLE}", {})
    assert got.human_ask is HumanAsk.OPERATOR_WORK and got.operator_fault is None


@pytest.mark.parametrize(
    ("body", "fault"),
    [
        # A Tier-C declaration anywhere wins (msg-5428 order).
        (
            "x\nTIER-C: cost\n\nHUMAN-TASK: buy a licence\nTIER-C-CHECK: none\nNEXT: human",
            OperatorFault.TIER_C_CONFLICT,
        ),
        # The task line carries no work.
        ("x\n\nHUMAN-TASK:\nTIER-C-CHECK: none\nNEXT: human", OperatorFault.NO_TASK),
        # The task line directly above NEXT: (the TIER-C-CHECK line left out).
        ("x\n\nHUMAN-TASK: restart it\nNEXT: human", OperatorFault.NO_TASK),
        # The task in its slot, but the line above NEXT: is not the check.
        ("x\n\nHUMAN-TASK: restart it\nplease\nNEXT: human", OperatorFault.NO_TIER_C_CHECK),
    ],
)
def test_a_broken_new_form_is_refused(body: str, fault: OperatorFault) -> None:
    got = resolve_handoff(body, _ROSTER)
    assert got.kind is HandoffKind.ABSENT
    assert got.operator_fault is fault
    assert human_task_refused(body)
    # head_skip launches it (``human`` is a stop token, so reading the token would park it
    # silently and the stand-down notice would never be posted).
    assert parse_head_token(body) == ""
    assert not stage1_skips(body)


def test_a_plain_human_is_still_a_decision() -> None:
    got = resolve_handoff("x\n\nTIER-C: cost\nNEXT: human", _ROSTER)
    assert got.kind is HandoffKind.HUMAN and got.human_ask is None
    assert got.tier_c_label == "cost" and got.author_requested_human is True
    # A task line further up is a quote, not the form.
    quoted = "HUMAN-TASK: quoted\n\nmore text\n\nNEXT: human"
    assert resolve_handoff(quoted, _ROSTER).human_ask is None
    assert not human_task_refused(quoted)


def test_a_human_field_keeps_the_body_s_request_for_work() -> None:
    got = resolve_handoff(HUMAN_TASK, _ROSTER, next_participant="human")
    assert got.human_ask is HumanAsk.OPERATOR_WORK and got.mismatch_reason is None
    assert got.operator_task == "restart the web service"


@pytest.mark.parametrize("body", [HUMAN_TASK, PLAIN_HUMAN])
def test_the_new_form_and_a_plain_human_park_in_the_sweep(body: str) -> None:
    assert parse_head_token(body) == "human"
    assert stage1_skips(body)
    # A cached ``human`` replays as ``NEXT: human`` and keeps parking.
    assert stage1_skips("NEXT: human", posted_at=CACHED_TOKEN_POSTED_AT)


@pytest.mark.anyio
async def test_the_conductor_stops_the_new_form_on_operator_work() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=HUMAN_TASK)
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["looks fine\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []  # no Obj2 forced consult: it is not a decision
    assert outcome.stop_reason is StopReason.OPERATOR_WORK
    assert mcp.posts == []


@pytest.mark.anyio
async def test_the_conductor_refuses_a_broken_new_form_with_the_new_form_quoted() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="x\n\nHUMAN-TASK: restart it\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert len(mcp.posts) == 1
    notice = mcp.posts[0]["content"]
    assert "HUMAN-TASK: <the concrete work, one line>" in notice
    assert notice.rstrip().endswith("NEXT: human")
    assert stage1_skips(notice)  # the notice parks the sweep


def test_the_pr_event_relay_writes_the_new_form() -> None:
    block = operator_handoff("T-x")
    assert f"NEXT: {OPERATOR_TOKEN}" not in block
    got = resolve_handoff(f"PR event\n\n{block}", {})
    assert got.token == "human" and got.human_ask is HumanAsk.OPERATOR_WORK


def test_1c_treats_a_new_form_tail_as_idle_like_an_operator_tail() -> None:
    def gate(body: str) -> object:
        return tail_decision(body, repo="a/b", number=1, roster=_ROSTER)

    assert isinstance(gate(HUMAN_TASK), Post)
    assert isinstance(gate(BODY_OPERATOR), Post)
    assert gate(PLAIN_HUMAN) is AutoGateNoop.QUEUED_BEHIND


def test_the_prompt_teaches_the_new_form_and_retires_operator() -> None:
    block = build_handoff_protocol_block(Role.PROPOSER)
    assert HUMAN_TASK_FORM_EXAMPLE.splitlines()[0] in block
    assert "OPERATOR-TASK:" not in block
    assert f"`NEXT: {OPERATOR_TOKEN}` is retired" in block
    notice = build_roster_notice(_ROSTER)
    assert notice is not None and OPERATOR_TOKEN not in notice


# --------------------------------------------------------------------------- the operator cutoff


def test_the_operator_cutoff_ships_unset() -> None:
    assert handoff_mod.OPERATOR_MALFORMED_AFTER is None


@pytest.mark.parametrize("body", [BODY_OPERATOR, BARE_OPERATOR, HUMAN_TASK, PLAIN_HUMAN])
@pytest.mark.parametrize("posted_at", [None, JUST_BEFORE, CUTOFF, AFTER, NAIVE])
def test_no_cutoff_changes_nothing(body: str, posted_at: datetime | None) -> None:
    assert resolve_handoff(body, _ROSTER, posted_at=posted_at) == resolve_handoff(body, _ROSTER)
    assert not operator_refused_after_cutoff(body, posted_at=posted_at)
    assert parse_head_token(body, posted_at=posted_at) == parse_head_token(body)


def test_body_operator_before_and_from_the_cutoff() -> None:
    before = _resolve(BODY_OPERATOR, posted_at=JUST_BEFORE)
    assert before == resolve_handoff(BODY_OPERATOR, _ROSTER)
    assert before.human_ask is HumanAsk.OPERATOR_WORK
    for posted_at in (CUTOFF, AFTER, None, NAIVE):
        got = _resolve(BODY_OPERATOR, posted_at=posted_at)
        assert got.kind is HandoffKind.ABSENT
        assert got.malformed is MalformedHandoff.OPERATOR_RETIRED
        assert got.operator_fault is None


def test_retirement_is_decided_before_the_form_checks() -> None:
    # Before the cutoff a bare / contradictory operator stands down on its form fault ...
    assert _resolve(BARE_OPERATOR, posted_at=JUST_BEFORE).operator_fault is OperatorFault.NO_TASK
    conflict = "x\nTIER-C: cost\nOPERATOR-TASK: a\nTIER-C-CHECK: none\nNEXT: operator"
    assert _resolve(conflict, posted_at=JUST_BEFORE).operator_fault is (
        OperatorFault.TIER_C_CONFLICT
    )
    # ... and from it, on the retirement, ahead of every fault.
    for body in (BARE_OPERATOR, conflict):
        got = _resolve(body, posted_at=CUTOFF)
        assert got.malformed is MalformedHandoff.OPERATOR_RETIRED
        assert got.operator_fault is None


def test_field_only_operator_before_and_from_the_cutoff() -> None:
    # Before C: the existing NO_TASK stand-down (test_field_only_operator_stands_down_with_the_
    # missing_task_reason). From C, and at an unknown time: retired, not NO_TASK.
    before = _resolve(FIELD_ONLY_BODY, next_participant="operator", posted_at=JUST_BEFORE)
    assert before.operator_fault is OperatorFault.NO_TASK and before.malformed is None
    for posted_at in (CUTOFF, None, NAIVE):
        got = _resolve(FIELD_ONLY_BODY, next_participant="operator", posted_at=posted_at)
        assert got.malformed is MalformedHandoff.OPERATOR_RETIRED, posted_at
        assert got.operator_fault is None


@pytest.mark.parametrize("posted_at", [CUTOFF, AFTER, None])
def test_only_the_retired_token_is_refused(posted_at: datetime | None) -> None:
    """msg-8943 §2: the same instant splits ①② (accepted) from ③ (retired)."""
    # ① the HUMAN-TASK form, ② a plain NEXT: human: as without a cutoff.
    new_form = _resolve(HUMAN_TASK, posted_at=posted_at)
    assert new_form.human_ask is HumanAsk.OPERATOR_WORK and new_form.malformed is None
    plain = _resolve(PLAIN_HUMAN, posted_at=posted_at)
    assert plain.kind is HandoffKind.HUMAN and plain.malformed is None
    # ③ the body form and the field-only form of the retired word.
    assert _resolve(BODY_OPERATOR, posted_at=posted_at).malformed is (
        MalformedHandoff.OPERATOR_RETIRED
    )
    field_only = _resolve(FIELD_ONLY_BODY, next_participant="operator", posted_at=posted_at)
    assert field_only.malformed is MalformedHandoff.OPERATOR_RETIRED
    # The roster-free reading agrees, body only.
    kw: dict[str, Any] = {"posted_at": posted_at, "operator_malformed_after": CUTOFF}
    assert not operator_refused_after_cutoff(HUMAN_TASK, **kw)
    assert not operator_refused_after_cutoff(PLAIN_HUMAN, **kw)
    assert operator_refused_after_cutoff(BODY_OPERATOR, **kw)
    assert not operator_refused_after_cutoff(FIELD_ONLY_BODY, **kw)


def test_the_none_cutoff_does_not_retire_operator() -> None:
    got = resolve_handoff(BODY_OPERATOR, _ROSTER, posted_at=AFTER, none_malformed_after=CUTOFF)
    assert got.human_ask is HumanAsk.OPERATOR_WORK


# -------------------------------------------------------------------------- head_skip, cut on


@pytest.fixture
def cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PR-3 state: the constant set (the readers that take no explicit cutoff)."""
    monkeypatch.setattr(handoff_mod, "OPERATOR_MALFORMED_AFTER", CUTOFF)


@pytest.mark.usefixtures("cut")
def test_head_skip_parks_before_and_launches_from_the_cutoff() -> None:
    now = AFTER + timedelta(minutes=5)
    # Before C a well-formed operator head parks, read with its own posting time (not as
    # "unknown", which the cutoff would retire).
    assert parse_head_token(BODY_OPERATOR, posted_at=JUST_BEFORE) == "operator"
    assert stage1_skips(BODY_OPERATOR, posted_at=JUST_BEFORE)
    before = decide(
        now=now,
        head_msg_id="m1",
        head_body=BODY_OPERATOR,
        control_state="run",
        record=None,
        posted_at=JUST_BEFORE,
    )
    assert before.decision is Decision.SKIP and before.reason == "stop-token"
    # From C it is UNRESOLVED and launches, so the conductor can post the notice.
    for posted_at in (CUTOFF, AFTER, None):
        assert parse_head_token(BODY_OPERATOR, posted_at=posted_at) == ""
        assert not stage1_skips(BODY_OPERATOR, posted_at=posted_at)
    after = decide(
        now=now,
        head_msg_id="m1",
        head_body=BODY_OPERATOR,
        control_state="run",
        record=None,
        posted_at=CUTOFF,
    )
    assert after.decision is Decision.LAUNCH
    # The new form and a plain human are untouched at the same instant.
    assert stage1_skips(HUMAN_TASK, posted_at=CUTOFF)
    assert stage1_skips(PLAIN_HUMAN, posted_at=CUTOFF)


@pytest.mark.usefixtures("cut")
def test_a_cached_token_is_not_cut_twice() -> None:
    # The head cache keeps only the token. ``operator`` is never served from it (its verdict
    # needs the body), a retired head is cached as ``""`` and replays UNRESOLVED, and a cached
    # ``human`` (the new form, or a plain human) replays with no time of its own and keeps parking.
    assert OPERATOR_TOKEN in BODY_DEPENDENT_STOP_TOKENS
    assert parse_head_token(BODY_OPERATOR, posted_at=CUTOFF) == ""
    assert parse_head_token("NEXT: ", posted_at=CACHED_TOKEN_POSTED_AT) == ""
    assert stage1_skips("NEXT: human", posted_at=CACHED_TOKEN_POSTED_AT)


def _stamped(body: str, *, field: str | None = None) -> _FakeChatroomMcp:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=body, next_participant=field)
    return mcp


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "field"),
    [(BODY_OPERATOR, None), (FIELD_ONLY_BODY, "operator")],
    ids=["body", "field-only"],
)
async def test_two_ticks_launch_once_and_post_one_notice(
    monkeypatch: pytest.MonkeyPatch, body: str, field: str | None
) -> None:
    # The fake chatroom stamps every message 2026-06-07; put the cutoff before it.
    cutoff = datetime(2026, 6, 1, tzinfo=UTC)
    stamp = datetime(2026, 6, 7, tzinfo=UTC)
    monkeypatch.setattr(handoff_mod, "OPERATOR_MALFORMED_AFTER", cutoff)
    now = stamp + timedelta(minutes=5)

    # Tick 1: the sweep launches the head (the body is UNRESOLVED either way) ...
    tick1 = decide(
        now=now,
        head_msg_id="m1",
        head_body=body,
        control_state="run",
        record=None,
        posted_at=stamp,
    )
    assert tick1.decision is Decision.LAUNCH
    # ... and the conductor stops on it with exactly one notice.
    mcp = _stamped(body, field=field)
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["looks fine\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.MALFORMED_HANDOFF
    notice = malformed_notice(MalformedHandoff.OPERATOR_RETIRED)
    assert [p["content"] for p in mcp.posts] == [notice]
    assert "HUMAN-TASK: <the concrete work, one line>" in notice

    # Tick 2: the head is the notice (it ends ``NEXT: human``), so the sweep parks it: one
    # launch and one notice in all.
    record = commit_launch(now=now, head_msg_id="m1", verdict=tick1, control_state="run")
    tick2 = decide(
        now=now + timedelta(minutes=20),
        head_msg_id="m2",
        head_body=notice,
        control_state="run",
        record=record,
        posted_at=stamp,
    )
    assert tick2.decision is Decision.SKIP and tick2.reason == "stop-token"


# -------------------------------------------------------------------------------- D2: Fermi


def _scoped(body: str, field: str | None, owner: str | None) -> tuple[Handoff, Handoff]:
    """The handoff the conductor resolves, and what the D2 scope makes of it (core.py's call)."""
    handoff = resolve_handoff(body, _ROSTER, next_participant=field)
    return handoff, scope_fermi_handoff(
        handoff, thread_owner=owner, body=body, next_participant=field
    )


# Fermi named on one side or both. The last five disagree with the other side (PR #562 gate,
# finding 1). In the first three of those, reconciliation alone hides Fermi: Fermi is not on the
# roster, so a body Fermi resolves ABSENT and any resolvable field -- ``human``, a persona, a
# ``pr-review`` -- wins quietly over it. The last two -- a Fermi
# field over another body target -- kept Fermi as the reconciled token already; they are here so
# that stays true by the scope's own reading of the field, not by _reconcile's choice of token.
_FERMI_REQUESTS = [
    ("x\n\nNEXT: Fermi", None),
    ("x\n\nNEXT: fermi (確認)", None),
    (FIELD_ONLY_BODY, "Fermi"),
    ("x\n\nNEXT: Fermi", "Fermi"),
    ("x\n\nNEXT: Fermi", "human"),
    ("x\n\nNEXT: Fermi", "Bohr"),
    ("x\n\nNEXT: Fermi", "pr-review a/b#1"),
    ("x\n\nNEXT: Bohr", "Fermi"),
    (PLAIN_HUMAN, "Fermi"),
]
_HIDDEN_BY_RECONCILIATION = _FERMI_REQUESTS[4:7]


@pytest.mark.parametrize("owner", ["Fermi", "fermi", "FERMI"])
@pytest.mark.parametrize(("body", "field"), _FERMI_REQUESTS)
def test_fermi_on_a_fermi_thread_is_unchanged(owner: str, body: str, field: str | None) -> None:
    handoff, scoped = _scoped(body, field, owner)
    assert scoped == handoff


@pytest.mark.parametrize("owner", ["Bohr", "operator", "human", "", None])
@pytest.mark.parametrize(("body", "field"), _FERMI_REQUESTS)
def test_fermi_elsewhere_is_malformed(owner: str | None, body: str, field: str | None) -> None:
    _, scoped = _scoped(body, field, owner)
    assert scoped.kind is HandoffKind.ABSENT
    assert scoped.malformed is MalformedHandoff.FERMI_OUTSIDE_OWN_THREAD


def test_the_disagreement_cases_really_lose_fermi_in_reconciliation() -> None:
    """Pins why the raw sides are read: the reconciled handoff alone no longer names Fermi."""
    for body, field in _HIDDEN_BY_RECONCILIATION:
        handoff = resolve_handoff(body, _ROSTER, next_participant=field)
        target = handoff.identity or handoff.token or ""
        assert handoff.kind is HandoffKind.PR_REVIEW or target.casefold() != "fermi", (body, field)
    # The worst of them: Fermi is not on the roster, so the body resolves ABSENT and a ``human``
    # field wins quietly -- reading as the author's own request of the human, exactly the silent
    # reinterpretation §7.1 forbids. The scope refuses it instead.
    handoff = resolve_handoff("x\n\nNEXT: Fermi", _ROSTER, next_participant="human")
    assert handoff.kind is HandoffKind.HUMAN and handoff.author_requested_human


@pytest.mark.parametrize(
    "body", ["x\n\nNEXT: Bohr", PLAIN_HUMAN, HUMAN_TASK, "x\n\nNEXT: pr-review a/b#1", "x"]
)
@pytest.mark.parametrize("field", [None, "Bohr", "human"])
def test_other_targets_are_not_scoped(body: str, field: str | None) -> None:
    for owner in ("Bohr", None):
        handoff, scoped = _scoped(body, field, owner)
        assert scoped == handoff


def test_an_earlier_fermi_line_is_not_the_handoff() -> None:
    """Only the last ``NEXT:`` line is the request, here as in the resolver."""
    handoff, scoped = _scoped("x\nNEXT: Fermi\nlater\n\nNEXT: Bohr", None, "Bohr")
    assert scoped == handoff and handoff.kind is HandoffKind.ROLE


def test_an_existing_refusal_is_kept() -> None:
    """A head already refused keeps its own notice; a Fermi field does not replace it."""
    handoff, scoped = _scoped("x\n\nNEXT: operator", "Fermi", "Bohr")
    assert handoff.kind is HandoffKind.ABSENT and handoff.operator_fault is OperatorFault.NO_TASK
    assert scoped == handoff


def test_a_bad_field_does_not_hide_a_fermi_body() -> None:
    """An unresolvable field escalates to HUMAN; the body's Fermi is still refused."""
    _, scoped = _scoped("x\n\nNEXT: Fermi", "no-such-persona", "Bohr")
    assert scoped.malformed is MalformedHandoff.FERMI_OUTSIDE_OWN_THREAD


class _OwnedChatroomMcp(_FakeChatroomMcp):
    """The fake chatroom, with ``thread.owner`` on the read (``None`` = the key is absent)."""

    def __init__(self, owner: str | None) -> None:
        super().__init__()
        self._owner = owner

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await super().call_tool(name, arguments)
        if name == "chatroom_get_thread" and self._owner is not None:
            result["thread"] = {"owner": self._owner, "tags": []}
        return result


@pytest.mark.anyio
async def test_the_conductor_keeps_the_spawn_blocked_stop_on_a_fermi_thread() -> None:
    mcp = _OwnedChatroomMcp("Fermi")
    mcp.seed(author="Bohr", content="x\n\nNEXT: Fermi")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    # The spawn-blocked stop has its own reason since T-next-names-unspawnable-identity PR-1
    # (#564; it was ``HUMAN``). Merged next to this PR, so this test met it only on main.
    assert outcome.stop_reason is StopReason.UNSPAWNABLE_TARGET
    assert all("fermi_outside_own_thread" not in p["content"] for p in mcp.posts)


@pytest.mark.anyio
@pytest.mark.parametrize("owner", ["Bohr", None])
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("x\n\nNEXT: Fermi", None),
        ("x", "Fermi"),
        # Disagreement (PR #562 gate, finding 1): reconciliation would make each of these a HUMAN
        # or a Bohr handoff, so only reading the raw sides catches the Fermi in them.
        ("x\n\nNEXT: Fermi", "human"),
        ("x\n\nNEXT: Fermi", "Bohr"),
        ("x\n\nNEXT: Bohr", "Fermi"),
    ],
)
async def test_the_conductor_refuses_fermi_outside_its_thread(
    owner: str | None, body: str, field: str | None
) -> None:
    mcp = _OwnedChatroomMcp(owner)
    mcp.seed(author="Bohr", content=body, next_participant=field)
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["looks fine\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.MALFORMED_HANDOFF
    notice = malformed_notice(MalformedHandoff.FERMI_OUTSIDE_OWN_THREAD)
    assert [p["content"] for p in mcp.posts] == [notice]
    assert stage1_skips(notice)
    assert "NEXT: human" in notice and "HUMAN-TASK:" in notice


def _conductor(mcp: _FakeChatroomMcp, disp: _ScriptedDispatcher) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
    )
