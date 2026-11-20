"""The run budget's dispatch-time reserve and phase-decided auto-resume.

Thread: T-run-budget-exhausted-by-chain-dispatches-new-turn (Fermi msg-9796; design Bohr msg-9833,
revised msg-9835 / msg-9837 after Einstein msg-9834 / msg-9836; endorsed by Einstein msg-9838 /
msg-9839-follow-up). The numbered sections follow the test list of msg-9835 and msg-9837:

1. a turn the budget cannot hold is not started, on every dispatch path, and posts nothing;
2. the boundary (``remaining == reserve`` goes on, one second less stops), round 1 included;
3. time spent after the check (the spawn) is caught by the check right before ``query``;
4. what a cut-off does is decided by the phase it hit, on the real call path, at ``query`` start,
   mid-post, right after a post landed and inside the PR gate;
5. every chatroom post of a conductor run passes through :class:`PostPhaseMcp`;
6. the stall watchdog bounds a head that keeps stopping on ``budget_cut`` / ``budget_yield``;
7. the timeout notice carries facts only, and the incident timeline (msg-9796 §1) is reproduced.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import (
    _GREEN,
    _ROSTER,
    _FakeChatroomMcp,
    _rollup,
    _ScriptedDispatcher,
    _ScriptedRollupSource,
    _thread_ref,
)
from test_gate_only_slice import _GATE_LOGIN, _PR, _rc_reviews, _RelayGate

from spirrow_mindwire import loop_runner
from spirrow_mindwire.conductor import core as core_module
from spirrow_mindwire.conductor.core import Conductor, ConductorOutcome, StopReason
from spirrow_mindwire.conductor.gate_records import RelayRoute
from spirrow_mindwire.conductor.run_budget import (
    AUTO_RESUME_PHASES,
    NON_IMPLEMENTER_RESERVE_S,
    PHASE_IDLE,
    YIELD_MARGIN_S,
    PostPhaseMcp,
    RunPhase,
    TurnRecord,
    enter_phase,
    render_run_timeout_notice,
    run_timeout_event,
    run_with_budget,
    validate_budgets,
)
from spirrow_mindwire.conductor.stall import STALL_THRESHOLD
from spirrow_mindwire.conductor.stop_kind import STOP_KIND_BY_STOP_REASON
from spirrow_mindwire.config import ConductorConfig, MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.github.client import ReviewEvent
from spirrow_mindwire.value_objects import Role

_IMPL_RESERVE = 1800.0 + YIELD_MARGIN_S


class _Clock:
    """A settable clock standing in for the loop clock of :meth:`RunPhase.now`."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(RunPhase, "now", staticmethod(c))
    return c


def _phase(clock: _Clock, remaining: float) -> RunPhase:
    phase = RunPhase()
    phase.implementer_reserve_s = _IMPL_RESERVE
    phase.started_at = clock.t
    phase.deadline = clock.t + remaining
    return phase


def _conductor(mcp: Any, disp: Any, phase: RunPhase, **kw: Any) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        run_phase=phase,
        **kw,
    )


def _gate_conductor(mcp: Any, disp: Any, phase: RunPhase, gate: Any) -> Conductor:
    reviews = _rc_reviews()
    return _conductor(
        mcp,
        disp,
        phase,
        orchestrator=gate,
        rollup_source=_ScriptedRollupSource(_rollup(*_GREEN)),
        review_source=reviews,
        head_source=reviews,
        review_login=_GATE_LOGIN,
    )


# --------------------------------------------------------------------------- #
# 1. every dispatch path refuses a turn the budget cannot hold
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_role_turn_short_of_reserve_is_not_dispatched_and_posts_nothing(
    clock: _Clock,
) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["ok\n\nNEXT: human"]})
    phase = _phase(clock, NON_IMPLEMENTER_RESERVE_S - 1)
    outcome = await _conductor(mcp, disp, phase).run()
    assert outcome.stop_reason is StopReason.BUDGET_YIELD
    assert outcome.last_msg_id == "m1"
    assert disp.dispatches == []
    assert mcp.posts == []


@pytest.mark.anyio
async def test_implementer_on_an_rc_relay_is_not_dispatched_and_the_relay_stays_the_head(
    clock: _Clock,
) -> None:
    """RC relay path: the relay is posted, the implementer turn does not start, and the relay is
    left as the head for the next tick's gate resume (the SLICE_END shape)."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_PR}")
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["fixed\n\nNEXT: human"]})
    # Enough for the gate (600) but not for the implementer (1920).
    phase = _phase(clock, 1000)
    outcome = await _gate_conductor(mcp, disp, phase, gate).run()
    assert outcome.stop_reason is StopReason.BUDGET_YIELD
    assert gate.fired == [_PR]
    assert disp.dispatches == []
    relay_id = "m2"  # m1 = the pr-review nomination, m2 = the RC relay
    assert mcp.posts[0]["author"] == "pr-gate-relay"
    assert outcome.last_msg_id == relay_id

    # Next tick, full budget: the gate resume dispatches the implementer from that relay and the
    # gate is not fired again.
    phase2 = _phase(clock, 3940)
    gate2 = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
    await _gate_conductor(mcp, disp, phase2, gate2).run()
    assert gate2.fired == []
    assert disp.dispatches[0] == (Role.IMPLEMENTER, relay_id)


@pytest.mark.anyio
async def test_pr_gate_short_of_reserve_is_not_fired(clock: _Clock) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_PR}")
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _gate_conductor(mcp, disp, _phase(clock, 599), gate).run()
    assert outcome.stop_reason is StopReason.BUDGET_YIELD
    assert gate.fired == []
    assert mcp.posts == []
    assert outcome.last_msg_id == "m1"


@pytest.mark.anyio
async def test_dispatch_implementer_helper_refuses_before_query(clock: _Clock) -> None:
    """The helper shared by the RC relay, the ci-route, the gate resume and the continuation
    (``_act_on_continuation`` ends in it): the turn is refused after the spawn, before dispatch."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="conductor-relay", content="continue\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["x\n\nNEXT: human"]})
    phase = _phase(clock, _IMPL_RESERVE - 1)
    cond = _conductor(mcp, disp, phase)
    trigger = {"msg_id": "m1", "author": "conductor-relay", "content": "continue"}
    with pytest.raises(core_module._BudgetYield) as excinfo:
        await cond._dispatch_implementer(
            {}, "Heisenberg", trigger, [trigger], round_index=0, forced=0, forced_saveable=0
        )
    assert excinfo.value.last_msg_id == "m1"
    assert disp.spawns == [(Role.IMPLEMENTER, "Heisenberg")]
    assert disp.dispatches == []


def test_every_role_dispatch_goes_through_the_one_checked_function() -> None:
    """D1' (msg-9835): all five implementer paths and every other role turn reach the adapter
    through ``_dispatch_recording``, which checks the budget before entering ``<role>.dispatch``."""
    module_src = inspect.getsource(core_module)
    assert module_src.count("self._dispatcher.dispatch(") == 1
    body = inspect.getsource(Conductor._dispatch_recording)
    assert body.index("self._check_budget(") < body.index("self._dispatcher.dispatch(")
    # Nothing awaits between the check and entering the dispatch phase (msg-9837 D3''-1).
    between = body[body.index("self._check_budget(") : body.index("enter_phase(self._run_phase")]
    assert "await" not in between
    # And the PR gate is fired only right after its own check.
    run_src = inspect.getsource(Conductor._run_rounds)
    fire = run_src.index("await self._fire_pr_gate(")
    assert run_src.rindex("self._check_budget(", 0, fire) > run_src.rindex("\n\n", 0, fire) - 2000


# --------------------------------------------------------------------------- #
# 2. boundary, round 1 included
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (NON_IMPLEMENTER_RESERVE_S, StopReason.HUMAN),
        (NON_IMPLEMENTER_RESERVE_S - 1, StopReason.BUDGET_YIELD),
    ],
)
async def test_equal_goes_on_one_second_less_stops_on_round_one(
    clock: _Clock, remaining: float, expected: StopReason
) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["ok\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp, _phase(clock, remaining)).run()
    assert outcome.stop_reason is expected


def test_reserve_short_for_boundary() -> None:
    phase = RunPhase()
    phase.implementer_reserve_s = _IMPL_RESERVE
    phase.deadline = 0.0  # replaced below through remaining_s
    phase.remaining_s = lambda: _IMPL_RESERVE  # type: ignore[method-assign]
    assert phase.short_for("implementer") is None
    phase.remaining_s = lambda: _IMPL_RESERVE - 1  # type: ignore[method-assign]
    assert phase.short_for("implementer") == (_IMPL_RESERVE - 1, _IMPL_RESERVE)


def test_no_budget_running_never_yields() -> None:
    assert RunPhase().short_for("implementer") is None


def test_validate_budgets_refuses_a_budget_below_the_implementer_reserve() -> None:
    validate_budgets(run_budget_s=3940, run_hard_budget_s=4840, implementer_reserve_s=1920)
    with pytest.raises(SystemExit, match="implementer's reserve"):
        validate_budgets(run_budget_s=1920, run_hard_budget_s=4840, implementer_reserve_s=1920)


def test_reserve_follows_the_implementer_turn_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINDWIRE_IMPLEMENTER_TURN_TIMEOUT_SECONDS", "900")
    assert loop_runner._implementer_reserve_s() == 900 + YIELD_MARGIN_S
    monkeypatch.delenv("MINDWIRE_IMPLEMENTER_TURN_TIMEOUT_SECONDS")
    assert loop_runner._implementer_reserve_s() == 1800 + YIELD_MARGIN_S


def test_validate_budgets_refuses_a_budget_below_the_other_role_reserve() -> None:
    validate_budgets(
        run_budget_s=NON_IMPLEMENTER_RESERVE_S + 1,
        run_hard_budget_s=4840,
        implementer_reserve_s=220,
        other_reserve_s=NON_IMPLEMENTER_RESERVE_S,
    )
    with pytest.raises(SystemExit, match="PR-gate reserve"):
        validate_budgets(
            run_budget_s=NON_IMPLEMENTER_RESERVE_S,
            run_hard_budget_s=4840,
            implementer_reserve_s=220,
            other_reserve_s=NON_IMPLEMENTER_RESERVE_S,
        )


@pytest.mark.anyio
async def test_startup_refuses_a_budget_only_the_implementer_reserve_fits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #597 gate (REQUEST_CHANGES @ 9bf1a1a): implementer timeout 100 s and run_budget_s=500
    passed startup (implementer reserve 220 s) although every proposer / naysayer / PR-gate turn
    (reserve 600 s) would yield on every launch. Startup must refuse it before anything runs."""
    monkeypatch.setenv("MINDWIRE_IMPLEMENTER_TURN_TIMEOUT_SECONDS", "100")
    called: list[Any] = []

    async def _once(*_a: Any, **_kw: Any) -> Any:
        called.append(1)
        raise AssertionError("must not run")

    monkeypatch.setattr(loop_runner, "_run_conductor_once", _once)
    settings = MindwireSettings(
        loop=Stage3LoopConfig(project="spirrow-mindwire", repo_dir=tmp_path),
        conductor=ConductorConfig(
            task_thread_id="T-cond", run_budget_s=500, run_hard_budget_s=10_000
        ),
    )
    chat = _FakeChatroomMcp()
    with pytest.raises(SystemExit, match="PR-gate reserve"):
        await loop_runner.run_conductor(settings, mcp=chat)
    assert called == []
    assert chat.posts == []


# --------------------------------------------------------------------------- #
# 3. time used after the check: the spawn
# --------------------------------------------------------------------------- #


class _SlowSpawn(_ScriptedDispatcher):
    def __init__(self, mcp: Any, replies: Any, clock: _Clock, spawn_s: float) -> None:
        super().__init__(mcp, replies)
        self._clock = clock
        self._spawn_s = spawn_s

    async def spawn_instance(self, thread_ref: Any, role: Role, instance_id: str) -> Any:
        self._clock.t += self._spawn_s
        return await super().spawn_instance(thread_ref, role, instance_id)


@pytest.mark.anyio
async def test_a_slow_spawn_is_caught_by_the_check_right_before_query(clock: _Clock) -> None:
    """Einstein msg-9834 #2: 1921 s left passes any check made before the spawn, a 150 s spawn
    leaves 1771 s, and the implementer must not start."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="conductor-relay", content="continue\n\nNEXT: Heisenberg")
    disp = _SlowSpawn(mcp, {Role.IMPLEMENTER: ["x\n\nNEXT: human"]}, clock, 150)
    phase = _phase(clock, _IMPL_RESERVE + 1)
    trigger = {"msg_id": "m1", "author": "conductor-relay", "content": "continue"}
    with pytest.raises(core_module._BudgetYield):
        await _conductor(mcp, disp, phase)._dispatch_implementer(
            {}, "Heisenberg", trigger, [trigger], round_index=0, forced=0, forced_saveable=0
        )
    assert disp.spawns and disp.dispatches == []


# --------------------------------------------------------------------------- #
# 4. the phase a cut-off hits, on the real call path
# --------------------------------------------------------------------------- #


class _HangingPostMcp(_FakeChatroomMcp):
    """Posts hang (the post's outcome is unknown) once ``hang_posts`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.hang_posts = False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_post_message" and self.hang_posts:
            await asyncio.Event().wait()
        return await super().call_tool(name, arguments)


class _Hang(_ScriptedDispatcher):
    """Dispatch posts its reply through ``post_mcp`` (as the gateway does), then optionally hangs.

    ``hang_before_reply`` = the role's ``query`` is running; ``hang_after_reply`` = the reply
    landed and the adapter is still draining.
    """

    def __init__(
        self,
        chat: _FakeChatroomMcp,
        post_mcp: Any,
        replies: Any,
        *,
        hang_before_reply: bool = False,
        hang_after_reply: bool = False,
    ) -> None:
        super().__init__(chat, replies)
        self._mcp = post_mcp
        self._before = hang_before_reply
        self._after = hang_after_reply

    async def dispatch(self, handle: Any, event: Any) -> None:
        if self._before:
            self.dispatches.append((handle.role, event.payload.msg_id))
            await asyncio.Event().wait()
        await super().dispatch(handle, event)
        if self._after:
            await asyncio.Event().wait()


async def _cut(cond: Conductor, phase: RunPhase) -> Any:
    _result, event = await run_with_budget(
        cond.run, budget_s=0.2, run_phase=phase, project="p", thread="t"
    )
    assert event is not None
    return event


def _free_phase() -> RunPhase:
    phase = RunPhase()
    phase.implementer_reserve_s = 0.0
    phase.other_reserve_s = 0.0
    return phase


@pytest.mark.anyio
async def test_cut_at_implementer_query_start_is_not_auto_resumed() -> None:
    chat = _HangingPostMcp()
    chat.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_PR}")
    phase = _free_phase()
    mcp = PostPhaseMcp(chat, phase)
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)  # type: ignore[arg-type]
    disp = _Hang(chat, mcp, {Role.IMPLEMENTER: ["x"]}, hang_before_reply=True)
    event = await _cut(_gate_conductor(mcp, disp, phase, gate), phase)
    assert event.fields["phase"] == "implementer.dispatch"
    assert event.fields["auto_resume"] is False
    notice = render_run_timeout_notice(event, tuple(phase.turns))
    assert "refs/heads/wip" in notice
    assert notice.rstrip().endswith("NEXT: human")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("author", "target", "role", "expected"),
    [
        ("Einstein", "Bohr", Role.PROPOSER, "proposer.post"),
        ("Bohr", "Einstein", Role.NAYSAYER, "naysayer.post"),
    ],
)
async def test_cut_mid_reply_post_is_not_auto_resumed(
    author: str, target: str, role: Role, expected: str
) -> None:
    chat = _HangingPostMcp()
    chat.seed(author=author, content=f"x\n\nNEXT: {target}")
    chat.hang_posts = True
    phase = _free_phase()
    mcp = PostPhaseMcp(chat, phase)
    disp = _Hang(chat, mcp, {role: ["reply\n\nNEXT: human"]})
    event = await _cut(_conductor(mcp, disp, phase), phase)
    assert event.fields["phase"] == expected
    assert event.fields["auto_resume"] is False


@pytest.mark.anyio
async def test_cut_mid_relay_post_is_not_auto_resumed() -> None:
    """The PR gate's verdict relay hangs mid-post: ``relay.post``, a person decides."""
    chat = _HangingPostMcp()
    chat.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_PR}")
    chat.hang_posts = True
    phase = _free_phase()
    mcp = PostPhaseMcp(chat, phase)
    gate = _RelayGate(mcp, ReviewEvent.REQUEST_CHANGES, RelayRoute.IMPLEMENTER)  # type: ignore[arg-type]
    event = await _cut(_gate_conductor(mcp, _ScriptedDispatcher(chat, {}), phase, gate), phase)
    assert event.fields["phase"] == "relay.post"
    assert event.fields["auto_resume"] is False


@pytest.mark.anyio
async def test_cut_inside_the_pr_gate_is_not_auto_resumed() -> None:
    class _HangingGate:
        def __init__(self) -> None:
            self.fired: list[str] = []

        async def fire_pr_review(self, **kw: Any) -> Any:
            self.fired.append(kw["pr_ref"])  # the GitHub review may already be submitted
            await asyncio.Event().wait()

    chat = _FakeChatroomMcp()
    chat.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_PR}")
    phase = _free_phase()
    event = await _cut(
        _gate_conductor(chat, _ScriptedDispatcher(chat, {}), phase, _HangingGate()), phase
    )
    assert event.fields["phase"] == "pr_gate.review"
    assert event.fields["auto_resume"] is False


@pytest.mark.anyio
async def test_cut_right_after_the_reply_landed_is_auto_resumed_and_not_redone() -> None:
    chat = _FakeChatroomMcp()
    chat.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    phase = _free_phase()
    mcp = PostPhaseMcp(chat, phase)
    disp = _Hang(chat, mcp, {Role.NAYSAYER: ["review\n\nNEXT: Bohr"]}, hang_after_reply=True)
    event = await _cut(_conductor(mcp, disp, phase), phase)
    assert event.fields["phase"] == "naysayer.dispatch"
    assert event.fields["auto_resume"] is True
    assert phase.turns[-1].msg_id == "m2"
    naysayer_turns = [d for d in disp.dispatches if d[0] is Role.NAYSAYER]
    assert naysayer_turns == [(Role.NAYSAYER, "m1")]

    # Next tick: the head is the naysayer's reply, so the naysayer is not dispatched on m1 again.
    phase2 = _free_phase()
    disp2 = _ScriptedDispatcher(chat, {Role.PROPOSER: ["rev\n\nNEXT: human"]})
    await _conductor(PostPhaseMcp(chat, phase2), disp2, phase2).run()
    assert (Role.NAYSAYER, "m1") not in disp2.dispatches
    assert disp2.dispatches[0] == (Role.PROPOSER, "m2")


def test_auto_resume_phases_are_exactly_the_endorsed_set() -> None:
    """msg-9837 D3''-4 safe-side rule: a role's ``*.dispatch`` may be in the set only if its posts
    are shown to switch to ``<role>.post``. That is verified by msg-9837 tests 2, 3 and 5 (not
    test 1, which is the implementer's query start — Einstein msg-9838):
    ``test_cut_mid_reply_post_is_not_auto_resumed``,
    ``test_cut_mid_relay_post_is_not_auto_resumed``,
    ``test_cut_right_after_the_reply_landed_is_auto_resumed_and_not_redone`` and
    ``test_conductor_run_post_sites_are_the_pinned_set``."""
    assert {
        "proposer.spawn",
        "proposer.dispatch",
        "naysayer.spawn",
        "naysayer.dispatch",
        "implementer.spawn",
        "thread.read",
        "launch.resolve",
    } == AUTO_RESUME_PHASES
    for phase in (
        PHASE_IDLE,
        "implementer.dispatch",
        "implementer.post",
        "proposer.post",
        "naysayer.post",
        "relay.post",
        "pr_gate.review",
        "teardown",
    ):
        assert phase not in AUTO_RESUME_PHASES


def test_a_post_inside_a_dispatch_restores_the_dispatch_phase() -> None:
    phase = RunPhase()
    with enter_phase(phase, "naysayer.dispatch"):
        with phase.enter("naysayer.post"):
            assert phase.current == "naysayer.post"
        assert phase.current == "naysayer.dispatch"
    assert phase.current == PHASE_IDLE


# Run-level outcome of a cut-off, through ``loop_runner.run_conductor``.


def _settings(tmp_path: Path) -> MindwireSettings:
    return MindwireSettings(
        loop=Stage3LoopConfig(project="spirrow-mindwire", repo_dir=tmp_path),
        conductor=ConductorConfig(task_thread_id="T-cond", run_budget_s=0.1, run_hard_budget_s=100),
    )


def _hang_in(phase_name: str) -> Callable[..., Any]:
    async def _once(settings: Any, *, mcp: Any, run_phase: RunPhase, **_kw: Any) -> Any:
        rec = run_phase.begin_turn(round_index=0, role="naysayer", identity="E", phase=phase_name)
        del rec
        with run_phase.enter(phase_name):
            await asyncio.Event().wait()

    return _once


@pytest.mark.anyio
@pytest.mark.parametrize("phase_name", sorted(AUTO_RESUME_PHASES))
async def test_budget_cut_posts_nothing_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase_name: str
) -> None:
    chat = _FakeChatroomMcp()
    monkeypatch.setattr(loop_runner, "_implementer_reserve_s", lambda: 0.0)
    monkeypatch.setattr(loop_runner, "_other_reserve_s", lambda: 0.0)
    monkeypatch.setattr(loop_runner, "_run_conductor_once", _hang_in(phase_name))
    outcome = await loop_runner.run_conductor(_settings(tmp_path), mcp=chat)
    assert outcome.stop_reason is StopReason.BUDGET_CUT
    assert outcome.last_msg_id is None
    assert chat.posts == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "phase_name",
    ["implementer.dispatch", "pr_gate.review", "relay.post", "naysayer.post", PHASE_IDLE],
)
async def test_cut_that_may_have_written_outside_goes_to_a_person(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase_name: str
) -> None:
    chat = _FakeChatroomMcp()
    monkeypatch.setattr(loop_runner, "_implementer_reserve_s", lambda: 0.0)
    monkeypatch.setattr(loop_runner, "_other_reserve_s", lambda: 0.0)
    monkeypatch.setattr(loop_runner, "_run_conductor_once", _hang_in(phase_name))
    outcome = await loop_runner.run_conductor(_settings(tmp_path), mcp=chat)
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(chat.posts) == 1
    body = chat.posts[0]["content"]
    assert body.rstrip().endswith("NEXT: human")
    assert f"`{phase_name}`" in body
    assert "原因は特定していません" not in body


# --------------------------------------------------------------------------- #
# 5. every chatroom post of a run passes through PostPhaseMcp
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_run_conductor_hands_the_wrapped_mcp_to_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Any] = []

    async def _once(settings: Any, *, mcp: Any, **_kw: Any) -> ConductorOutcome:
        seen.append(mcp)
        return ConductorOutcome(
            rounds=0, stop_reason=StopReason.SETTLED, last_msg_id=None, forced_naysayer_turns=0
        )

    monkeypatch.setattr(loop_runner, "_implementer_reserve_s", lambda: 0.0)
    monkeypatch.setattr(loop_runner, "_other_reserve_s", lambda: 0.0)
    monkeypatch.setattr(loop_runner, "_run_conductor_once", _once)
    await loop_runner.run_conductor(_settings(tmp_path), mcp=_FakeChatroomMcp())
    assert isinstance(seen[0], PostPhaseMcp)


#: Every module a conductor run executes that calls ``chatroom_post_message``, and how its MCP
#: client reaches it. Each one is handed the run's ``PostPhaseMcp`` (``chatroom`` in
#: ``run_conductor``): the Conductor and its stall / spawn notices, the gateway built by
#: ``_build_dispatcher(mcp=...)``, the orchestrator built in ``build_conductor`` and the
#: stand-down post in ``_run_conductor_once``. ``run_budget`` posts the timeout notice after the
#: run, with the raw client, on purpose: that post is not part of the run being timed.
_CONDUCTOR_RUN_POSTERS = {
    "conductor/core.py",
    "conductor/stand_down.py",
    "conductor/run_budget.py",
    "magickit/gateway.py",
    "orchestrator.py",
}


def test_conductor_run_post_sites_are_the_pinned_set() -> None:
    """msg-9837 test 5: no chatroom post of a conductor run bypasses the wrapper. A new module that
    posts during a run has to be added here after checking it is handed the run's client."""
    root = Path(core_module.__file__).resolve().parents[1]
    reachable = {"conductor", "magickit", "orchestrator.py", "naysayer", "adapters"}
    found = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[0] not in reachable:
            continue
        src = path.read_text(encoding="utf-8")
        if "call_tool(\n" in src or "call_tool(" in src:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "call_tool"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "chatroom_post_message"
                ):
                    found.add(rel)
    assert found == _CONDUCTOR_RUN_POSTERS


# --------------------------------------------------------------------------- #
# 6. stall watchdog bounds a head that keeps stopping on the budget
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_third_launch_on_the_same_head_stands_down(clock: _Clock) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["ok\n\nNEXT: human"]})
    cond = _conductor(
        mcp,
        disp,
        _phase(clock, 3940),
        launches_same_head=STALL_THRESHOLD,
        launch_head_msg_id="m1",
    )
    outcome = await cond.run()
    assert outcome.stop_reason is StopReason.STALLED
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_a_new_head_after_progress_counts_from_one(clock: _Clock) -> None:
    """The sweep's count was for an older head; the run's head moved, so no stand-down."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content="review\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {})
    cond = _conductor(
        mcp,
        disp,
        _phase(clock, 1),
        launches_same_head=STALL_THRESHOLD,
        launch_head_msg_id="m1",
    )
    outcome = await cond.run()
    assert outcome.stop_reason is StopReason.BUDGET_YIELD
    assert outcome.last_msg_id == "m2"


def test_budget_stops_are_not_terminal_and_have_no_stop_kind() -> None:
    from spirrow_mindwire.conductor.head_skip import TERMINAL_STOP_REASONS

    for reason in (StopReason.BUDGET_YIELD, StopReason.BUDGET_CUT):
        assert reason.value not in TERMINAL_STOP_REASONS
        assert STOP_KIND_BY_STOP_REASON[reason] is None


# --------------------------------------------------------------------------- #
# 7. notice text and the incident timeline (msg-9796 §1)
# --------------------------------------------------------------------------- #

#: msg-9796 §1: 23:37 start, Heisenberg x4 (~52 min), Bohr / Einstein x2 each, then the fifth
#: implementer turn at 00:31 (3220 s in), cut at 3940 s.
_INCIDENT = (
    TurnRecord(1, "implementer", "Heisenberg", "implementer.dispatch", 0.0, 780.0, "msg-9770"),
    TurnRecord(2, "implementer", "Heisenberg", "implementer.dispatch", 780.0, 780.0, "msg-9772"),
    TurnRecord(3, "implementer", "Heisenberg", "implementer.dispatch", 1560.0, 780.0, "msg-9774"),
    TurnRecord(4, "implementer", "Heisenberg", "implementer.dispatch", 2340.0, 780.0, "msg-9776"),
    TurnRecord(5, "proposer", "Bohr", "proposer.dispatch", 3120.0, 20.0, "msg-9777"),
    TurnRecord(6, "naysayer", "Einstein", "naysayer.dispatch", 3140.0, 20.0, "msg-9778"),
    TurnRecord(7, "proposer", "Bohr", "proposer.dispatch", 3160.0, 30.0, "msg-9779"),
    TurnRecord(8, "naysayer", "Einstein", "naysayer.dispatch", 3190.0, 30.0, "msg-9780"),
    TurnRecord(9, "implementer", "Heisenberg", "implementer.dispatch", 3220.0, None, "-"),
)


def test_incident_notice_is_facts_only_and_goes_to_a_person() -> None:
    event = run_timeout_event(
        budget_s=3940.0,
        elapsed_s=3940.0,
        phase="implementer.dispatch",
        project="spirrow-mindwire",
        thread="T-tier-c-six-articles-security-boundary",
        in_flight_s=720.0,
        turns=len(_INCIDENT),
    )
    assert event.fields["auto_resume"] is False
    assert event.fields["in_flight_share"] == 0.18
    body = render_run_timeout_notice(event, _INCIDENT)
    assert "原因は特定していません" not in body
    assert "`implementer.dispatch` で、経過は 720.0 秒" in body
    assert "| 9 | implementer | Heisenberg | `implementer.dispatch` | 3220.0 | 実行中 | - |" in body
    assert "git for-each-ref refs/heads/wip --sort=-creatordate" in body
    assert body.rstrip().endswith("NEXT: human")


def test_with_the_reserve_the_incident_fifth_turn_never_starts(clock: _Clock) -> None:
    phase = _phase(clock, 3940)
    clock.t += 3220  # 00:31 JST: 720 s left
    assert phase.short_for("implementer") == (720.0, _IMPL_RESERVE)
