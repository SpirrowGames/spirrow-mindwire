"""T-role-body-field-divergence-relaunch — a head head_skip LAUNCHes always gets moved.

Design: Bohr msg-5855 (table + D1-D3), corrected in msg-5857 (N1 = 14 cells, N2 = 4 cells, T6 /
T7 added), summarised in msg-5859; approved by Einstein msg-5858 and msg-5859's endorsement.

head_skip reads only the body's ``NEXT:``; the conductor routes on the ``next_participant`` field
first. When the field stops the run and the body is not a stop token, nothing used to be posted,
so the same head was relaunched every tick (CAP 60 min) forever. What is pinned here:

- T1: every (body x field x consulted) combination head_skip LAUNCHes ends with a spawn, the PR
  gate, or a final post head_skip SKIPs.
- T2: the N1 cells (field/body divergence, unresolvable field) post one marked stand-down.
- T3 / T6: the N2 cells ((c)/(d) x {human, none}) copy the field's verdict, unmarked.
- T4: an N1 notice that does not land exits non-zero; an N2 one does not.
- T5: ``stage1_skips`` is exactly Stage 1 of ``decide``.
- T7: a whitespace-only field is no field: (d) stays a silent NO_HANDOFF (out of scope).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.handoff import (
    OPERATOR_FORM_EXAMPLE,
    HandoffKind,
    MismatchReason,
    resolve_handoff,
)
from spirrow_mindwire.conductor.head_skip import Decision, decide, stage1_skips
from spirrow_mindwire.conductor.parked_lane import ParkedLane, classify_parked
from spirrow_mindwire.conductor.stand_down import (
    EVENT_KIND_STAND_DOWN,
    StandDownError,
    StandDownReason,
    UnresolvedItem,
)
from spirrow_mindwire.conductor.stop_marker import parse_stop_marker
from spirrow_mindwire.magickit.client import ThreadResolvedError
from spirrow_mindwire.value_objects import Role

_T0 = datetime(2026, 10, 2, tzinfo=UTC)

# A second proposer gives the "another persona" field a target that is neither the author (a
# self-handoff) nor the implementer (guard (i)), so each cell exercises the field/body question
# alone. The naysayer stays the unique holder of its role, so ``NEXT: naysayer`` is a role alias.
_ROSTER: Mapping[str, Role] = {
    "Bohr": Role.PROPOSER,
    "Pauli": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}
_AUTHOR = "Bohr"

# The body kinds of msg-5855 §1 that head_skip LAUNCHes ((e), malformed operator, is #402's).
_BODIES: dict[str, str] = {
    "persona": "design\n\nNEXT: Einstein",
    "role_alias": "design\n\nNEXT: naysayer",
    "pr_review": "done\n\nNEXT: pr-review acme/widgets#7",
    "unresolvable": "x\n\nNEXT: Bohrr",
    "no_next": "prose only",
}
# The field the body names, so "agrees" exists for every row that names a target.
_AGREEING_FIELD: dict[str, str] = {
    "persona": "Einstein",
    "role_alias": "Einstein",
    "pr_review": "pr-review acme/widgets#7",
}
_FIELDS: dict[str, str] = {
    "human": "human",
    "none": "none",
    "operator": "operator",
    "other_persona": "Pauli",
    "other_pr_review": "pr-review acme/widgets#9",
    "unresolvable": "Schrodinger",
}

_NAYSAYER_REPLY = _attested("review\n\nNEXT: human")


def _decide(body: str) -> Decision:
    return decide(
        now=_T0, head_msg_id="msg-1", head_body=body, control_state="run", record=None
    ).decision


def _seed(mcp: _FakeChatroomMcp, *, body: str, field: str, consulted: bool) -> None:
    if consulted:
        mcp.seed(author="Einstein", content=_attested("review\n\nNEXT: Bohr"))
    mcp.seed(author=_AUTHOR, content=body, next_participant=field)


def _run_parts(
    mcp: _FakeChatroomMcp,
) -> tuple[Conductor, _ScriptedDispatcher]:
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.NAYSAYER: [_NAYSAYER_REPLY] * 3,
            Role.PROPOSER: ["ok\n\nNEXT: human"] * 3,
            Role.IMPLEMENTER: ["ok\n\nNEXT: human"] * 3,
        },
    )
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
    )
    return conductor, disp


def _all_cells() -> list[tuple[str, str]]:
    cells: list[tuple[str, str]] = []
    for body_id in _BODIES:
        fields = dict(_FIELDS)
        if body_id in _AGREEING_FIELD:
            fields["agrees"] = _AGREEING_FIELD[body_id]
        cells.extend((body_id, field_id) for field_id in fields)
    return cells


def _field_value(body_id: str, field_id: str) -> str:
    return _AGREEING_FIELD[body_id] if field_id == "agrees" else _FIELDS[field_id]


# --------------------------------------------------------------------------- #
# T1 — the invariant over the whole grid
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize("consulted", [True, False], ids=["consulted", "unconsulted"])
@pytest.mark.parametrize(("body_id", "field_id"), _all_cells())
async def test_every_launched_head_is_moved(body_id: str, field_id: str, consulted: bool) -> None:
    body = _BODIES[body_id]
    field = _field_value(body_id, field_id)
    assert _decide(body) is Decision.LAUNCH  # the precondition the whole grid stands on
    mcp = _FakeChatroomMcp()
    _seed(mcp, body=body, field=field, consulted=consulted)
    conductor, disp = _run_parts(mcp)
    outcome = await conductor.run()
    if disp.spawns:
        return  # somebody was started; their post moves the head
    if resolve_handoff(body, _ROSTER, next_participant=field).kind is HandoffKind.PR_REVIEW:
        return  # the PR gate owns this head (no orchestrator is wired in this fixture)
    if outcome.stop_reason is StopReason.CI_WAIT:
        return
    assert mcp.posts, f"silent park: body={body_id} field={field_id} stop={outcome.stop_reason}"
    assert _decide(str(mcp.posts[-1]["content"])) is Decision.SKIP


# --------------------------------------------------------------------------- #
# T2 — N1: the 14 stand-down cells
# --------------------------------------------------------------------------- #

_DIVERGING_FIELDS = ["human", "none", "operator", "other_persona", "other_pr_review"]
_N1_CELLS: list[tuple[str, str, StandDownReason]] = [
    *(
        (body_id, field_id, StandDownReason.FIELD_BODY_DIVERGENCE)
        for body_id in ("persona", "role_alias", "pr_review")
        for field_id in _DIVERGING_FIELDS
    ),
    *(
        (body_id, "unresolvable", StandDownReason.FIELD_UNRESOLVABLE)
        for body_id in ("persona", "role_alias", "pr_review", "unresolvable", "no_next")
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("body_id", "field_id", "reason"), _N1_CELLS)
async def test_n1_posts_one_marked_stand_down(
    body_id: str, field_id: str, reason: StandDownReason
) -> None:
    mcp = _FakeChatroomMcp()
    _seed(mcp, body=_BODIES[body_id], field=_FIELDS[field_id], consulted=True)
    conductor, disp = _run_parts(mcp)
    outcome = await conductor.run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.HUMAN
    (post,) = mcp.posts
    content = str(post["content"])
    marker = parse_stop_marker(content)
    assert marker is not None
    assert marker["kind"] == EVENT_KIND_STAND_DOWN
    assert marker["unresolved"] == UnresolvedItem.IDENTITY.value
    assert marker["reason"] == reason.value
    assert content.rstrip("\n").splitlines()[-1] == "NEXT: human"
    assert outcome.last_msg_id == mcp._messages[-1]["msg_id"]


def test_n1_reason_values_are_the_mismatch_vocabulary() -> None:
    assert StandDownReason.FIELD_BODY_DIVERGENCE.value == MismatchReason.TARGET_DIVERGENCE.value
    assert StandDownReason.FIELD_UNRESOLVABLE.value == MismatchReason.FIELD_UNRESOLVABLE.value


# --------------------------------------------------------------------------- #
# T3 / T6 — N2: the field's verdict copied onto the head
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["human", "none"])
@pytest.mark.parametrize(
    "body",
    ["prose only", "x\n\nNEXT: Bohrr"],
    ids=["d_no_next", "c_unresolvable"],
)
async def test_n2_body_without_a_target_gets_the_field_copied(body: str, field: str) -> None:
    # T6 (msg-5857): ``_next_participant`` reads the FIELD, so (d) x human / none is NOT empty and
    # does get its notice. This is the case msg-5856 doubted; named so it stays pinned.
    mcp = _FakeChatroomMcp()
    _seed(mcp, body=body, field=field, consulted=True)
    conductor, disp = _run_parts(mcp)
    outcome = await conductor.run()
    assert disp.spawns == []
    assert outcome.stop_reason is (StopReason.SETTLED if field == "none" else StopReason.HUMAN)
    (post,) = mcp.posts
    content = str(post["content"])
    assert parse_stop_marker(content) is None
    assert content.rstrip("\n").splitlines()[-1] == f"NEXT: {field}"
    assert _decide(content) is Decision.SKIP
    assert outcome.last_msg_id == mcp._messages[-1]["msg_id"]


# --------------------------------------------------------------------------- #
# T4 — a notice that does not land
# --------------------------------------------------------------------------- #


class _ResolvedOnPostMcp(_FakeChatroomMcp):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_post_message":
            raise ThreadResolvedError("thread resolved")
        return await super().call_tool(name, arguments)


@pytest.mark.anyio
async def test_n1_notice_that_does_not_land_exits_non_zero() -> None:
    mcp = _ResolvedOnPostMcp()
    _seed(mcp, body=_BODIES["persona"], field="human", consulted=True)
    conductor, _ = _run_parts(mcp)
    with pytest.raises(StandDownError) as exc_info:
        await conductor.run()
    assert exc_info.value.event.fields["reason"] == StandDownReason.FIELD_BODY_DIVERGENCE.value


@pytest.mark.anyio
async def test_n2_notice_that_does_not_land_logs_error_and_stops_normally(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp = _ResolvedOnPostMcp()
    _seed(mcp, body="prose only", field="human", consulted=True)
    conductor, _ = _run_parts(mcp)
    with caplog.at_level(logging.ERROR):
        outcome = await conductor.run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert any("field-stop notice did not land" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# T5 — one owner of the Stage 1 boundary
# --------------------------------------------------------------------------- #

_STAGE1_BODIES = [
    *(_BODIES.values()),
    "x\n\nNEXT: human",
    "x\n\nNEXT: none",
    "x\n\nNEXT: Human",
    "x\n\nNEXT: operator",
    f"notes\n\n{OPERATOR_FORM_EXAMPLE}",
    "",
]


@pytest.mark.parametrize("body", _STAGE1_BODIES)
def test_stage1_skips_is_exactly_decide_stage1(body: str) -> None:
    verdict = decide(now=_T0, head_msg_id="msg-1", head_body=body, control_state="run", record=None)
    assert stage1_skips(body) is (verdict.reason == "stop-token")


# --------------------------------------------------------------------------- #
# T7 — a whitespace-only field is no field (NO_HANDOFF stays out of scope)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_whitespace_field_with_no_next_stays_a_silent_no_handoff() -> None:
    mcp = _FakeChatroomMcp()
    _seed(mcp, body="prose only", field="  ", consulted=True)
    conductor, disp = _run_parts(mcp)
    outcome = await conductor.run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert mcp.posts == []


# --------------------------------------------------------------------------- #
# Unchanged: a well-formed stop-token body is not touched (head_skip SKIPs it already)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_stop_token_body_with_a_diverging_field_posts_nothing() -> None:
    mcp = _FakeChatroomMcp()
    _seed(mcp, body="x\n\nNEXT: human", field="none", consulted=True)
    conductor, _ = _run_parts(mcp)
    outcome = await conductor.run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert mcp.posts == []


# --------------------------------------------------------------------------- #
# Follow-up (msg-6047) — N1 notices land in the board's misroute lane
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body_id", "field_id", "reason"),
    [
        ("persona", "other_persona", StandDownReason.FIELD_BODY_DIVERGENCE),
        ("persona", "unresolvable", StandDownReason.FIELD_UNRESOLVABLE),
    ],
    ids=["div", "unres"],
)
async def test_n1_notice_is_classified_as_a_misroute(
    body_id: str, field_id: str, reason: StandDownReason
) -> None:
    mcp = _FakeChatroomMcp()
    _seed(mcp, body=_BODIES[body_id], field=_FIELDS[field_id], consulted=True)
    conductor, _ = _run_parts(mcp)
    await conductor.run()
    (post,) = mcp.posts
    content = str(post["content"])
    marker = parse_stop_marker(content)
    assert marker is not None and marker["reason"] == reason.value
    lane = classify_parked(content)
    assert lane.lane is ParkedLane.MISROUTE
    assert lane.protocol_violation is False
