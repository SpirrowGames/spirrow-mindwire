"""PR-B of T-spawn-via-claude-bg: the ``claude --bg`` adapter, against a fake CLI.

Nothing here runs the real ``claude``: whether tomtebo's CLI accepts a model id is an
operator check on that host (Bohr msg-6937), not something a CI runner can prove.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.adapters.claude_bg import (
    EVENT_ID_MISMATCH,
    EVENT_PENDING_EXPIRED,
    EVENT_UNKNOWN_STATE,
    REASON_DOUBLE_SPAWN,
    REASON_MODEL_UNAVAILABLE,
    REASON_NOT_LISTED,
    REASON_SESSION_LOST,
    REASON_SPAWN_FAILED,
    REASON_WORKSPACE_UNTRUSTED,
    BgPhase,
    BgTurnPlan,
    ClaudeBgAdapter,
    ClaudeBgSpawnError,
    CommandResult,
    classify_spawn_failure,
    mint_name,
    parse_agent_list,
    phase_of,
)
from spirrow_mindwire.conductor.model_policy import ModelChoice
from spirrow_mindwire.exceptions import (
    AdapterDeliveryError,
    AdapterHaltError,
    AdapterSpawnError,
)
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

REPO = "/repo"
THREAD = ThreadRef("spirrow-mindwire", "T-demo", "chatroom://T-demo")
SONNET = ModelChoice("claude-sonnet-5-5", ("C1:pass:mechanical",))
OPUS = ModelChoice("claude-opus-5-5", ("C1:fail:no-turn-kind",))


@dataclass
class FakeCli:
    """Scripted ``claude`` / ``git``. ``agents`` holds the live list (dicts, as the CLI prints)."""

    bg_result: CommandResult = field(
        default_factory=lambda: CommandResult(0, "backgrounded · {id}\n", "")
    )
    agents: list[dict[str, Any]] = field(default_factory=list)
    on_bg: Callable[[str], list[dict[str, Any]]] | None = None
    list_fails: bool = False
    rm_fails: bool = False
    rm_keeps: bool = False
    git_dirty: bool = False
    calls: list[list[str]] = field(default_factory=list)

    async def run(self, argv: Sequence[str], *, cwd: str) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        assert cwd == REPO
        if argv[0] == "git":
            if argv[1] == "rev-parse":
                return CommandResult(0, "abc123\n", "")
            return CommandResult(0, " M x.py\n" if self.git_dirty else "", "")
        sub = argv[1]
        if sub == "--bg":
            name = argv[argv.index("--name") + 1]
            if self.bg_result.returncode == 0:
                new = self.on_bg(name) if self.on_bg else [_agent("d6390382", name)]
                self.agents.extend(new)
                first = new[0]["id"] if new else "00000000"
                return CommandResult(0, self.bg_result.stdout.format(id=first), "")
            return self.bg_result
        if sub == "agents":
            if self.list_fails:
                return CommandResult(1, "", "boom")
            return CommandResult(0, json.dumps(self.agents), "")
        if sub == "stop":
            return CommandResult(0, "", "")
        if sub == "rm":
            if self.rm_fails:
                return CommandResult(1, "", "rm refused")
            if not self.rm_keeps:
                self.agents = [a for a in self.agents if a.get("id") != argv[2]]
            return CommandResult(0, "", "")
        raise AssertionError(argv)

    def count(self, sub: str) -> int:
        return sum(1 for c in self.calls if c[0] == "claude" and c[1] == sub)


def _agent(id_: str, name: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "sessionId": f"{id_}-uuid",
        "cwd": REPO,
        "kind": "background",
        "name": name,
        "startedAt": 1,
        "state": "running",
        "pid": 42,
        "status": "busy",
    }
    base.update(extra)
    return base


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


async def _setup(
    cli: FakeCli,
    choice: ModelChoice = OPUS,
    clock: Clock | None = None,
    on_event_hook: Callable[[Event], Any] | None = None,
) -> tuple[ClaudeBgAdapter, SessionHandle, list[Event], list[bool | None]]:
    events: list[Event] = []
    seen_clean: list[bool | None] = []
    clock = clock or Clock()

    async def planner(h: SessionHandle, e: ChatroomEvent, clean: bool | None) -> BgTurnPlan:
        seen_clean.append(clean)
        return BgTurnPlan("Heisenberg", 3, "do the turn", choice)

    async def on_reply(_: ReplyDraft) -> None:
        raise AssertionError("the bg session posts its own handoff")

    async def on_event(e: Event) -> None:
        events.append(e)
        if on_event_hook is not None:
            await on_event_hook(e)

    adapter = ClaudeBgAdapter(
        repo_dir=REPO, planner=planner, runner=cli, sleep=clock.sleep, clock=clock
    )
    ctx = SpawnContext(on_reply, on_event, Role.IMPLEMENTER, "implementer-1")
    handle = await adapter.spawn(THREAD, Role.IMPLEMENTER, ctx)
    return adapter, handle, events, seen_clean


def _event(body: str = "TURN-KIND: mechanical") -> ChatroomEvent:
    return ChatroomEvent(
        "e1",
        EventType.NEW_MESSAGE,
        THREAD,
        datetime(2026, 10, 14, tzinfo=UTC),
        NewMessagePayload("msg-1", "Bohr", body, None),
    )


# --------------------------------------------------------------------------- #
# Launch command line and record
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_launch_passes_chosen_model_effort_and_name() -> None:
    cli = FakeCli()
    adapter, handle, _, seen_clean = await _setup(cli, SONNET)
    await adapter.deliver_event(handle, _event())
    bg = next(c for c in cli.calls if c[:2] == ["claude", "--bg"])
    assert bg[bg.index("--agent") + 1] == "Heisenberg"
    assert bg[bg.index("--model") + 1] == "claude-sonnet-5-5"
    assert bg[bg.index("--effort") + 1] == "high"
    assert "--dangerously-skip-permissions" in bg
    assert bg[-1] == "do the turn"
    name = bg[bg.index("--name") + 1]
    assert name.startswith("T-demo-r3-") and len(name) == len("T-demo-r3-") + 6
    assert seen_clean == [True]
    rec = adapter.last_launch(handle)
    assert rec is not None
    assert (rec.id, rec.session_id, rec.name) == ("d6390382", "d6390382-uuid", name)
    assert (rec.model, rec.model_reasons, rec.effort) == (SONNET.model, SONNET.reasons, "high")
    assert (rec.start_head, rec.start_worktree_clean, rec.round) == ("abc123", True, 3)


@pytest.mark.anyio
async def test_launch_record_drops_rollback_era_fields() -> None:
    # msg-6888 / msg-6935: only start_head and the C6 input are recorded at launch.
    cli = FakeCli()
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    rec = adapter.last_launch(handle)
    for gone in ("start_branch", "start_local_heads", "start_tracking_refs"):
        assert not hasattr(rec, gone)


@pytest.mark.anyio
async def test_dirty_worktree_reaches_the_planner() -> None:
    cli = FakeCli(git_dirty=True)
    adapter, handle, _, seen_clean = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    assert seen_clean == [False]
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.start_worktree_clean is False


def test_mint_name_shape() -> None:
    assert mint_name("T-x", 2, "a1b2c3") == "T-x-r2-a1b2c3"


# --------------------------------------------------------------------------- #
# Spawn failure ladder (msg-6937 steps 1-3): one test per path, never relaunched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("Error: Workspace not trusted. Run `claude` here first.", REASON_WORKSPACE_UNTRUSTED),
        ("API Error: 404 model: claude-sonnet-5-5 not found", REASON_MODEL_UNAVAILABLE),
        ("Segmentation fault", REASON_SPAWN_FAILED),
    ],
)
@pytest.mark.anyio
async def test_spawn_failure_paths_stand_down_without_relaunch(stderr: str, reason: str) -> None:
    cli = FakeCli(bg_result=CommandResult(2, "", stderr))
    adapter, handle, _, _ = await _setup(cli, SONNET)
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    err = info.value
    assert err.reason == reason
    assert isinstance(err, AdapterSpawnError) and isinstance(err, AdapterDeliveryError)
    body = err.stand_down_body()
    assert body.startswith(f"STAND-DOWN: {reason}")
    assert "exit code: 2" in body and stderr in body
    # Exactly one launch, on the model that was chosen: no Opus retry.
    assert cli.count("--bg") == 1
    assert cli.count("agents") == 0
    assert adapter.last_launch(handle) is None


def test_untrusted_wins_over_model_wording() -> None:
    assert (
        classify_spawn_failure(1, "Workspace not trusted; invalid model")
        == REASON_WORKSPACE_UNTRUSTED
    )
    assert classify_spawn_failure(0, "Workspace not trusted") is None


def test_stand_down_body_truncates_stderr() -> None:
    err = ClaudeBgSpawnError(REASON_SPAWN_FAILED, "x", returncode=1, stderr="e" * 5000)
    assert "e" * 500 in err.stand_down_body()
    assert "e" * 501 not in err.stand_down_body()


# --------------------------------------------------------------------------- #
# Exit 0: resolution by name (V-1)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_zero_matches_is_not_listed() -> None:
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    assert info.value.reason == REASON_NOT_LISTED
    assert cli.count("--bg") == 1
    assert cli.count("agents") > 1  # it polled before giving up


@pytest.mark.anyio
async def test_match_that_appears_late_is_adopted() -> None:
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, _, _ = await _setup(cli)
    original = cli.run
    polls = 0

    async def run(argv: Sequence[str], *, cwd: str) -> CommandResult:
        nonlocal polls
        if list(argv[1:2]) == ["agents"]:
            polls += 1
            if polls == 3:
                name = next(c for c in cli.calls if c[1] == "--bg")
                cli.agents.append(_agent("aaaaaaaa", name[name.index("--name") + 1]))
        return await original(argv, cwd=cwd)

    cli.run = run  # type: ignore[method-assign]
    await adapter.deliver_event(handle, _event())
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.id == "aaaaaaaa"


@pytest.mark.anyio
async def test_double_spawn_removes_every_match() -> None:
    cli = FakeCli(on_bg=lambda name: [_agent("11111111", name), _agent("22222222", name)])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    assert info.value.reason == REASON_DOUBLE_SPAWN
    assert set(info.value.session_ids) == {"11111111", "22222222"}
    assert cli.agents == []  # AC-1: all removed
    assert cli.count("stop") == 2 and cli.count("rm") == 2


@pytest.mark.anyio
async def test_stdout_id_mismatch_is_logged_not_fatal() -> None:
    cli = FakeCli(bg_result=CommandResult(0, "backgrounded · e8284921\n", ""))
    adapter, handle, events, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.id == "d6390382"
    mismatch = [e for e in events if e.kind == EVENT_ID_MISMATCH]
    assert len(mismatch) == 1
    assert mismatch[0].fields["stdout_id"] == "e8284921"


@pytest.mark.anyio
async def test_other_sessions_with_other_names_are_ignored() -> None:
    cli = FakeCli()
    cli.agents.append(_agent("99999999", "T-other-r1-abcdef"))
    cli.agents.append({"pid": 7, "status": "idle"})  # interactive element: no id
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.id == "d6390382"


# --------------------------------------------------------------------------- #
# State table (V-4), lost sessions (V-3), health
# --------------------------------------------------------------------------- #


def test_phase_table() -> None:
    agents = parse_agent_list(
        json.dumps(
            [
                _agent("a0000000", "n", state="done", status="idle"),
                _agent("b0000000", "n", state="running", status="idle"),
                _agent("c0000000", "n", state="blocked", status="busy"),
            ]
        )
    )
    assert [phase_of(a) for a in agents] == [BgPhase.DONE, BgPhase.IDLE, BgPhase.RUNNING]
    assert phase_of(None) is BgPhase.LOST


@pytest.mark.anyio
async def test_unknown_state_runs_on_and_is_logged_once() -> None:
    cli = FakeCli(on_bg=lambda name: [_agent("d6390382", name, state="weird")])
    adapter, handle, events, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    for _ in range(3):
        obs = await adapter.observe(handle)
        assert obs.phase is BgPhase.RUNNING
    assert [e.fields["state"] for e in events if e.kind == EVENT_UNKNOWN_STATE] == ["weird"]
    assert (await adapter.health(handle)).state is SessionState.PROCESSING


@pytest.mark.anyio
async def test_session_gone_from_list_is_lost() -> None:
    cli = FakeCli()
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    cli.agents.clear()
    obs = await adapter.observe(handle)
    assert obs.phase is BgPhase.LOST and obs.reason == REASON_SESSION_LOST
    health = await adapter.health(handle)
    assert health.state is SessionState.FAILED
    assert health.error is not None and REASON_SESSION_LOST in health.error.message
    assert cli.count("respawn") == 0


# --------------------------------------------------------------------------- #
# stop -> rm (V-2) and orphan sweep
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_halt_stops_then_removes_and_is_idempotent() -> None:
    cli = FakeCli(on_bg=lambda name: [_agent("d6390382", name, state="done", status="idle")])
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    await adapter.halt(handle)
    subs = [c[1] for c in cli.calls if c[0] == "claude" and c[1] in ("stop", "rm")]
    assert subs == ["stop", "rm"]
    assert cli.agents == []  # AC-2: the id is gone from the list
    await adapter.halt(handle)
    assert cli.count("rm") == 1
    assert (await adapter.health(handle)).state is SessionState.HALTED


@pytest.mark.anyio
async def test_rm_failure_is_halt_error() -> None:
    cli = FakeCli()
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    cli.rm_fails = True
    with pytest.raises(AdapterHaltError):
        await adapter.halt(handle)


@pytest.mark.anyio
async def test_rm_that_leaves_the_session_listed_is_halt_error() -> None:
    cli = FakeCli(rm_keeps=True)
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    with pytest.raises(AdapterHaltError, match="still listed"):
        await adapter.halt(handle)


@pytest.mark.anyio
async def test_next_turn_ends_previous_session_first() -> None:
    cli = FakeCli(on_bg=lambda name: [_agent(f"{len(cli.calls):08x}", name)])
    adapter, handle, _, _ = await _setup(cli)
    await adapter.deliver_event(handle, _event())
    first = adapter.last_launch(handle)
    await adapter.deliver_event(handle, _event())
    second = adapter.last_launch(handle)
    assert first is not None and second is not None and first.id != second.id
    assert [a["id"] for a in cli.agents] == [second.id]
    assert cli.count("--bg") == 2


@pytest.mark.anyio
async def test_reap_orphans_touches_only_managed_names_in_repo_dir() -> None:
    cli = FakeCli()
    cli.agents = [
        _agent("aaaaaaa1", "T-x-r1-abcdef"),  # orphan -> reaped
        _agent("aaaaaaa2", "T-x-r2-abcdef"),  # live -> kept
        _agent("aaaaaaa3", "verify-cc-reply"),  # someone else's name -> kept
        _agent("aaaaaaa4", "T-x-r1-abcdef", cwd="/elsewhere"),  # other cwd -> kept
        {"pid": 7, "status": "idle"},  # interactive -> kept
    ]
    adapter, _, _, _ = await _setup(cli)
    report = await adapter.reap_orphans(frozenset({"aaaaaaa2"}))
    assert report.reaped == ("aaaaaaa1",)
    assert report.failed == ()
    assert [a.get("id") for a in cli.agents] == ["aaaaaaa2", "aaaaaaa3", "aaaaaaa4", None]


@pytest.mark.anyio
async def test_reap_reports_rm_failures() -> None:
    cli = FakeCli(rm_fails=True)
    cli.agents = [_agent("aaaaaaa1", "T-x-r1-abcdef")]
    adapter, _, _, _ = await _setup(cli)
    report = await adapter.reap_orphans(frozenset())
    assert report.reaped == ()
    assert report.failed == (("aaaaaaa1", "`claude rm` exited 1: rm refused"),)


# --------------------------------------------------------------------------- #
# Confirmed cleanup (PR-gate msg on #481 @ 8a1f614, finding 1)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_reap_does_not_report_an_agent_rm_left_listed() -> None:
    cli = FakeCli(rm_keeps=True)
    cli.agents = [_agent("aaaaaaa1", "T-x-r1-abcdef")]
    adapter, _, _, _ = await _setup(cli)
    report = await adapter.reap_orphans(frozenset())
    assert report.reaped == ()
    assert report.failed == (("aaaaaaa1", "still listed after `claude rm`"),)


@pytest.mark.anyio
async def test_reap_cannot_confirm_when_the_list_fails_after_rm() -> None:
    cli = FakeCli()
    cli.agents = [_agent("aaaaaaa1", "T-x-r1-abcdef")]
    adapter, _, _, _ = await _setup(cli)
    original = cli.run

    async def run(argv: Sequence[str], *, cwd: str) -> CommandResult:
        if list(argv[1:2]) == ["rm"]:
            cli.list_fails = True
        return await original(argv, cwd=cwd)

    cli.run = run  # type: ignore[method-assign]
    report = await adapter.reap_orphans(frozenset())
    assert report.reaped == ()
    assert len(report.failed) == 1 and "could not confirm removal" in report.failed[0][1]


@pytest.mark.parametrize("mode", ["rm_fails", "rm_keeps"])
@pytest.mark.anyio
async def test_double_spawn_cleanup_failure_is_reported_and_retried_by_halt(mode: str) -> None:
    cli = FakeCli(on_bg=lambda name: [_agent("11111111", name), _agent("22222222", name)])
    setattr(cli, mode, True)
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    assert info.value.reason == REASON_DOUBLE_SPAWN
    assert "all were stopped and removed" not in info.value.detail
    assert "NOT all could be confirmed removed" in info.value.detail
    assert "11111111" in info.value.detail and "22222222" in info.value.detail
    # The launch is not forgotten: halt() reconciles it by name.
    name = adapter.pending_launch(handle)
    assert name is not None and {a["name"] for a in cli.agents} == {name}
    with pytest.raises(AdapterHaltError, match="not cleaned up"):
        await adapter.halt(handle)
    setattr(cli, mode, False)
    await adapter.halt(handle)
    assert cli.agents == []
    assert adapter.pending_launch(handle) is None
    assert (await adapter.health(handle)).state is SessionState.HALTED


@pytest.mark.anyio
async def test_double_spawn_partial_cleanup_names_what_was_removed() -> None:
    cli = FakeCli(on_bg=lambda name: [_agent("11111111", name), _agent("22222222", name)])
    adapter, handle, _, _ = await _setup(cli)
    original = cli.run

    async def run(argv: Sequence[str], *, cwd: str) -> CommandResult:
        if list(argv[1:3]) == ["rm", "22222222"]:
            return CommandResult(1, "", "rm refused")
        return await original(argv, cwd=cwd)

    cli.run = run  # type: ignore[method-assign]
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    detail = info.value.detail
    assert "22222222 (`claude rm` exited 1: rm refused)" in detail
    assert "removed: 11111111" in detail
    assert [a["id"] for a in cli.agents] == ["22222222"]


@pytest.mark.anyio
async def test_fully_removed_double_spawn_leaves_nothing_pending() -> None:
    cli = FakeCli(on_bg=lambda name: [_agent("11111111", name), _agent("22222222", name)])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError):
        await adapter.deliver_event(handle, _event())
    assert adapter.pending_launch(handle) is None


# --------------------------------------------------------------------------- #
# Cancellation (PR-gate msg on #481 @ 8a1f614, finding 2)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_subprocess_runner_kills_the_child_on_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import asyncio
    import sys

    from spirrow_mindwire.adapters import claude_bg

    spawned: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    runner = claude_bg.SubprocessRunner(timeout_s=60)
    task = asyncio.create_task(
        runner.run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(tmp_path))
    )
    while not spawned:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned[0].returncode is not None  # killed and reaped, not left running


@pytest.mark.anyio
async def test_cancelled_launch_is_reconciled_by_name_on_halt() -> None:
    import asyncio

    cli = FakeCli()
    adapter, handle, _, _ = await _setup(cli)
    original = cli.run
    entered = asyncio.Event()

    async def run(argv: Sequence[str], *, cwd: str) -> CommandResult:
        if list(argv[1:2]) == ["--bg"]:
            # The CLI detaches the session, then the caller is cancelled before it returns.
            await original(argv, cwd=cwd)
            entered.set()
            await asyncio.Event().wait()
        return await original(argv, cwd=cwd)

    cli.run = run  # type: ignore[method-assign]
    task = asyncio.create_task(adapter.deliver_event(handle, _event()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.last_launch(handle) is None
    name = adapter.pending_launch(handle)
    assert name is not None and [a["name"] for a in cli.agents] == [name]
    await adapter.halt(handle)
    assert cli.agents == []
    assert cli.count("stop") == 1 and cli.count("rm") == 1
    assert adapter.pending_launch(handle) is None


@pytest.mark.anyio
async def test_pending_launch_is_reconciled_before_the_next_turn() -> None:
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    assert info.value.reason == REASON_NOT_LISTED
    late = adapter.pending_launch(handle)
    assert late is not None
    cli.agents.append(_agent("abababab", late))  # it shows up after the deadline
    cli.on_bg = None
    await adapter.deliver_event(handle, _event())
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.name != late
    assert [a["id"] for a in cli.agents] == [rec.id]


@pytest.mark.anyio
async def test_unreconcilable_pending_launch_blocks_the_next_turn() -> None:
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError):
        await adapter.deliver_event(handle, _event())
    cli.list_fails = True
    bg_before = cli.count("--bg")
    with pytest.raises(AdapterHaltError, match="could not reconcile pending launch"):
        await adapter.deliver_event(handle, _event())
    assert cli.count("--bg") == bg_before


# --------------------------------------------------------------------------- #
# Launch ownership and settlement (PR-gate on #481 @ aab123b)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_cancel_inside_mismatch_logging_still_lets_halt_remove_the_session() -> None:
    import asyncio

    async def hang_on_mismatch(e: Event) -> None:
        if e.kind == EVENT_ID_MISMATCH:
            await asyncio.Event().wait()

    stdout = "backgrounded · e8284921" + chr(10)
    cli = FakeCli(bg_result=CommandResult(0, stdout, ""))
    adapter, handle, events, _ = await _setup(cli, on_event_hook=hang_on_mismatch)
    task = asyncio.create_task(adapter.deliver_event(handle, _event()))
    while not any(e.kind == EVENT_ID_MISMATCH for e in events):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.id == "d6390382"  # owned before the log was awaited
    assert [a["id"] for a in cli.agents] == ["d6390382"]
    await adapter.halt(handle)
    assert cli.agents == []
    assert (await adapter.health(handle)).state is SessionState.HALTED


@pytest.mark.anyio
async def test_unlisted_pending_launch_survives_halt_until_it_appears() -> None:
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError) as info:
        await adapter.deliver_event(handle, _event())
    assert info.value.reason == REASON_NOT_LISTED
    late = adapter.pending_launch(handle)
    assert late is not None
    # First reconciliation sees nothing: the name is NOT forgotten and halt is not HALTED.
    with pytest.raises(AdapterHaltError, match="not listed yet"):
        await adapter.halt(handle)
    assert adapter.pending_launch(handle) == late
    health = await adapter.health(handle)
    assert health.state is SessionState.HALTING and health.details == {"pending": late}
    with pytest.raises(AdapterDeliveryError, match="halted"):
        await adapter.deliver_event(handle, _event())
    # The detached session surfaces afterwards; the next halt() reconciles it.
    cli.agents.append(_agent("abababab", late))
    await adapter.halt(handle)
    assert cli.agents == []
    assert adapter.pending_launch(handle) is None
    assert (await adapter.health(handle)).state is SessionState.HALTED
    await adapter.halt(handle)  # idempotent once settled
    assert cli.count("rm") == 1


@pytest.mark.anyio
async def test_unlisted_pending_launch_refuses_the_next_turn_until_it_appears() -> None:
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, _, _ = await _setup(cli)
    with pytest.raises(ClaudeBgSpawnError):
        await adapter.deliver_event(handle, _event())
    late = adapter.pending_launch(handle)
    assert late is not None
    cli.on_bg = None
    bg_before = cli.count("--bg")
    with pytest.raises(AdapterHaltError, match="not listed yet"):
        await adapter.deliver_event(handle, _event())
    assert cli.count("--bg") == bg_before  # no second session beside the unsettled one
    assert adapter.pending_launch(handle) == late
    cli.agents.append(_agent("abababab", late))
    await adapter.deliver_event(handle, _event())
    rec = adapter.last_launch(handle)
    assert rec is not None and rec.name != late
    assert [a["id"] for a in cli.agents] == [rec.id]


@pytest.mark.anyio
async def test_pending_launch_never_listed_expires_after_the_settle_window() -> None:
    clock = Clock()
    cli = FakeCli(on_bg=lambda name: [])
    adapter, handle, events, _ = await _setup(cli, clock=clock)
    with pytest.raises(ClaudeBgSpawnError):
        await adapter.deliver_event(handle, _event())
    late = adapter.pending_launch(handle)
    clock.t += 120.0
    cli.on_bg = None
    await adapter.deliver_event(handle, _event())
    expired = [e for e in events if e.kind == EVENT_PENDING_EXPIRED]
    assert len(expired) == 1 and expired[0].fields["name"] == late
    rec = adapter.last_launch(handle)
    assert rec is not None and adapter.pending_launch(handle) is None
