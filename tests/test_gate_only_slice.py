"""Gate-only slice — a gate-lane run stops right before the implementer's turn.

Thread T-sweep-starves-deep-candidates, PR-B. Design: Bohr msg-6313 §1', amended by msg-6316
(slice only when the resume's sources are wired; the next-tick invariant is "run the conductor
again and the implementer is dispatched"); PR-A (#444, the gate resume) is what makes the next
tick pick the sliced head up.

What is pinned (msg-6313 / msg-6316 test list):

1. ``gate_only`` + REQUEST_CHANGES: the relay is posted, the implementer is not spawned, the run
   stops on ``slice_end``.
2. ``gate_only`` + R4: the ci-route post (with its marker) is posted, nothing is spawned, the run
   stops on ``slice_end``.
3. APPROVE / COMMENT / R3 / R5 / R6 / ``ci_wait`` end the same with and without ``gate_only``.
4. Without ``gate_only`` the run still goes on to the implementer (RC and R4).
5. ``ROUND_CAP`` still comes only from ``max_rounds``.
6. Next tick: the sliced head is ``LAUNCH`` in the role lane (never SKIP), and running the
   conductor again dispatches the implementer with no naysayer and no human stop (the msg-6315
   reproduction, as a regression test). After R4, the next red on the same head is R5.
7. Sources not wired → ``conductor.gate_only.ignored reason=resume_unavailable`` and the run goes
   on inline. A relay the resume would not accept (the U3' advisory APPROVE routed to the
   implementer) is not sliced either.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_conductor_core import (
    _GREEN,
    _HEAD,
    _PENDING,
    _RED,
    _ROSTER,
    _check,
    _FakeChatroomMcp,
    _rollup,
    _ScriptedDispatcher,
    _ScriptedRollupSource,
    _thread_ref,
)

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.gate_records import (
    MERGE_WAIT_LINE,
    RELAY_AUTHOR,
    RelayRoute,
    ci_route_heads,
    render_ci_route_marker,
    render_relay_heading,
)
from spirrow_mindwire.conductor.head_skip import (
    TERMINAL_STOP_REASONS,
    Decision,
    commit_launch,
    commit_terminal,
    decide,
)
from spirrow_mindwire.conductor.stop_kind import STOP_KIND_BY_STOP_REASON
from spirrow_mindwire.github.client import CiState, PrRef, ReviewEvent, ReviewInfo
from spirrow_mindwire.naysayer.pr_review import PrReviewOutcome
from spirrow_mindwire.value_objects import Role, ThreadRef

_PR = "acme/widgets#7"
_NEW_HEAD = "2222222222222222222222222222222222222222"
_GATE_LOGIN = "spirrowgames-ops"


class _RelayGate:
    """A PR gate whose relay has production's shape: heading, VERDICT, critique, verdict footer.

    The footer matters here: it is what :func:`~.gate_resume.resume_candidate` reads, and the
    slice applies only to a relay the next tick's resume will accept. ``route`` is given per test
    (the relay writer decides it in production; U3').
    """

    def __init__(self, mcp: _FakeChatroomMcp, verdict: ReviewEvent, route: RelayRoute) -> None:
        self._mcp = mcp
        self._verdict = verdict
        self._route = route
        self.fired: list[str] = []

    async def fire_pr_review(
        self, *, project: str, pr_ref: str, design_thread: str, implementer: str | None = None
    ) -> tuple[ThreadRef, PrReviewOutcome, dict[str, Any]]:
        self.fired.append(pr_ref)
        outcome = PrReviewOutcome(
            verdict=self._verdict, body="critique body", ci_state=CiState.SUCCESS, head_sha=_HEAD
        )
        nxt = implementer if self._route is RelayRoute.IMPLEMENTER and implementer else "human"
        merge_wait = (
            f"{MERGE_WAIT_LINE}\n"
            if self._verdict is ReviewEvent.APPROVE and nxt == "human"
            else ""
        )
        content = (
            f"{render_relay_heading(pr_ref, _HEAD)}\n\n"
            f"VERDICT: {self._verdict.value} (ci=success)\n\n"
            "critique body\n\n"
            f"<!-- mindwire:verdict head_sha={_HEAD} event={self._verdict.value.upper()} -->\n\n"
            f"{merge_wait}NEXT: {nxt}"
        )
        result = await self._mcp.call_tool(
            "chatroom_post_message",
            {
                "project": project,
                "thread_id": design_thread,
                "msg_type": "report",
                "author": RELAY_AUTHOR,
                "content": content,
            },
        )
        msg_id = str(result["msg"]["msg_id"])
        return (
            _thread_ref(),
            outcome,
            {"msg_id": msg_id, "author": RELAY_AUTHOR, "content": content, "route": self._route},
        )


class _Reviews:
    def __init__(self, reviews: list[ReviewInfo]) -> None:
        self.reviews = reviews
        self.asked = 0

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]:
        self.asked += 1
        return list(self.reviews)


def _rc_reviews() -> _Reviews:
    return _Reviews(
        [
            ReviewInfo(
                login=_GATE_LOGIN,
                state="CHANGES_REQUESTED",
                commit_id=_HEAD,
                submitted_at="2026-10-04T00:00:00Z",
            )
        ]
    )


def _opened() -> _FakeChatroomMcp:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"opened the PR\n\nNEXT: pr-review {_PR}")
    return mcp


def _conductor(
    mcp: _FakeChatroomMcp,
    disp: _ScriptedDispatcher,
    *,
    gate: Any,
    rollups: Any,
    reviews: Any = None,
    gate_only: bool = False,
    max_rounds: int = 40,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        orchestrator=gate,
        rollup_source=rollups,
        review_source=reviews,
        review_login=_GATE_LOGIN,
        gate_only=gate_only,
        max_rounds=max_rounds,
    )


def _fixer(mcp: _FakeChatroomMcp) -> _ScriptedDispatcher:
    return _ScriptedDispatcher(
        mcp,
        {
            Role.IMPLEMENTER: [f"fixed and pushed\n\nNEXT: pr-review {_PR}"],
            Role.NAYSAYER: ["review\n\nNEXT: human"],
        },
    )


# --------------------------------------------------------------------------- #
# 1-2. the two slice points
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_gate_only_rc_posts_the_relay_and_stops_on_slice_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp = _opened()
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
    disp = _fixer(mcp)
    with caplog.at_level(logging.INFO):
        outcome = await _conductor(
            mcp,
            disp,
            gate=gate,
            rollups=_ScriptedRollupSource(_rollup(*_GREEN)),
            reviews=_rc_reviews(),
            gate_only=True,
        ).run()
    assert gate.fired == [_PR]
    assert outcome.stop_reason is StopReason.SLICE_END
    assert disp.spawns == [] and disp.dispatches == []
    assert [p["author"] for p in mcp.posts] == [RELAY_AUTHOR]
    assert outcome.last_msg_id == "m2"  # the relay, which stays the head
    assert outcome.rounds == 0
    assert "conductor.gate_only.slice_end" in caplog.text


@pytest.mark.anyio
async def test_gate_only_r4_posts_the_ci_route_and_stops_on_slice_end() -> None:
    mcp = _opened()
    gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN)
    disp = _fixer(mcp)
    outcome = await _conductor(
        mcp,
        disp,
        gate=gate,
        rollups=_ScriptedRollupSource(_rollup(*_RED)),
        reviews=_rc_reviews(),
        gate_only=True,
    ).run()
    assert gate.fired == []
    assert outcome.stop_reason is StopReason.SLICE_END
    assert disp.spawns == [] and disp.dispatches == []
    assert len(mcp.posts) == 1
    assert ci_route_heads([mcp.posts[0]["content"]]) == frozenset({_HEAD})
    assert outcome.last_msg_id == "m2"


# --------------------------------------------------------------------------- #
# 3. every other gate outcome is unchanged by gate_only
# --------------------------------------------------------------------------- #


def _seed_r5(mcp: _FakeChatroomMcp) -> None:
    mcp.seed(
        author=RELAY_AUTHOR,
        content=(
            "earlier route\n\nNEXT: Heisenberg\n\n"
            + render_ci_route_marker(head=_HEAD, conclusion="failure", checks=["CI"])
        ),
    )
    mcp.seed(author="Heisenberg", content=f"tried\n\nNEXT: pr-review {_PR}")


def _seed_r6(mcp: _FakeChatroomMcp) -> None:
    mcp.seed(
        author=RELAY_AUTHOR,
        content=(
            f"{render_relay_heading(_PR, _HEAD)}\n\nVERDICT: approve (ci=success)\n\n"
            "critique\n\nNEXT: human"
        ),
    )
    mcp.seed(author="Heisenberg", content=f"please re-check\n\nNEXT: pr-review {_PR}")


_STUCK = _rollup(
    _check("CI", "queued", started_ago=timedelta(hours=9)),
    committed_ago=timedelta(hours=9),
    pushed_ago=timedelta(hours=9),
)
_FRESH_PENDING = _rollup(
    *_PENDING, committed_ago=timedelta(minutes=1), pushed_ago=timedelta(minutes=1)
)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("verdict", "route", "rollup", "seed", "expected"),
    [
        pytest.param(
            ReviewEvent.APPROVE,
            RelayRoute.HUMAN,
            _rollup(*_GREEN),
            None,
            StopReason.MERGE_WAIT,
            id="approve",
        ),
        pytest.param(
            ReviewEvent.COMMENT,
            RelayRoute.HUMAN,
            _rollup(*_GREEN),
            None,
            StopReason.HUMAN,
            id="comment",
        ),
        pytest.param(
            ReviewEvent.APPROVE, RelayRoute.HUMAN, _STUCK, None, StopReason.HUMAN, id="r3"
        ),
        pytest.param(
            ReviewEvent.APPROVE,
            RelayRoute.HUMAN,
            _rollup(*_RED),
            _seed_r5,
            StopReason.HUMAN,
            id="r5",
        ),
        pytest.param(
            ReviewEvent.APPROVE,
            RelayRoute.HUMAN,
            _rollup(*_GREEN),
            _seed_r6,
            StopReason.HUMAN,
            id="r6",
        ),
        pytest.param(
            ReviewEvent.APPROVE,
            RelayRoute.HUMAN,
            _FRESH_PENDING,
            None,
            StopReason.CI_WAIT,
            id="ci-wait",
        ),
    ],
)
async def test_other_gate_outcomes_are_the_same_with_and_without_gate_only(
    verdict: ReviewEvent, route: RelayRoute, rollup: Any, seed: Any, expected: StopReason
) -> None:
    results = []
    for gate_only in (False, True):
        mcp = _opened()
        if seed is not None:
            seed(mcp)
        gate = _RelayGate(mcp, verdict, route)
        disp = _fixer(mcp)
        outcome = await _conductor(
            mcp,
            disp,
            gate=gate,
            rollups=_ScriptedRollupSource(rollup),
            reviews=_rc_reviews(),
            gate_only=gate_only,
        ).run()
        results.append(
            (
                outcome.stop_reason,
                outcome.rounds,
                gate.fired,
                disp.spawns,
                [(p["author"], p["content"]) for p in mcp.posts],
            )
        )
    assert results[0] == results[1]
    assert results[0][0] is expected
    assert results[0][3] == []  # none of these spawn anybody


# --------------------------------------------------------------------------- #
# 4. without gate_only, the run goes on to the implementer as before
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_without_gate_only_rc_dispatches_the_implementer_in_the_same_run() -> None:
    mcp = _opened()
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(
        mcp, disp, gate=gate, rollups=_ScriptedRollupSource(_rollup(*_GREEN)), reviews=_rc_reviews()
    ).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m2")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS  # the scripted implementer is silent


@pytest.mark.anyio
async def test_without_gate_only_r4_dispatches_the_implementer_in_the_same_run() -> None:
    mcp = _opened()
    gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(
        mcp, disp, gate=gate, rollups=_ScriptedRollupSource(_rollup(*_RED)), reviews=_rc_reviews()
    ).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m2")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS


# --------------------------------------------------------------------------- #
# 5. ROUND_CAP comes only from max_rounds
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_round_cap_still_comes_only_from_max_rounds() -> None:
    # gate_only, max_rounds=1: the slice ends round 0 on slice_end, not on the cap.
    mcp = _opened()
    outcome = await _conductor(
        mcp,
        _fixer(mcp),
        gate=_RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER),
        rollups=_ScriptedRollupSource(_rollup(*_GREEN)),
        reviews=_rc_reviews(),
        gate_only=True,
        max_rounds=1,
    ).run()
    assert outcome.stop_reason is StopReason.SLICE_END
    # Without gate_only the same run dispatches the implementer and hits the cap after round 0.
    mcp2 = _opened()
    outcome2 = await _conductor(
        mcp2,
        _fixer(mcp2),
        gate=_RelayGate(mcp2, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER),
        rollups=_ScriptedRollupSource(_rollup(*_GREEN)),
        reviews=_rc_reviews(),
        max_rounds=1,
    ).run()
    assert outcome2.stop_reason is StopReason.ROUND_CAP


# --------------------------------------------------------------------------- #
# 6. the next tick
# --------------------------------------------------------------------------- #


def test_slice_end_is_not_terminal_and_has_no_stop_kind() -> None:
    assert StopReason.SLICE_END.value not in TERMINAL_STOP_REASONS
    assert STOP_KIND_BY_STOP_REASON[StopReason.SLICE_END] is None


async def _sliced(kind: str) -> tuple[_FakeChatroomMcp, str]:
    """Run a gate-only tick to its slice; return the thread and the sliced head's id."""
    mcp = _opened()
    if kind == "rc":
        gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
        rollups = _ScriptedRollupSource(_rollup(*_GREEN))
    else:
        gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN)
        rollups = _ScriptedRollupSource(_rollup(*_RED))
    outcome = await _conductor(
        mcp, _fixer(mcp), gate=gate, rollups=rollups, reviews=_rc_reviews(), gate_only=True
    ).run()
    assert outcome.stop_reason is StopReason.SLICE_END
    assert outcome.last_msg_id is not None
    return mcp, outcome.last_msg_id


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["rc", "r4"])
async def test_next_tick_decide_launches_the_sliced_head_in_the_role_lane(kind: str) -> None:
    mcp, head_id = await _sliced(kind)
    thread = (await mcp.call_tool("chatroom_get_thread", {}))["messages"]
    head = thread[-1]
    assert head["msg_id"] == head_id
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    # The gate run was launched on the pr-review head (m1) and recorded slice_end; the head has
    # since moved to the relay / ci-route post.
    launched_on = decide(
        now=now, head_msg_id="m1", head_body=thread[0]["content"], control_state="run", record=None
    )
    record = commit_launch(
        now=now, head_msg_id="m1", verdict=launched_on, control_state="run", prior_record=None
    )
    record = commit_terminal(reason=StopReason.SLICE_END.value, head_msg_id=head_id, record=record)
    assert record.terminal_stop_reason == ""
    verdict = decide(
        now=now + timedelta(minutes=1),
        head_msg_id=head_id,
        head_body=head["content"],
        control_state="run",
        record=record,
    )
    assert verdict.decision is Decision.LAUNCH
    # The wrapper's Get-SweepLane puts every LAUNCH whose token is not pr-review in the role lane.
    assert verdict.token == "heisenberg"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["rc", "r4"])
async def test_running_again_after_the_slice_dispatches_the_implementer(kind: str) -> None:
    # The msg-6315 reproduction, as a regression test: before PR-A, this second run went through
    # guard (i) to a forced naysayer consult and a human stop.
    mcp, head_id = await _sliced(kind)
    resume_rollup = _rollup(*(_GREEN if kind == "rc" else _RED))
    # The implementer pushes a fix: CI on the new head has only just started (ci_wait).
    after_push = _rollup(
        *_PENDING,
        head=_NEW_HEAD,
        committed_ago=timedelta(minutes=1),
        pushed_ago=timedelta(minutes=1),
    )
    gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN)
    disp = _fixer(mcp)
    posts_before = len(mcp.posts)
    outcome = await _conductor(
        mcp,
        disp,
        gate=gate,
        rollups=_ScriptedRollupSource(resume_rollup, after_push),
        reviews=_rc_reviews(),
    ).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, head_id)]
    assert Role.NAYSAYER not in [role for role, _ in disp.spawns]
    assert [p["author"] for p in mcp.posts[posts_before:]] == ["Heisenberg"]
    assert outcome.stop_reason is StopReason.CI_WAIT
    assert gate.fired == []


@pytest.mark.anyio
async def test_after_an_r4_slice_the_next_red_on_the_same_head_is_r5() -> None:
    mcp, head_id = await _sliced("r4")
    gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN)
    disp = _ScriptedDispatcher(
        mcp, {Role.IMPLEMENTER: [f"could not fix it\n\nNEXT: pr-review {_PR}"]}
    )
    outcome = await _conductor(
        mcp,
        disp,
        gate=gate,
        rollups=_ScriptedRollupSource(_rollup(*_RED)),
        reviews=_rc_reviews(),
    ).run()
    # The resume did not count as a red (it never calls gate_admission); the implementer turn
    # was the one dispatch, and the next red on the unchanged head is R5.
    assert disp.dispatches == [(Role.IMPLEMENTER, head_id)]
    assert outcome.stop_reason is StopReason.HUMAN
    assert "rule=R5" in mcp.posts[-1]["content"]


# --------------------------------------------------------------------------- #
# 7. when not to slice
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["rc", "r4"])
async def test_gate_only_without_the_review_source_runs_inline(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    mcp = _opened()
    if kind == "rc":
        gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
        rollups = _ScriptedRollupSource(_rollup(*_GREEN))
    else:
        gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN)
        rollups = _ScriptedRollupSource(_rollup(*_RED))
    disp = _ScriptedDispatcher(mcp, {})
    with caplog.at_level(logging.INFO):
        outcome = await _conductor(
            mcp, disp, gate=gate, rollups=rollups, reviews=None, gate_only=True
        ).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m2")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS
    assert "conductor.gate_only.ignored reason=resume_unavailable" in caplog.text


@pytest.mark.anyio
async def test_gate_only_without_the_rollup_source_runs_inline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp = _opened()
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
    disp = _ScriptedDispatcher(mcp, {})
    with caplog.at_level(logging.INFO):
        outcome = await _conductor(
            mcp, disp, gate=gate, rollups=None, reviews=_rc_reviews(), gate_only=True
        ).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m2")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS
    assert "conductor.gate_only.ignored reason=resume_unavailable" in caplog.text


@pytest.mark.anyio
async def test_gate_only_does_not_slice_an_advisory_approve_routed_to_the_implementer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # U3': the first advisory APPROVE goes to the implementer, but the resume only accepts RC
    # relays and ci-routes (msg-6316). Slicing it would strand it, so it runs inline.
    mcp = _opened()
    gate = _RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.IMPLEMENTER)
    disp = _ScriptedDispatcher(mcp, {})
    with caplog.at_level(logging.INFO):
        outcome = await _conductor(
            mcp,
            disp,
            gate=gate,
            rollups=_ScriptedRollupSource(_rollup(*_GREEN)),
            reviews=_rc_reviews(),
            gate_only=True,
        ).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m2")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS
    assert "conductor.gate_only.ignored reason=not_resumable" in caplog.text


@pytest.mark.anyio
async def test_gate_only_with_no_implementer_persona_still_stops_at_the_human() -> None:
    # The execution fail-safe comes first: with nobody to dispatch, there is nothing to slice.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=f"opened\n\nNEXT: pr-review {_PR}")
    disp = _ScriptedDispatcher(mcp, {})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster={"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER},
        naysayer_identity="Einstein",
        orchestrator=_RelayGate(mcp, ReviewEvent.APPROVE, RelayRoute.HUMAN),
        rollup_source=_ScriptedRollupSource(_rollup(*_RED)),
        review_source=_rc_reviews(),
        gate_only=True,
    )
    outcome = await conductor.run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == []


# --------------------------------------------------------------------------- #
# 8. the CLI hands --gate-only to the conductor; absent means False
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("argv", "expected"), [([], False), (["--gate-only"], True)])
def test_main_forwards_gate_only(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    from spirrow_mindwire import loop_runner
    from spirrow_mindwire.config import MindwireSettings

    seen: dict[str, Any] = {}

    async def _fake_run_conductor(_settings: Any, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor", *argv])
    loop_runner.main()
    assert seen["gate_only"] is expected
