"""Wall-clock budget for one conductor run (W-1 soft limit, W-3 / W-4 vocabulary, W-2 pins).

Thread T-agmsg-transport-lessons-readiness-session-claim-board (Bohr msg-5496, revised msg-5498;
endorsed by Einstein msg-5499 / msg-5501). The PowerShell hard limit (W-2) and the sweep's exit-7
stop are exercised by ``tests/Test-ConductorWallClock.ps1``; this file pins the literals the two
languages share.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire import loop_runner
from spirrow_mindwire.conductor import run_budget
from spirrow_mindwire.conductor.core import (
    CONDUCTOR_RELAY_AUTHOR,
    Conductor,
    ConductorOutcome,
    ConductorStopSlot,
    StopReason,
)
from spirrow_mindwire.conductor.run_budget import (
    EVENT_KIND_RUN_KILLED,
    EVENT_KIND_RUN_TIMEOUT,
    PHASE_IDLE,
    RELAY_POST_BUDGET_S,
    RUN_KILL_UNCONFIRMED_EXIT_CODE,
    RUN_KILLED_EXIT_CODE,
    RUN_TIMEOUT_EXIT_CODE,
    RunPhase,
    RunTimeoutError,
    post_run_timeout_notice,
    render_run_timeout_notice,
    run_timeout_event,
    run_with_budget,
    validate_budgets,
)
from spirrow_mindwire.conductor.stall import STALLED_EXIT_CODE
from spirrow_mindwire.conductor.stand_down import STAND_DOWN_EXIT_CODE
from spirrow_mindwire.conductor.stop_kind import (
    STOP_KIND_BY_EVENT_KIND,
    STOP_KIND_BY_STOP_REASON,
    StopKind,
)
from spirrow_mindwire.conductor.stop_marker import parse_stop_marker
from spirrow_mindwire.config import (
    DEFAULT_CONDUCTOR_RUN_BUDGET_S,
    DEFAULT_CONDUCTOR_RUN_HARD_BUDGET_S,
    ConductorConfig,
    MindwireSettings,
    Stage3LoopConfig,
)
from spirrow_mindwire.magickit.client import ThreadResolvedError
from spirrow_mindwire.value_objects import Role

_REPO = Path(__file__).resolve().parents[1]
_BUDGET_LOGGER = "spirrow_mindwire.conductor.run_budget"


def _event(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "budget_s": 3940.0,
        "elapsed_s": 3940.2,
        "phase": "naysayer.dispatch",
        "project": "spirrow-mindwire",
        "thread": "T-cond",
    }
    fields.update(over)
    return run_timeout_event(**fields)


def _timeout_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _BUDGET_LOGGER
        and r.getMessage().startswith(EVENT_KIND_RUN_TIMEOUT + " budget_s=")
    ]


# --------------------------------------------------------------------------- #
# run_with_budget
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_body_that_finishes_returns_its_result_and_writes_no_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    async def _body() -> str:
        return "done"

    result, event = await run_with_budget(
        _body, budget_s=5, run_phase=RunPhase(), project="p", thread="t"
    )
    assert (result, event) == ("done", None)
    assert _timeout_lines(caplog) == []


@pytest.mark.anyio
async def test_line_is_written_before_the_body_tears_down(caplog: pytest.LogCaptureFixture) -> None:
    # Einstein msg-5497 objection 1: the line must exist whatever happens afterwards. It is written
    # at the deadline, before the cancellation reaches the body's ``finally`` (adapter teardown).
    caplog.set_level(logging.INFO)
    order: list[str] = []
    phase = RunPhase()

    async def _body() -> None:
        try:
            with phase.enter("naysayer.dispatch"):
                await asyncio.sleep(30)
        finally:
            order.append("teardown")
            logging.getLogger("test.teardown").info("teardown ran")

    result, event = await run_with_budget(
        _body, budget_s=0.05, run_phase=phase, project="spirrow-mindwire", thread="T-cond"
    )
    assert result is None and event is not None
    assert event.kind == EVENT_KIND_RUN_TIMEOUT
    assert event.fields["phase"] == "naysayer.dispatch"
    assert event.fields["budget_s"] == 0.05
    assert event.fields["project"] == "spirrow-mindwire" and event.fields["thread"] == "T-cond"
    messages = [r.getMessage() for r in caplog.records]
    timeout_at = next(i for i, m in enumerate(messages) if m.startswith(EVENT_KIND_RUN_TIMEOUT))
    assert timeout_at < messages.index("teardown ran")
    line = _timeout_lines(caplog)[0]
    assert "phase=naysayer.dispatch" in line and "budget_s=0.05" in line
    assert "project=spirrow-mindwire" in line and "thread=T-cond" in line
    assert phase.current == PHASE_IDLE  # restored on the way out


@pytest.mark.anyio
async def test_async_teardown_runs_to_completion_after_the_deadline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # PR-gate on f045ba8: does the expired scope cancel every ``await`` in the body's ``finally``
    # (``await cond.aclose()`` in ``loop_runner._run_conductor_once``), aborting teardown on its
    # first await? No: asyncio's cancellation is edge-triggered. ``asyncio.timeout`` calls
    # ``task.cancel()`` once at the deadline; that one CancelledError lands on the await that was
    # pending, and later awaits in ``finally`` run normally. This test does what the real
    # teardown does, several awaits that suspend, and checks that every one of them completes.
    caplog.set_level(logging.INFO)
    order: list[str] = []

    async def _aclose() -> None:
        for i in range(3):
            await asyncio.sleep(0.01)  # really suspends, unlike sleep(0)
            order.append(f"aclose.{i}")
        order.append("aclose.done")

    async def _body() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            order.append("teardown")
            await _aclose()

    result, event = await run_with_budget(
        _body, budget_s=0.05, run_phase=RunPhase(), project="p", thread="t"
    )
    assert result is None and event is not None
    assert order == ["teardown", "aclose.0", "aclose.1", "aclose.2", "aclose.done"]
    assert len(_timeout_lines(caplog)) == 1


@pytest.mark.anyio
async def test_teardown_that_hangs_cannot_keep_the_line_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A teardown that never returns leaves the hard budget to end the process; the soft line must
    # already be in the log by then. Bounded here by an outer wait_for standing in for the kill.
    caplog.set_level(logging.WARNING)

    async def _body() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_with_budget(_body, budget_s=0.05, run_phase=RunPhase(), project="p", thread="t"),
            timeout=0.5,
        )
    assert len(_timeout_lines(caplog)) == 1


@pytest.mark.anyio
async def test_a_timeout_the_body_raises_itself_is_not_converted() -> None:
    async def _body() -> None:
        raise TimeoutError("the body's own")

    with pytest.raises(TimeoutError, match="the body's own"):
        await run_with_budget(_body, budget_s=5, run_phase=RunPhase(), project="p", thread="t")


def test_run_phase_restores_the_previous_value() -> None:
    phase = RunPhase()
    with phase.enter("implementer.spawn"):
        assert phase.current == "implementer.spawn"
        with phase.enter("thread.read"):
            assert phase.current == "thread.read"
        assert phase.current == "implementer.spawn"
    assert phase.current == PHASE_IDLE


# --------------------------------------------------------------------------- #
# the notice and its post
# --------------------------------------------------------------------------- #


def test_notice_carries_the_stop_marker_and_ends_on_next_human() -> None:
    event = _event(phase="pr_gate.review")
    body = render_run_timeout_notice(event)
    lines = body.splitlines()
    assert lines[-1] == "NEXT: human"
    assert lines[-2] == ""  # the TIER-C: / STOP: readers look at the line above NEXT:
    marker = parse_stop_marker(body)
    assert marker is not None
    assert marker == {"kind": EVENT_KIND_RUN_TIMEOUT, **event.fields}
    assert "`pr_gate.review`" in body


class _PostMcp:
    def __init__(self, *, error: BaseException | None = None, msg_id: str = "m-n") -> None:
        self.error = error
        self.msg_id = msg_id
        self.hang = False
        self.posts: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "chatroom_post_message"
        self.posts.append(arguments)
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return {"msg": {"msg_id": self.msg_id}}


@pytest.mark.anyio
async def test_post_returns_the_msg_id_under_conductor_relay() -> None:
    mcp = _PostMcp()
    got = await post_run_timeout_notice(
        mcp, project="p", thread_id="t", event=_event(), author=CONDUCTOR_RELAY_AUTHOR
    )
    assert got == "m-n"
    assert mcp.posts[0]["author"] == CONDUCTOR_RELAY_AUTHOR
    assert mcp.posts[0]["thread_id"] == "t"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode", ["resolved", "no_msg_id", "raises", "hangs"], ids=lambda m: f"post-{m}"
)
async def test_post_that_does_not_land_exits_5(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Einstein msg-5497 objection 2: the post is bounded too, so a chatroom that stopped answering
    # cannot hang the soft path.
    monkeypatch.setattr(run_budget, "RELAY_POST_BUDGET_S", 0.05)
    mcp = _PostMcp()
    if mode == "resolved":
        mcp.error = ThreadResolvedError("resolved")
    elif mode == "no_msg_id":
        mcp.msg_id = ""
    elif mode == "raises":
        mcp.error = RuntimeError("boom")
    else:
        mcp.hang = True
    with pytest.raises(RunTimeoutError) as excinfo:
        await post_run_timeout_notice(
            mcp, project="p", thread_id="t", event=_event(), author=CONDUCTOR_RELAY_AUTHOR
        )
    assert excinfo.value.code == RUN_TIMEOUT_EXIT_CODE == 5


# --------------------------------------------------------------------------- #
# run_conductor end to end (the conductor itself replaced by a hang)
# --------------------------------------------------------------------------- #


def _settings(tmp_path: Path, *, run_budget_s: float = 0.1) -> MindwireSettings:
    return MindwireSettings(
        loop=Stage3LoopConfig(project="spirrow-mindwire", repo_dir=tmp_path),
        conductor=ConductorConfig(
            task_thread_id="T-cond", run_budget_s=run_budget_s, run_hard_budget_s=100
        ),
    )


def _hanging_once(monkeypatch: pytest.MonkeyPatch, *, phase: str = "implementer.dispatch") -> None:
    async def _once(_settings: Any, **kw: Any) -> ConductorOutcome:
        run_phase: RunPhase = kw["run_phase"]
        run_phase.rounds_started = 2
        with run_phase.enter(phase):
            await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(loop_runner, "_run_conductor_once", _once)


@pytest.mark.anyio
async def test_timeout_posts_the_notice_and_stops_at_human(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _hanging_once(monkeypatch)
    mcp = _PostMcp(msg_id="m-timeout")
    outcome = await loop_runner.run_conductor(_settings(tmp_path), mcp=mcp)
    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.last_msg_id == "m-timeout"
    assert outcome.rounds == 2  # the round that was cut off counts (RunPhase.rounds_started)
    assert len(mcp.posts) == 1 and mcp.posts[0]["author"] == CONDUCTOR_RELAY_AUTHOR
    marker = parse_stop_marker(mcp.posts[0]["content"])
    assert marker is not None and marker["phase"] == "implementer.dispatch"
    messages = [r.getMessage() for r in caplog.records]
    stopped = [m for m in messages if m.startswith("conductor stopped:")]
    assert stopped == [
        "conductor stopped: reason=human rounds=2 forced_naysayer=0 "
        "forced_naysayer_saveable=0 last_msg=m-timeout"
    ]
    assert len(_timeout_lines(caplog)) == 1


@pytest.mark.anyio
async def test_timeout_whose_notice_fails_exits_5_with_the_line_already_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _hanging_once(monkeypatch)
    mcp = _PostMcp(error=RuntimeError("chatroom down"))
    with pytest.raises(RunTimeoutError) as excinfo:
        await loop_runner.run_conductor(_settings(tmp_path), mcp=mcp)
    assert excinfo.value.code == 5
    assert len(_timeout_lines(caplog)) == 1
    assert not any(r.getMessage().startswith("conductor stopped:") for r in caplog.records)


def test_timeout_exit_code_reaches_the_process_through_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # main()'s catch-all must not turn the run timeout into an adapter_error exit 1.
    _hanging_once(monkeypatch)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    monkeypatch.delenv("MINDWIRE_MAGICKIT_MCP_URL", raising=False)
    monkeypatch.setattr(
        loop_runner, "StreamableHttpChatroomMcp", lambda: _PostMcp(error=RuntimeError("down"))
    )
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == RUN_TIMEOUT_EXIT_CODE


@pytest.mark.anyio
async def test_budgets_are_checked_before_anything_is_read(tmp_path: Path) -> None:
    settings = MindwireSettings(
        loop=Stage3LoopConfig(project="spirrow-mindwire", repo_dir=tmp_path),
        conductor=ConductorConfig(task_thread_id="T-cond", run_budget_s=100, run_hard_budget_s=120),
    )
    mcp = _PostMcp()
    with pytest.raises(SystemExit, match="run_hard_budget_s"):
        await loop_runner.run_conductor(settings, mcp=mcp)
    assert mcp.posts == []


def test_validate_budgets() -> None:
    validate_budgets(run_budget_s=100, run_hard_budget_s=100 + RELAY_POST_BUDGET_S + 1)
    with pytest.raises(SystemExit):
        validate_budgets(run_budget_s=100, run_hard_budget_s=100 + RELAY_POST_BUDGET_S)
    with pytest.raises(SystemExit):
        validate_budgets(run_budget_s=0, run_hard_budget_s=100)
    # The shipped defaults satisfy the ordering, with the 15-minute margin of msg-5496 W-5.
    validate_budgets(
        run_budget_s=DEFAULT_CONDUCTOR_RUN_BUDGET_S,
        run_hard_budget_s=DEFAULT_CONDUCTOR_RUN_HARD_BUDGET_S,
    )
    assert DEFAULT_CONDUCTOR_RUN_HARD_BUDGET_S - DEFAULT_CONDUCTOR_RUN_BUDGET_S == 15 * 60
    assert DEFAULT_CONDUCTOR_RUN_HARD_BUDGET_S < 4 * 3600  # under ExecutionTimeLimit PT4H


# --------------------------------------------------------------------------- #
# the Conductor reports its phase, and a cancellation is not an adapter error
# --------------------------------------------------------------------------- #


class _PhaseDispatcher(_ScriptedDispatcher):
    def __init__(self, mcp: _FakeChatroomMcp, phase: RunPhase, *, hang: bool = False) -> None:
        super().__init__(mcp, {Role.NAYSAYER: ["ok\n\nNEXT: human"]})
        self._phase = phase
        self._hang = hang
        self.seen: list[str] = []

    async def spawn_instance(self, thread_ref: Any, role: Role, instance_id: str) -> Any:
        self.seen.append(self._phase.current)
        return await super().spawn_instance(thread_ref, role, instance_id)

    async def dispatch(self, handle: Any, event: Any) -> None:
        self.seen.append(self._phase.current)
        if self._hang:
            await asyncio.Event().wait()
        await super().dispatch(handle, event)


def _phase_conductor(
    mcp: _FakeChatroomMcp, dispatcher: Any, phase: RunPhase, slot: ConductorStopSlot
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        stop_slot=slot,
        run_phase=phase,
    )


@pytest.mark.anyio
async def test_conductor_sets_the_phase_around_spawn_and_dispatch() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    phase = RunPhase()
    dispatcher = _PhaseDispatcher(mcp, phase)
    outcome = await _phase_conductor(mcp, dispatcher, phase, ConductorStopSlot()).run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert dispatcher.seen == ["naysayer.spawn", "naysayer.dispatch"]
    assert phase.current == PHASE_IDLE
    assert phase.rounds_started == 2


@pytest.mark.anyio
async def test_cancelled_dispatch_records_no_adapter_error_snapshot() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    phase = RunPhase()
    slot = ConductorStopSlot()
    conductor = _phase_conductor(mcp, _PhaseDispatcher(mcp, phase, hang=True), phase, slot)
    _result, event = await run_with_budget(
        conductor.run, budget_s=0.1, run_phase=phase, project="p", thread="t"
    )
    assert event is not None and event.fields["phase"] == "naysayer.dispatch"
    assert slot.snapshot is None


# --------------------------------------------------------------------------- #
# W-4: stop_kind closes over every entry path
# --------------------------------------------------------------------------- #


def test_every_stop_reason_has_a_decided_stop_kind() -> None:
    assert set(STOP_KIND_BY_STOP_REASON) == set(StopReason)


def test_stop_reasons_that_section_6_3_sends_to_stalled_have_a_value() -> None:
    for reason, kind in [
        (StopReason.EMPTY, StopKind.EMPTY_THREAD),
        (StopReason.NO_HANDOFF, StopKind.NO_HANDOFF),
        (StopReason.NO_PROGRESS, StopKind.NO_PROGRESS),
        (StopReason.ROUND_CAP, StopKind.ROUND_CAP),
        (StopReason.STALLED, StopKind.STALLED),
    ]:
        assert STOP_KIND_BY_STOP_REASON[reason] is kind


def test_every_stop_event_maps_to_a_stop_kind() -> None:
    assert STOP_KIND_BY_EVENT_KIND == {
        "conductor.stalled": StopKind.STALLED,
        "conductor.stand_down": StopKind.STAND_DOWN,
        "spawn.timeout": StopKind.SPAWN_TIMEOUT,
        EVENT_KIND_RUN_TIMEOUT: StopKind.RUN_TIMEOUT,
        EVENT_KIND_RUN_KILLED: StopKind.RUN_KILLED,
    }


def test_stop_kind_set_is_the_designed_twelve() -> None:
    assert {k.value for k in StopKind} == {
        "stalled",
        "stand_down",
        "spawn_timeout",
        "quarantine",
        "silent",
        "human_spin",
        "run_timeout",
        "run_killed",
        "empty_thread",
        "no_handoff",
        "no_progress",
        "round_cap",
    }


# --------------------------------------------------------------------------- #
# literals shared with PowerShell
# --------------------------------------------------------------------------- #


def _ps_assignment(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^\${name}\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    assert m is not None, f"${name} not assigned in {path.name}"
    return m.group(1).strip("'\"")


def test_exit_codes_are_distinct_and_shared_with_powershell() -> None:
    codes = [
        STAND_DOWN_EXIT_CODE,
        STALLED_EXIT_CODE,
        RUN_TIMEOUT_EXIT_CODE,
        RUN_KILLED_EXIT_CODE,
        RUN_KILL_UNCONFIRMED_EXIT_CODE,
    ]
    assert codes == [3, 4, 5, 6, 7]
    lib = _REPO / "deploy" / "lib" / "ConductorBudget.ps1"
    sweep = _REPO / "deploy" / "run-conductor-scheduled.ps1"
    assert int(_ps_assignment(lib, "ConductorRunKilledExitCode")) == RUN_KILLED_EXIT_CODE
    assert int(_ps_assignment(lib, "ConductorKillUnconfirmedExitCode")) == (
        RUN_KILL_UNCONFIRMED_EXIT_CODE
    )
    assert int(_ps_assignment(sweep, "ConductorKillUnconfirmedExitCode")) == (
        RUN_KILL_UNCONFIRMED_EXIT_CODE
    )


def test_powershell_mirrors_the_event_name_and_the_hard_default() -> None:
    lib = _REPO / "deploy" / "lib" / "ConductorBudget.ps1"
    assert _ps_assignment(lib, "ConductorRunKilledEventKind") == EVENT_KIND_RUN_KILLED
    assert float(_ps_assignment(lib, "DefaultConductorRunHardBudgetSeconds")) == (
        DEFAULT_CONDUCTOR_RUN_HARD_BUDGET_S
    )
