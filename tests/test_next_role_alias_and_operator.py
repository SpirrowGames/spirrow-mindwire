"""T-next-role-name-stands-down-to-human — role-name handoffs and ``NEXT: operator``.

Design: Bohr msg-5416 (D1-D7), revised in msg-5418 (D4' / D4'-a / D6'), msg-5422, msg-5426
(D4''' / D6''') and msg-5428 (D6'''' + D7 supplement); approved by Einstein msg-5429.

What is pinned here:

- D1 / D3: a role name resolves to the one roster identity holding it; zero or several holders
  stand down as ``identity_role_ambiguous``; identity names always win; the answer follows the
  roster, not a fixed map.
- D2: guard (i), the self-handoff check and the spawnability check see the resolved identity.
- D4'-a: the protocol text carries the operator keywords and ``NEXT: operator``, and its example
  resolves to ``OPERATOR_WORK``.
- D6'''': the four tests msg-5428 lists, plus the missing-task / missing-check stand-downs.
- D7: the board lane of each park, including the conflict stand-down staying a decision.
- D5: a valid ``NEXT: operator`` head parks in head-skip like ``human``; a malformed one launches.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.handoff import (
    OPERATOR_FORM_EXAMPLE,
    OPERATOR_TASK_KEYWORD,
    OPERATOR_TOKEN,
    TIER_C_CHECK_KEYWORD,
    TIER_C_CHECK_NONE,
    HandoffKind,
    HumanAsk,
    MismatchReason,
    OperatorFault,
    build_handoff_protocol_block,
    resolve_handoff,
)
from spirrow_mindwire.conductor.head_skip import Decision, decide
from spirrow_mindwire.conductor.parked_lane import ParkedLane, classify_parked
from spirrow_mindwire.conductor.stand_down import StandDownReason
from spirrow_mindwire.conductor.stop_marker import parse_stop_marker
from spirrow_mindwire.value_objects import Role

_VALID_OPERATOR = f"setup notes\n\n{OPERATOR_FORM_EXAMPLE}"


def _conductor(
    mcp: _FakeChatroomMcp,
    dispatcher: _ScriptedDispatcher,
    roster: Mapping[str, Role] = _ROSTER,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=roster,
        naysayer_identity="Einstein",
    )


# --------------------------------------------------------------------------- #
# D1 / D3 — role name → identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("token", ["implementer", "Implementer", "IMPLEMENTER"])
def test_role_name_resolves_to_its_unique_holder(token: str) -> None:
    handoff = resolve_handoff(f"done\n\nNEXT: {token}", _ROSTER)
    assert handoff.kind is HandoffKind.ROLE
    assert (handoff.identity, handoff.role) == ("Heisenberg", Role.IMPLEMENTER)
    assert handoff.via_role_alias is True


def test_identity_name_does_not_set_the_alias_flag() -> None:
    handoff = resolve_handoff("done\n\nNEXT: Heisenberg", _ROSTER)
    assert handoff.identity == "Heisenberg" and handoff.via_role_alias is False


def test_identity_name_wins_over_a_role_name_spelled_the_same() -> None:
    roster: Mapping[str, Role] = {"implementer": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}
    handoff = resolve_handoff("x\n\nNEXT: implementer", roster)
    assert (handoff.identity, handoff.role) == ("implementer", Role.NAYSAYER)
    assert handoff.via_role_alias is False


def test_resolution_follows_the_roster_not_a_fixed_map() -> None:
    # Same role, different persona in another project's roster → a different identity.
    other: Mapping[str, Role] = {"Feynman": Role.IMPLEMENTER, "Bohr": Role.PROPOSER}
    assert resolve_handoff("x\n\nNEXT: implementer", _ROSTER).identity == "Heisenberg"
    assert resolve_handoff("x\n\nNEXT: implementer", other).identity == "Feynman"


@pytest.mark.parametrize(
    "roster",
    [
        {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER},  # zero holders
        {"Heisenberg": Role.IMPLEMENTER, "Feynman": Role.IMPLEMENTER},  # two holders
    ],
    ids=["zero", "two"],
)
def test_role_name_without_a_unique_holder_is_absent_and_flagged(
    roster: Mapping[str, Role],
) -> None:
    handoff = resolve_handoff("x\n\nNEXT: implementer", roster)
    assert handoff.kind is HandoffKind.ABSENT
    assert handoff.role_alias_unresolved is True


def test_typo_is_not_flagged_as_a_role_alias() -> None:
    handoff = resolve_handoff("x\n\nNEXT: Bohrr", _ROSTER)
    assert handoff.kind is HandoffKind.ABSENT and handoff.role_alias_unresolved is False


def test_field_and_body_resolve_role_names_the_same_way() -> None:
    # One parser for both routes: a field role name agreeing with a body identity is agreement.
    handoff = resolve_handoff("x\n\nNEXT: Heisenberg", _ROSTER, next_participant="implementer")
    assert handoff.kind is HandoffKind.ROLE and handoff.identity == "Heisenberg"
    assert handoff.mismatch_reason is None


# --------------------------------------------------------------------------- #
# D2 — the existing checks see the resolved identity
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_guard_i_still_gates_a_proposer_role_name_handoff() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: implementer")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert all(role is not Role.IMPLEMENTER for role, _ in disp.dispatches)
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_self_handoff_through_a_role_name_is_caught() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="done\n\nNEXT: implementer")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.SELF_HANDOFF


@pytest.mark.anyio
async def test_spawn_blocked_applies_to_the_resolved_identity() -> None:
    # The implementer here is a web identity (ADR-2026-09-14-21): reaching it by role name must
    # hit the same spawnability stop as naming it.
    roster: Mapping[str, Role] = {
        "Bohr": Role.PROPOSER,
        "Fermi": Role.IMPLEMENTER,
        "Einstein": Role.NAYSAYER,
    }
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="x\n\nNEXT: implementer")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, roster).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.HUMAN
    (post,) = mcp.posts
    assert "Fermi" in post["content"]


@pytest.mark.anyio
async def test_role_name_handoff_dispatches_and_logs_the_alias(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: proposer")
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["revised\n\nNEXT: none"]})
    await _conductor(mcp, disp).run()
    assert disp.spawns == [(Role.PROPOSER, "Bohr")]
    assert any(
        "conductor.handoff.role_alias" in r.getMessage() and "identity=Bohr" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.anyio
async def test_ambiguous_role_name_stands_down_with_its_own_reason() -> None:
    roster: Mapping[str, Role] = {**_ROSTER, "Feynman": Role.IMPLEMENTER}
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: implementer")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, roster).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    (post,) = mcp.posts
    marker = parse_stop_marker(post["content"])
    assert marker is not None
    assert marker["reason"] == StandDownReason.IDENTITY_ROLE_AMBIGUOUS.value


# --------------------------------------------------------------------------- #
# D4'-a — one source for the prompt and the parser
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(Role))
def test_protocol_teaches_the_operator_form_from_the_constants(role: Role) -> None:
    block = build_handoff_protocol_block(role)
    assert f"NEXT: {OPERATOR_TOKEN}" in block
    assert f"{OPERATOR_TASK_KEYWORD}:" in block
    assert f"{TIER_C_CHECK_KEYWORD}: {TIER_C_CHECK_NONE}" in block
    for line in OPERATOR_FORM_EXAMPLE.splitlines():
        assert line in block
    assert "one of three reserved words" in block
    assert "not a role name" in block


def test_protocol_does_not_list_kinds_of_work_as_operator_examples() -> None:
    # D4'' item 1 (msg-5422): Tier-C is decided by its four types, not by the kind of work.
    block = build_handoff_protocol_block(Role.IMPLEMENTER)
    operator_part = block.split(f"`NEXT: {OPERATOR_TOKEN}` —", 1)[1]
    assert "secret" not in operator_part.lower()
    assert "host" not in operator_part.lower()


def test_the_taught_example_resolves_to_operator_work() -> None:
    handoff = resolve_handoff(_VALID_OPERATOR, {})
    assert handoff.kind is HandoffKind.HUMAN
    assert handoff.human_ask is HumanAsk.OPERATOR_WORK
    assert handoff.operator_task == "<the concrete work, one line>"
    # Operator work is not a Tier-C request: it must not enter the Decider's Tier-C entry.
    assert handoff.author_requested_human is False


# --------------------------------------------------------------------------- #
# D6'''' — the operator checks, in order
# --------------------------------------------------------------------------- #


def test_msg5428_case1_three_lines_are_operator_work() -> None:
    assert resolve_handoff(_VALID_OPERATOR, _ROSTER).human_ask is HumanAsk.OPERATOR_WORK


def test_msg5428_case2_tier_c_plus_the_three_lines_is_a_conflict() -> None:
    handoff = resolve_handoff(f"TIER-C: irreversible\n\n{OPERATOR_FORM_EXAMPLE}", _ROSTER)
    assert handoff.kind is HandoffKind.ABSENT
    assert handoff.operator_fault is OperatorFault.TIER_C_CONFLICT


def test_msg5428_case3_conflict_is_reported_before_missing_lines() -> None:
    handoff = resolve_handoff("TIER-C: cost\nNEXT: operator", _ROSTER)
    assert handoff.operator_fault is OperatorFault.TIER_C_CONFLICT


def test_msg5428_case4_decided_msg_ref_is_refused() -> None:
    # Regression: the ``decided <msg-ref>`` form removed in msg-5426 must not come back.
    body = "OPERATOR-TASK: rotate it\nTIER-C-CHECK: decided msg-x\nNEXT: operator"
    assert resolve_handoff(body, _ROSTER).operator_fault is OperatorFault.NO_TIER_C_CHECK


@pytest.mark.parametrize(
    ("body", "fault"),
    [
        ("TIER-C-CHECK: none\nNEXT: operator", OperatorFault.NO_TASK),
        ("OPERATOR-TASK:\nTIER-C-CHECK: none\nNEXT: operator", OperatorFault.NO_TASK),
        ("OPERATOR-TASK: do it\nNEXT: operator", OperatorFault.NO_TASK),
        ("OPERATOR-TASK: do it\n\nNEXT: operator", OperatorFault.NO_TIER_C_CHECK),
        ("NEXT: operator", OperatorFault.NO_TASK),
    ],
)
def test_malformed_operator_forms_stand_down(body: str, fault: OperatorFault) -> None:
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.kind is HandoffKind.ABSENT and handoff.operator_fault is fault


def test_tier_c_check_elsewhere_does_not_satisfy_the_line_above_next() -> None:
    # PR #402 gate finding 1 (pinned): ``TIER-C-CHECK: none`` quoted at the top of the message,
    # the task two lines up, and an unrelated line directly above ``NEXT: operator``. G2 reads
    # only the line directly above the final ``NEXT:`` (``declares_no_tier_c`` ->
    # ``_line_above_last_next``), so this must stand down, never be accepted.
    body = "TIER-C-CHECK: none\nOPERATOR-TASK: re-arm the hook\ngarbage line\nNEXT: operator"
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.kind is HandoffKind.ABSENT
    assert handoff.operator_fault is OperatorFault.NO_TIER_C_CHECK
    assert handoff.human_ask is None


def test_field_only_operator_stands_down_with_the_missing_task_reason() -> None:
    # PR #402 gate finding 2 (pinned): field ``operator`` and a body with no ``NEXT:`` line.
    # ``_reconcile`` row 3 (body ABSENT -> field wins) keeps ``OPERATOR_WORK``, so the 3-line
    # check runs and stands down with ``NO_TASK`` -- not a TARGET_DIVERGENCE park.
    handoff = resolve_handoff("Some prose, no handoff line.", _ROSTER, next_participant="operator")
    assert handoff.kind is HandoffKind.ABSENT
    assert handoff.operator_fault is OperatorFault.NO_TASK
    assert handoff.mismatch_reason is None


def test_operator_fault_values_are_stand_down_reasons() -> None:
    reasons = {r.value for r in StandDownReason}
    assert {f.value for f in OperatorFault} <= reasons


def test_field_human_with_body_operator_is_a_divergence() -> None:
    handoff = resolve_handoff(_VALID_OPERATOR, _ROSTER, next_participant="human")
    assert handoff.mismatch_reason is MismatchReason.TARGET_DIVERGENCE


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "reason", "teaches"),
    [
        (
            "x\n\nTIER-C-CHECK: none\nNEXT: operator",
            StandDownReason.IDENTITY_OPERATOR_NO_TASK,
            OPERATOR_FORM_EXAMPLE.splitlines()[0],
        ),
        (
            "x\n\nOPERATOR-TASK: do it\nNEXT: operator",
            StandDownReason.IDENTITY_OPERATOR_NO_TASK,
            OPERATOR_FORM_EXAMPLE.splitlines()[1],
        ),
        (
            f"TIER-C: cost\n\n{OPERATOR_FORM_EXAMPLE}",
            StandDownReason.OPERATOR_TIER_C_CONFLICT,
            "TIER-C: <type>",
        ),
    ],
    ids=["no-task", "no-check", "conflict"],
)
async def test_operator_stand_down_posts_the_correct_form_and_does_not_spawn(
    body: str, reason: StandDownReason, teaches: str
) -> None:
    mcp = _FakeChatroomMcp()
    # A proposer author: the refused operator handoff must NOT be sent to a forced consult first.
    mcp.seed(author="Bohr", content=body)
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    (post,) = mcp.posts
    content = post["content"]
    assert content.rstrip().splitlines()[-1] == "NEXT: human"
    assert teaches in content
    marker = parse_stop_marker(content)
    assert marker is not None and marker["reason"] == reason.value
    # The notice itself must not declare a Tier-C (it would latch guard (i)'s G1).
    assert "TIER-C: cost" not in content


@pytest.mark.anyio
async def test_valid_operator_parks_like_human() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_VALID_OPERATOR)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.HUMAN
    assert mcp.posts == []


# --------------------------------------------------------------------------- #
# D7 — board lanes
# --------------------------------------------------------------------------- #


async def _notice_for(body: str, roster: Mapping[str, Role] = _ROSTER) -> str:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=body)
    await _conductor(mcp, _ScriptedDispatcher(mcp, {}), roster).run()
    (post,) = mcp.posts
    return str(post["content"])


def test_lane_of_valid_operator_work() -> None:
    lane = classify_parked(_VALID_OPERATOR)
    assert lane.lane is ParkedLane.OPERATOR_WORK
    assert lane.operator_task == "<the concrete work, one line>"


def test_lane_of_a_plain_human_handoff_is_decision() -> None:
    lane = classify_parked("TIER-C: merge-protected\nNEXT: human")
    assert lane.lane is ParkedLane.DECISION and lane.protocol_violation is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        "x\n\nNEXT: Bohrr",
        "x\n\nTIER-C-CHECK: none\nNEXT: operator",
        "x\n\nOPERATOR-TASK: a\nNEXT: operator",
    ],
    ids=["typo", "no-task", "no-check"],
)
async def test_routing_stand_downs_are_misroutes(body: str) -> None:
    lane = classify_parked(await _notice_for(body))
    assert lane.lane is ParkedLane.MISROUTE


@pytest.mark.anyio
async def test_ambiguous_role_stand_down_is_a_misroute() -> None:
    roster: Mapping[str, Role] = {**_ROSTER, "Feynman": Role.IMPLEMENTER}
    lane = classify_parked(await _notice_for("x\n\nNEXT: implementer", roster))
    assert lane.lane is ParkedLane.MISROUTE


@pytest.mark.anyio
async def test_msg5428_case2_conflict_stand_down_stays_in_the_decision_lane() -> None:
    notice = await _notice_for(f"TIER-C: irreversible\n\n{OPERATOR_FORM_EXAMPLE}")
    lane = classify_parked(notice)
    assert lane.lane is ParkedLane.DECISION
    assert lane.protocol_violation is True


@pytest.mark.anyio
async def test_not_spawnable_stand_down_is_not_a_misroute() -> None:
    lane = classify_parked(await _notice_for("x\n\nNEXT: Fermi"))
    assert lane.lane is ParkedLane.DECISION


# --------------------------------------------------------------------------- #
# D5 — head-skip parks a valid operator head, launches a malformed one
# --------------------------------------------------------------------------- #

_T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _decide(body: str) -> Decision:
    return decide(
        now=_T0, head_msg_id="msg-1", head_body=body, control_state="run", record=None
    ).decision


def test_head_skip_parks_a_valid_operator_head() -> None:
    assert _decide(_VALID_OPERATOR) is Decision.SKIP


@pytest.mark.parametrize(
    "body",
    [
        "x\n\nNEXT: operator",
        f"TIER-C: cost\n\n{OPERATOR_FORM_EXAMPLE}",
        "OPERATOR-TASK: a\nTIER-C-CHECK: decided msg-1\nNEXT: operator",
    ],
)
def test_head_skip_launches_a_malformed_operator_head(body: str) -> None:
    # The conductor is what posts the stand-down notice; skipping here would park it silently.
    assert _decide(body) is not Decision.SKIP
