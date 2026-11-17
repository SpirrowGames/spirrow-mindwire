"""Unit 2a wiring of turn isolation (ADR-2026-10-09-23; design msg-9439 B, tests msg-9441 B).

The boundary these tests fix is "a turn starts down exactly one path, and only after this
launch's check passed":

1. ``[turn.isolation] user`` unset: check 0 calls, launcher 0 calls, direct spawn as today (INV-0).
2. set, and the check fails / times out / raises: exit 10, exactly one stop line carrying no
   value, and **neither** the launcher route nor the direct spawn is called. One test per case.
3. set, check passes: the launcher route is called once, the direct spawn 0 times.
4. set, the launcher route raises: a named stop, and no fall-back to the direct spawn.
5. a UPN-form user stops at parse time with the explicit reason; check and launcher 0 calls.

No Win32 API is called: the check and both client factories are fakes. The composition-root
cases run the real ``loop_runner._build_dispatcher`` so the wiring under test is the production
one, not a re-creation of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import loop_runner
from spirrow_mindwire.adapters import implementer as implementer_mod
from spirrow_mindwire.config import (
    MindwireSettings,
    Stage3LoopConfig,
    TurnConfig,
    TurnIsolationConfig,
)
from spirrow_mindwire.turn_isolation import check as check_mod
from spirrow_mindwire.turn_isolation import gate as gate_mod
from spirrow_mindwire.turn_isolation.errors import (
    SECRETS_STOP_EXIT_CODE,
    STOP_LINE_PREFIX,
    TURN_ISOLATION_CHECK_FAILED,
    TURN_ISOLATION_CHECK_TIMED_OUT,
    TURN_LAUNCH_ROUTE_FAILED,
    TURN_LAUNCH_ROUTE_PENDING_SPIKE,
    TURN_USER_UPN_NOT_SUPPORTED,
    TurnIsolationStop,
)
from spirrow_mindwire.turn_isolation.launcher import TurnUser
from spirrow_mindwire.value_objects import Role

# A value that must never reach a stop line (stands in for a secret in an exception message).
_SECRET = "ghp_SECRETVALUE0123456789"


class _InertMcp:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError(f"the composition root should not call {name}")


class _Calls:
    """Counts the three ways a turn could start (or be checked)."""

    def __init__(self) -> None:
        self.check = 0
        self.launcher = 0
        self.direct = 0


class _ConnectableClient:
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _NoJob:
    def create_job(self, _sid: str) -> Any:
        raise NotImplementedError  # POSIX path: no Job Object, spawn proceeds


def _settings(tmp_path: Path, user: str | None) -> MindwireSettings:
    return MindwireSettings(
        loop=Stage3LoopConfig(repo_dir=tmp_path),
        turn=TurnConfig(isolation=TurnIsolationConfig(user=user)),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Spy every start path at the module seams the production wiring reads."""
    c = _Calls()

    def _direct(_options: Any) -> Any:
        c.direct += 1
        return _ConnectableClient()

    def _launcher(_options: Any) -> Any:
        c.launcher += 1
        return _ConnectableClient()

    monkeypatch.setattr(implementer_mod, "_default_client_factory", _direct)
    monkeypatch.setattr(gate_mod, "pending_spike_launch_factory", _launcher)
    return c


def _check_that(c: _Calls, outcome: BaseException | None) -> Any:
    def _check(user: str, clones: Any, **_kw: Any) -> bool:
        c.check += 1
        if outcome is not None:
            raise outcome
        return True

    return _check


def _build(settings: MindwireSettings) -> Any:
    _, registry, _ = loop_runner._build_dispatcher(
        settings, mcp=_InertMcp(), proposer=None, implementer=None, naysayer=None
    )
    return registry.qualified_for(Role.IMPLEMENTER)[0]


async def _spawn(adapter: Any) -> None:
    from spirrow_mindwire.ports import SpawnContext
    from spirrow_mindwire.value_objects import ThreadRef

    async def _noop(_x: Any) -> None:
        return None

    adapter._inference_base_url = "http://lexora.invalid"
    adapter._job_module = _NoJob()
    await adapter.spawn(
        ThreadRef(project_id="p", thread_id="T", chatroom_uri="mc://t/1"),
        Role.IMPLEMENTER,
        SpawnContext(
            on_reply=_noop,
            on_event_log=_noop,
            own_role=Role.IMPLEMENTER,
            own_instance_id="implementer-1",
        ),
    )


# --------------------------------------------------------------------------- #
# 1. INV-0
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_isolation_unset_calls_no_check_no_launcher_and_spawns_directly(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate_mod, "check_turn_isolation", _check_that(calls, None))
    adapter = _build(_settings(tmp_path, None))
    assert adapter._turn_launch_factory is None
    await _spawn(adapter)
    assert (calls.check, calls.launcher, calls.direct) == (0, 0, 1)


def test_resolve_turn_launch_off_never_calls_the_check() -> None:
    seen: list[str] = []

    def _spy(user: str, clones: Any) -> bool:
        seen.append(user)
        return True

    assert gate_mod.resolve_turn_launch(None, [Path(".")], check=_spy) is None
    assert gate_mod.resolve_turn_launch("", [Path(".")], check=_spy) is None
    assert seen == []


# --------------------------------------------------------------------------- #
# 2. check fails / times out / raises -> exit 10, one line, no turn started
# --------------------------------------------------------------------------- #


def _assert_stopped_before_any_start(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch, outcome: BaseException
) -> TurnIsolationStop:
    monkeypatch.setattr(gate_mod, "check_turn_isolation", _check_that(calls, outcome))
    with pytest.raises(TurnIsolationStop) as excinfo:
        _build(_settings(tmp_path, "mindwire-turn"))
    assert calls.check == 1
    assert (calls.launcher, calls.direct) == (0, 0)
    return excinfo.value


def test_check_failure_stops_without_starting_either_path(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "turn user cannot use clone X: ACL (Y)"
    stop = _assert_stopped_before_any_start(tmp_path, calls, monkeypatch, TurnIsolationStop(reason))
    assert stop.reason == reason


def test_check_timeout_stops_without_starting_either_path(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = _assert_stopped_before_any_start(
        tmp_path, calls, monkeypatch, subprocess.TimeoutExpired(["launcher", _SECRET], 120.0)
    )
    assert stop.reason == TURN_ISOLATION_CHECK_TIMED_OUT
    assert _SECRET not in stop.stop_line()


def test_check_exception_stops_without_starting_either_path(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = _assert_stopped_before_any_start(
        tmp_path, calls, monkeypatch, RuntimeError(f"boom {_SECRET}")
    )
    assert stop.reason == f"{TURN_ISOLATION_CHECK_FAILED} (RuntimeError)"
    assert _SECRET not in stop.stop_line()


@pytest.mark.parametrize(
    "outcome",
    [
        TurnIsolationStop("turn user credential missing: mindwire/turn-user"),
        subprocess.TimeoutExpired(["x", _SECRET], 1.0),
        OSError(f"io {_SECRET}"),
    ],
    ids=["failure", "timeout", "exception"],
)
def test_main_turns_every_check_outcome_into_exit_10_and_one_stop_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    calls: _Calls,
    outcome: BaseException,
) -> None:
    """The real composition root inside the real ``main``: exit 10, one value-free stop line."""
    monkeypatch.setattr(gate_mod, "check_turn_isolation", _check_that(calls, outcome))
    settings = _settings(tmp_path, "mindwire-turn")

    async def _fake_run_conductor(_settings: MindwireSettings, **_kw: object) -> None:
        _build(settings)

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])

    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == SECRETS_STOP_EXIT_CODE
    captured = capsys.readouterr()
    lines = [ln for ln in (captured.out + captured.err).splitlines() if STOP_LINE_PREFIX in ln]
    assert len(lines) == 1, lines
    assert _SECRET not in captured.out + captured.err
    assert (calls.launcher, calls.direct) == (0, 0)


def test_check_cli_turns_a_probe_timeout_into_exit_10(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """msg-7363 A: ``mindwire turn-isolation-check`` no longer dies with a traceback / exit 1."""

    class _Ops:
        def rev_parse(self, clone: Path) -> Any:
            raise subprocess.TimeoutExpired(["launcher"], 120.0)

        def probe_write(self, directory: Path, pid: int) -> Any:  # pragma: no cover
            raise AssertionError

    class _Secret:
        def reveal_length(self) -> int:
            return 1

        def wipe(self) -> None:
            return None

    class _Api:
        def cred_read(self, _target: str) -> Any:
            return _Secret()

    code = check_mod.run(
        "mindwire-turn", [Path(".")], api_factory=_Api, ops_factory=lambda _u: _Ops()
    )
    assert code == SECRETS_STOP_EXIT_CODE
    err = capsys.readouterr().err.splitlines()
    assert err == [STOP_LINE_PREFIX + TURN_ISOLATION_CHECK_TIMED_OUT]


# --------------------------------------------------------------------------- #
# 3. check passes -> launcher once, direct 0
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_check_pass_routes_the_spawn_through_the_launcher_only(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate_mod, "check_turn_isolation", _check_that(calls, None))
    adapter = _build(_settings(tmp_path, "mindwire-turn"))
    await _spawn(adapter)
    assert (calls.check, calls.launcher, calls.direct) == (1, 1, 0)


@pytest.mark.anyio
async def test_production_route_refuses_by_name_until_the_spike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a test double the route is the pending-spike refusal, never the direct spawn."""
    direct: list[int] = []
    monkeypatch.setattr(implementer_mod, "_default_client_factory", lambda _o: direct.append(1))
    monkeypatch.setattr(gate_mod, "check_turn_isolation", lambda u, c: True)
    adapter = _build(_settings(tmp_path, "mindwire-turn"))
    with pytest.raises(TurnIsolationStop) as excinfo:
        await _spawn(adapter)
    assert excinfo.value.reason == TURN_LAUNCH_ROUTE_PENDING_SPIKE
    assert direct == []


# --------------------------------------------------------------------------- #
# 4. launcher raises -> named stop, no fall-back
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (TurnIsolationStop("turn spawn failed: CreateProcessAsUserW (5)"), None),
        (RuntimeError(f"x {_SECRET}"), f"{TURN_LAUNCH_ROUTE_FAILED} (RuntimeError)"),
    ],
    ids=["stop", "exception"],
)
async def test_launcher_failure_stops_by_name_and_never_falls_back(
    tmp_path: Path,
    calls: _Calls,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    reason: str | None,
) -> None:
    def _failing_launcher(_options: Any) -> Any:
        calls.launcher += 1
        raise raised

    monkeypatch.setattr(gate_mod, "pending_spike_launch_factory", _failing_launcher)
    monkeypatch.setattr(gate_mod, "check_turn_isolation", _check_that(calls, None))
    adapter = _build(_settings(tmp_path, "mindwire-turn"))
    with pytest.raises(TurnIsolationStop) as excinfo:
        await _spawn(adapter)
    expected = reason if reason is not None else raised.reason  # type: ignore[attr-defined]
    assert excinfo.value.reason == expected
    assert _SECRET not in excinfo.value.stop_line()
    assert (calls.launcher, calls.direct) == (1, 0)


# --------------------------------------------------------------------------- #
# 5. UPN
# --------------------------------------------------------------------------- #


def test_upn_is_refused_at_parse_with_the_explicit_reason() -> None:
    with pytest.raises(TurnIsolationStop) as excinfo:
        TurnUser.parse("turn@example.local")
    assert excinfo.value.reason == TURN_USER_UPN_NOT_SUPPORTED
    assert excinfo.value.reason == "turn user must be DOMAIN\\user or user (UPN not supported)"
    assert TurnUser.parse("HOST\\turn") == TurnUser(user="turn", domain="HOST")
    assert TurnUser.parse("turn") == TurnUser(user="turn", domain=".")


def test_upn_stops_before_check_work_and_before_either_start_path(
    tmp_path: Path, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real check parses first: no credential read, no probe, no launcher, no direct spawn."""
    reads: list[str] = []
    probes: list[str] = []

    class _Api:
        def cred_read(self, target: str) -> Any:  # pragma: no cover - must not be reached
            reads.append(target)
            return None

    def _ops(_user: str) -> Any:  # pragma: no cover - must not be reached
        probes.append(_user)
        raise AssertionError

    def _real_check(user: str, clones: Any) -> bool:
        calls.check += 1
        return check_mod.check_turn_isolation(user, clones, api_factory=_Api, ops_factory=_ops)

    monkeypatch.setattr(gate_mod, "check_turn_isolation", _real_check)
    with pytest.raises(TurnIsolationStop) as excinfo:
        _build(_settings(tmp_path, "turn@example.local"))
    assert excinfo.value.reason == TURN_USER_UPN_NOT_SUPPORTED
    assert (reads, probes) == ([], [])
    assert (calls.launcher, calls.direct) == (0, 0)


# --------------------------------------------------------------------------- #
# wrapper pin: the sweep's exit-10 branch reads the same code the daemon exits with
# --------------------------------------------------------------------------- #


def test_stop_exit_code_is_pinned_to_the_sweep_wrapper() -> None:
    import re

    sweep = (
        Path(__file__).resolve().parent.parent / "deploy" / "run-conductor-scheduled.ps1"
    ).read_text(encoding="utf-8")
    m = re.search(r"^\$TurnIsolationStopExitCode\s*=\s*(\d+)\s*$", sweep, re.MULTILINE)
    assert m is not None and int(m.group(1)) == SECRETS_STOP_EXIT_CODE == 10
    # The wrapper parses the same stop-line prefix the daemon prints.
    assert f"'^{STOP_LINE_PREFIX.strip()} (.+)$'" in sweep
