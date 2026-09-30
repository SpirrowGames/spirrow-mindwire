"""Adapter-error stop line (T-successful-turn-quarantined-on-sdk-lifecycle-failure).

Bohr msg-4440 D-1'' / D-2' (endorsed by Einstein): when an adapter's ``deliver_event`` raises,
the conductor writes a :class:`ConductorStopSnapshot` into a :class:`ConductorStopSlot` it was
handed and re-raises the exception UNCHANGED; ``loop_runner.main`` is the single place that prints
``conductor stopped: reason=adapter_error … error_code=<code>``. These tests pin D-4:

* conductor: same exception object re-raised, snapshot fields right, no ``conductor stopped:``
  log from the conductor, ``__slots__`` exceptions and a raising ``.code`` property still yield a
  non-empty ``error_code``;
* loop_runner: exactly one non-empty stop line, SDK marker stays last, no snapshot = unchanged,
  ``EnvironmentTerminalError`` stays exit=2 with no stop line;
* the two adapter raise sites carry ``.code``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire import loop_runner
from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkDeliveryError
from spirrow_mindwire.adapters.implementer import (
    ImplementerSdkDeliveryError,
    ImplementerSdkTurnTimeoutError,
)
from spirrow_mindwire.adapters.naysayer_lexora import NaysayerLexoraDeliveryError
from spirrow_mindwire.adapters.naysayer_sdk import (
    NaysayerSdkDeliveryError,
    NaysayerSdkShutdownError,
)
from spirrow_mindwire.conductor.core import (
    Conductor,
    ConductorStopSlot,
    ConductorStopSnapshot,
    StopReason,
    adapter_error_code,
)
from spirrow_mindwire.config import ConductorConfig, MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.exceptions import AdapterDeliveryError
from spirrow_mindwire.value_objects import ChatroomEvent, Role, SessionHandle

# --------------------------------------------------------------------------- #
# Conductor side (D-1'')
# --------------------------------------------------------------------------- #


class _RaisingDispatcher(_ScriptedDispatcher):
    """A scripted dispatcher whose dispatch to ``raise_on`` raises ``exc``."""

    def __init__(
        self,
        mcp: _FakeChatroomMcp,
        replies: dict[Role, list[str]],
        *,
        raise_on: Role,
        exc: BaseException,
    ) -> None:
        super().__init__(mcp, replies)
        self._raise_on = raise_on
        self._exc = exc

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        if handle.role is self._raise_on:
            self.dispatches.append((handle.role, event.payload.msg_id))
            raise self._exc
        await super().dispatch(handle, event)


def _conductor(mcp: _FakeChatroomMcp, dispatcher: Any, slot: ConductorStopSlot | None) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        stop_slot=slot,
    )


async def _run_until_raise(exc: BaseException) -> tuple[BaseException, ConductorStopSlot]:
    """Bohr → Einstein (posts, round 0 completes) → Bohr's dispatch raises (round 1)."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design proposal\n\nNEXT: Einstein")
    disp = _RaisingDispatcher(
        mcp,
        {Role.NAYSAYER: ["critique\n\nNEXT: Bohr"]},
        raise_on=Role.PROPOSER,
        exc=exc,
    )
    slot = ConductorStopSlot()
    with pytest.raises(BaseException) as excinfo:
        await _conductor(mcp, disp, slot).run()
    return excinfo.value, slot


@pytest.mark.anyio
async def test_dispatch_error_fills_slot_and_reraises_the_same_object(
    caplog: pytest.LogCaptureFixture,
) -> None:
    boom = ImplementerSdkTurnTimeoutError("deliver_event turn timeout for session S")
    with caplog.at_level(logging.DEBUG):
        raised, slot = await _run_until_raise(boom)

    assert raised is boom  # identity, not a wrapper
    assert raised.__cause__ is None and type(raised) is ImplementerSdkTurnTimeoutError
    assert slot.snapshot == ConductorStopSnapshot(
        rounds=1,  # Einstein's round completed; Bohr's (round_index 1) raised
        last_msg_id="m2",  # the head the raising dispatch was delivering (Einstein's post)
        forced=0,
        forced_saveable=0,
        error_code="adapter.turn_timeout",
    )
    # D-1'': the conductor prints nothing for this case — loop_runner.main owns the only line.
    assert not [r for r in caplog.records if "conductor stopped" in r.getMessage()]


class _AttrRefusingError(Exception):
    """An exception that refuses attribute assignment — the msg-4439 counter-example.

    A pure-Python ``Exception`` subclass with ``__slots__ = ()`` does NOT refuse on CPython
    (``BaseException`` instances always carry ``__dict__``), so the refusal is modelled with a
    ``__setattr__`` override — the shape a C-extension exception would present.
    """

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} refuses attribute {name!r}")


def test_attr_refusing_exception_really_refuses() -> None:
    # Pins the premise, so the next test is not vacuous.
    with pytest.raises(AttributeError):
        _AttrRefusingError("x").mindwire_stop = 1


@pytest.mark.anyio
async def test_attr_refusing_exception_still_yields_a_snapshot_with_class_name() -> None:
    boom = _AttrRefusingError("no attributes for you")
    raised, slot = await _run_until_raise(boom)
    assert raised is boom
    assert slot.snapshot is not None
    assert slot.snapshot.error_code == "_AttrRefusingError"


class _RaisingCodePropertyError(Exception):
    @property
    def code(self) -> str:
        raise RuntimeError("property blew up")


@pytest.mark.anyio
async def test_raising_code_property_falls_back_to_class_name() -> None:
    raised, slot = await _run_until_raise(_RaisingCodePropertyError("x"))
    assert isinstance(raised, _RaisingCodePropertyError)
    assert slot.snapshot is not None
    assert slot.snapshot.error_code == "_RaisingCodePropertyError"


@pytest.mark.anyio
async def test_no_slot_means_plain_reraise() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design proposal\n\nNEXT: Einstein")
    boom = RuntimeError("x")
    disp = _RaisingDispatcher(mcp, {}, raise_on=Role.NAYSAYER, exc=boom)
    with pytest.raises(RuntimeError) as excinfo:
        await _conductor(mcp, disp, None).run()
    assert excinfo.value is boom


class _WithCodeError(Exception):
    def __init__(self, code: object) -> None:
        super().__init__("x")
        self.code = code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_WithCodeError("adapter.shutdown_failed"), "adapter.shutdown_failed"),
        (_WithCodeError(""), "_WithCodeError"),  # empty → class name, never empty
        (_WithCodeError("has space"), "_WithCodeError"),  # would be truncated by the \S+ parser
        (_WithCodeError(404), "_WithCodeError"),  # not an adapter code
        (RuntimeError("plain"), "RuntimeError"),
    ],
)
def test_adapter_error_code(exc: BaseException, expected: str) -> None:
    assert adapter_error_code(exc) == expected


def test_adapter_raise_sites_carry_code() -> None:
    assert ImplementerSdkTurnTimeoutError("x").code == "adapter.turn_timeout"
    # The naysayer shutdown site is pinned end-to-end in test_naysayer_sdk_adapter.py.


def test_delivery_error_base_declares_code_as_none() -> None:
    # The contract lives on the base class (human msg-4910), and its default is None rather than
    # a generic string (Einstein msg-5001).
    # The declaration itself is not re-checked here: ``mypy src tests`` in the gate rejects the
    # typed reads below if ``code`` is undeclared anywhere in the MRO, and at runtime they raise
    # AttributeError. No ``vars()``/type-hint introspection, so moving the declaration to an
    # intermediate base or mixin stays green (PR-gate advisories on #389).
    assert AdapterDeliveryError.code is None
    assert AdapterDeliveryError("x").code is None


@pytest.mark.parametrize(
    "cls",
    [
        AdapterDeliveryError,
        ClaudeCodeSdkDeliveryError,
        ImplementerSdkDeliveryError,
        NaysayerLexoraDeliveryError,
        NaysayerSdkDeliveryError,
    ],
)
def test_delivery_error_without_a_code_keeps_its_class_name(
    cls: type[AdapterDeliveryError],
) -> None:
    # The None default must not cost the stop line the adapter's identity: a delivery error that
    # names no code is still reported by its concrete class (msg-5001).
    exc = cls("x")
    assert exc.code is None
    assert adapter_error_code(exc) == cls.__name__


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (ImplementerSdkTurnTimeoutError, "adapter.turn_timeout"),
        (NaysayerSdkShutdownError, "adapter.shutdown_failed"),
    ],
)
def test_delivery_error_subclass_override_wins(
    cls: type[AdapterDeliveryError], expected: str
) -> None:
    assert cls.code == expected
    assert adapter_error_code(cls("x")) == expected


# --------------------------------------------------------------------------- #
# loop_runner side (D-2')
# --------------------------------------------------------------------------- #

_SNAPSHOT = ConductorStopSnapshot(
    rounds=3,
    last_msg_id="msg-4413",
    forced=1,
    forced_saveable=0,
    error_code="adapter.shutdown_failed",
)


def _patch_main(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    async def _fake_run_conductor(
        _settings: MindwireSettings,
        *,
        stop_slot: ConductorStopSlot | None = None,
        # T42: main() now also forwards the sweep's stall-watchdog input (defaults never stall).
        launches_same_head: int = 0,
        launch_head_msg_id: str | None = None,
    ) -> None:
        assert stop_slot is not None, "main must hand the conductor a stop slot"
        await body(stop_slot)

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])


def _stop_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "conductor stopped:" in line]


def test_main_prints_exactly_one_adapter_error_stop_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def body(slot: ConductorStopSlot) -> None:
        slot.snapshot = _SNAPSHOT
        raise RuntimeError("per-turn client shutdown failed")

    _patch_main(monkeypatch, body)
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == 1  # the wrapper's quarantine branch is unchanged

    out = capsys.readouterr().out
    lines = _stop_lines(out)
    assert lines == [
        "conductor stopped: reason=adapter_error rounds=3 forced_naysayer=1 "
        "forced_naysayer_saveable=0 last_msg=msg-4413 error_code=adapter.shutdown_failed"
    ]
    assert StopReason.ADAPTER_ERROR.value == "adapter_error"
    # Traceback preserved, and above the stop line.
    nonempty = [line for line in out.splitlines() if line.strip()]
    assert "Traceback" in out
    assert nonempty[-1] == lines[0]


def test_main_keeps_the_sdk_marker_last_when_both_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from spirrow_mindwire.adapters._sdk_result import SdkIsErrorSignal

    async def body(slot: ConductorStopSlot) -> None:
        slot.snapshot = _SNAPSHOT
        try:
            raise SdkIsErrorSignal(
                {"reason_source": "result", "message": "429", "captured_fields": {}}
            )
        except SdkIsErrorSignal as sig:
            raise RuntimeError("wrapped") from sig

    _patch_main(monkeypatch, body)
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == 1

    out = capsys.readouterr().out
    nonempty = [line for line in out.splitlines() if line.strip()]
    assert nonempty[-1].startswith("sdk_error_detail=")  # PR #181 contract
    stop = _stop_lines(out)
    assert len(stop) == 1
    assert nonempty.index(stop[0]) < len(nonempty) - 1


def test_main_without_snapshot_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def body(_slot: ConductorStopSlot) -> None:
        raise RuntimeError("outside the dispatch")

    _patch_main(monkeypatch, body)
    with pytest.raises(RuntimeError, match="outside the dispatch"):
        loop_runner.main()
    assert _stop_lines(capsys.readouterr().out) == []


def test_main_environment_terminal_stays_exit_two_without_stop_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from spirrow_mindwire.github.client import EnvironmentTerminalError, Scope
    from spirrow_mindwire.github.client import PrRef as ClientPrRef

    async def body(slot: ConductorStopSlot) -> None:
        # What the conductor does when the dispatch raises it: fill the slot, re-raise unchanged.
        slot.snapshot = _SNAPSHOT
        raise EnvironmentTerminalError(
            pr=ClientPrRef("spirrowgames", "spirrow-mindwire", 1),
            scope=Scope.ENVIRONMENT_CREDENTIAL,
            status_code=401,
            message="Bad credentials",
        )

    _patch_main(monkeypatch, body)
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == 2
    assert _stop_lines(capsys.readouterr().out) == []


@pytest.mark.anyio
async def test_environment_terminal_through_a_real_conductor_is_not_wrapped() -> None:
    # The type-routing premise of msg-4440: the conductor must not change the exception's type,
    # or ``except EnvironmentTerminalError`` in main would miss it and exit=2 would become exit=1.
    from spirrow_mindwire.github.client import EnvironmentTerminalError, Scope
    from spirrow_mindwire.github.client import PrRef as ClientPrRef

    boom = EnvironmentTerminalError(
        pr=ClientPrRef("o", "r", 1),
        scope=Scope.ENVIRONMENT_CREDENTIAL,
        status_code=401,
        message="x",
    )
    raised, slot = await _run_until_raise(boom)
    assert raised is boom
    assert isinstance(raised, EnvironmentTerminalError)
    assert slot.snapshot is not None


class _ReadableThreadMcp:
    """Answers only the T44 launch-resolution read: the thread exists."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "chatroom_get_thread", name
        return {"messages": []}


def _resolvable_settings(tmp_path: Path) -> MindwireSettings:
    # T44: run_conductor resolves project / thread / repo_dir before building anything, so the
    # settings must name a thread and an existing repo_dir to reach build_conductor at all.
    return MindwireSettings(
        loop=Stage3LoopConfig(repo_dir=tmp_path),
        conductor=ConductorConfig(task_thread_id="T-slot"),
    )


def test_run_conductor_threads_the_slot_into_the_conductor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from spirrow_mindwire.conductor.core import ConductorOutcome

    seen: list[object] = []

    def _fake_build(_settings: MindwireSettings, **kw: object) -> Any:
        seen.append(kw.get("stop_slot"))

        class _C:
            async def run(self) -> ConductorOutcome:
                return ConductorOutcome(
                    rounds=0,
                    stop_reason=StopReason.SETTLED,
                    last_msg_id=None,
                    forced_naysayer_turns=0,
                )

            async def aclose(self) -> None:
                return None

        return _C()

    import asyncio

    monkeypatch.setattr(loop_runner, "build_conductor", _fake_build)
    monkeypatch.setattr(loop_runner, "_preflight", lambda _cfg: None)
    slot = ConductorStopSlot()
    asyncio.run(
        loop_runner.run_conductor(
            _resolvable_settings(tmp_path), stop_slot=slot, mcp=_ReadableThreadMcp()
        )
    )
    assert seen == [slot]
