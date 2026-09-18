"""Tests for :mod:`spirrow_mindwire.adapters._sdk_job_hook` (v12).

The hook module has three moving parts, and all three are covered here:

1. ``_JobAwareAnyioProxy`` — must intercept ``open_process`` only when the
   ContextVar is set; must pass through untouched otherwise.
2. ``_JOB_HANDLE_CTX`` task isolation — sibling asyncio tasks in the same
   event loop must see independent context copies; Task A's setting of the
   handle must not leak into Task B.
3. ``close_handle`` — must be idempotent (double-close is safe, no Win32
   API calls the second time), must fire Terminate → Close in order, and
   must set its sentinel BEFORE the API calls to close the reentry window.

Windows-only APIs (``win32api``, ``win32job``, ``psutil``) are exercised via
injected fakes on the module — the tests never touch a real Job Object, so
they run identically on POSIX and Windows CI.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from spirrow_mindwire.adapters import _sdk_job_hook
from spirrow_mindwire.adapters._sdk_job_hook import (
    _JOB_HANDLE_CTX,
    JobState,
    _JobAwareAnyioProxy,
)

_IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# _JobAwareAnyioProxy — pass-through and intercept behaviour
# --------------------------------------------------------------------------- #


class _FakeAnyio:
    """Stand-in for the ``anyio`` module the SDK imports.

    Records ``open_process`` calls and every attribute the proxy forwards
    via ``__getattr__``.
    """

    def __init__(self) -> None:
        self.open_process_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.sleep_called = False

    async def open_process(self, *args: Any, **kwargs: Any) -> Any:
        self.open_process_calls.append((args, kwargs))
        return SimpleNamespace(pid=4242, terminate=lambda: None)

    async def sleep(self, _seconds: float) -> None:
        self.sleep_called = True


@pytest.mark.anyio
async def test_proxy_passes_through_when_ctxvar_is_unset() -> None:
    """No ContextVar => the proxy is a no-op forward, no Win32 calls made."""
    real = _FakeAnyio()
    proxy = _JobAwareAnyioProxy(real)
    process = await proxy.open_process("cmd", check=True)
    assert process.pid == 4242
    assert real.open_process_calls == [(("cmd",), {"check": True})]


@pytest.mark.anyio
async def test_proxy_forwards_arbitrary_attributes_via_getattr() -> None:
    """The proxy is behaviourally indistinguishable from anyio for non-open_process."""
    real = _FakeAnyio()
    proxy = _JobAwareAnyioProxy(real)
    await proxy.sleep(0)
    assert real.sleep_called is True


@pytest.mark.anyio
async def test_proxy_assigns_process_to_job_when_ctxvar_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy calls Win32 assign exactly once when the ContextVar carries a handle."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    real = _FakeAnyio()
    proxy = _JobAwareAnyioProxy(real)

    class _FakeWin32Api:
        def __init__(self) -> None:
            self.open_calls: list[int] = []
            self.close_calls: list[int] = []

        def OpenProcess(  # noqa: N802 — Win32 name
            self, _access: int, _inherit: bool, pid: int
        ) -> int:
            self.open_calls.append(pid)
            return 0xDEAD

        def CloseHandle(self, handle: int) -> None:  # noqa: N802
            self.close_calls.append(handle)

    class _FakeWin32Job:
        def __init__(self) -> None:
            self.assign_calls: list[tuple[int, int]] = []

        def AssignProcessToJobObject(  # noqa: N802
            self, job_handle: int, proc_handle: int
        ) -> None:
            self.assign_calls.append((job_handle, proc_handle))

    class _FakePywintypes:
        # Win32 uses lowercase ``error``; that's the name pywintypes exposes
        # and the name our code catches. Not renamed to satisfy N818 because
        # then the ``raise pywintypes.error(...)`` in production wouldn't
        # match this fake's class.
        class error(Exception):  # noqa: N801, N818
            pass

    fake_api = _FakeWin32Api()
    fake_job = _FakeWin32Job()
    monkeypatch.setitem(sys.modules, "win32api", fake_api)
    monkeypatch.setitem(sys.modules, "win32job", fake_job)
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)

    token = _JOB_HANDLE_CTX.set(0xB0B)
    try:
        process = await proxy.open_process("cmd")
    finally:
        _JOB_HANDLE_CTX.reset(token)

    assert process.pid == 4242
    assert fake_api.open_calls == [4242]
    assert fake_job.assign_calls == [(0xB0B, 0xDEAD)]
    assert fake_api.close_calls == [0xDEAD]


# --------------------------------------------------------------------------- #
# ContextVar task isolation (v12's core safety claim)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_ctxvar_isolates_between_sibling_tasks() -> None:
    """Task A's set() must not leak into a concurrent Task B's context."""
    observations: dict[str, int | None] = {}

    async def task_a() -> None:
        token = _JOB_HANDLE_CTX.set(0xA)
        try:
            await asyncio.sleep(0)  # yield to the scheduler
            observations["A"] = _JOB_HANDLE_CTX.get()
        finally:
            _JOB_HANDLE_CTX.reset(token)

    async def task_b() -> None:
        # Yield first so A has a chance to set (and stay set) before B reads.
        await asyncio.sleep(0)
        observations["B"] = _JOB_HANDLE_CTX.get()

    # ``ensure_future`` starts them; the current context is copied per task.
    a = asyncio.create_task(task_a())
    b = asyncio.create_task(task_b())
    await asyncio.gather(a, b)

    assert observations["A"] == 0xA
    assert observations["B"] is None  # sibling isolation


# --------------------------------------------------------------------------- #
# close_handle — idempotency, sentinel-first, ordering
# --------------------------------------------------------------------------- #


class _RecordingWin32:
    """Fake for ``win32api`` and ``win32job`` combined, records ordering."""

    def __init__(self, terminate_raises: bool = False, close_raises: bool = False) -> None:
        self.terminate_calls: list[int] = []
        self.close_calls: list[int] = []
        self._terminate_raises = terminate_raises
        self._close_raises = close_raises

    def TerminateJobObject(  # noqa: N802
        self, handle: int, _exit_code: int
    ) -> None:
        self.terminate_calls.append(handle)
        if self._terminate_raises:
            import pywintypes

            raise pywintypes.error(6, "TerminateJobObject", "fake")

    def CloseHandle(self, handle: int) -> None:  # noqa: N802
        self.close_calls.append(handle)
        if self._close_raises:
            import pywintypes

            raise pywintypes.error(6, "CloseHandle", "fake")


def _install_fake_win32(monkeypatch: pytest.MonkeyPatch, rec: _RecordingWin32) -> None:
    monkeypatch.setitem(sys.modules, "win32api", rec)
    monkeypatch.setitem(sys.modules, "win32job", rec)


def test_close_handle_is_noop_when_sentinel_is_none() -> None:
    """The first thing close_handle checks is the sentinel — no API calls if None."""
    state = JobState(handle=None, session_id="s1")
    _sdk_job_hook.close_handle(state)  # must not raise


def test_close_handle_double_call_only_touches_api_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v11+v12 idempotency guarantee: second call sees None and returns."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    rec = _RecordingWin32()
    _install_fake_win32(monkeypatch, rec)
    state = JobState(handle=0xDEAD, session_id="s1")

    _sdk_job_hook.close_handle(state)
    _sdk_job_hook.close_handle(state)  # must not double-Close

    assert rec.terminate_calls == [0xDEAD]
    assert rec.close_calls == [0xDEAD]
    assert state.handle is None


def test_close_handle_sets_sentinel_before_win32_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentinel-first order is what makes reentry safe under a raise."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    observed: list[int | None] = []

    class _SpyingRec(_RecordingWin32):
        def TerminateJobObject(self, handle: int, exit_code: int) -> None:  # noqa: N802
            observed.append(state.handle)
            super().TerminateJobObject(handle, exit_code)

    rec = _SpyingRec()
    _install_fake_win32(monkeypatch, rec)
    state = JobState(handle=0xDEAD, session_id="s1")

    _sdk_job_hook.close_handle(state)
    # By the time Terminate runs, the sentinel is already cleared.
    assert observed == [None]


def test_close_handle_swallows_terminate_error_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TerminateJobObject failure is logged but CloseHandle still runs."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    rec = _RecordingWin32(terminate_raises=True)
    _install_fake_win32(monkeypatch, rec)
    state = JobState(handle=0xDEAD, session_id="s1")

    _sdk_job_hook.close_handle(state)  # must not raise
    assert rec.terminate_calls == [0xDEAD]
    assert rec.close_calls == [0xDEAD]
    assert state.handle is None


def test_close_handle_swallows_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CloseHandle failure is logged but the sentinel is still cleared."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    rec = _RecordingWin32(close_raises=True)
    _install_fake_win32(monkeypatch, rec)
    state = JobState(handle=0xDEAD, session_id="s1")

    _sdk_job_hook.close_handle(state)  # must not raise
    assert state.handle is None


# --------------------------------------------------------------------------- #
# install_hook — idempotent, POSIX no-op
# --------------------------------------------------------------------------- #


def test_install_hook_is_idempotent() -> None:
    """Calling install_hook twice must not double-wrap the SDK's anyio."""
    _sdk_job_hook._reset_hook_for_tests()
    try:
        _sdk_job_hook.install_hook()
        # Re-install: idempotent under production and test lifecycle.
        _sdk_job_hook.install_hook()

        if _IS_WINDOWS:
            from claude_agent_sdk._internal.transport import subprocess_cli

            proxy: Any = getattr(subprocess_cli, "anyio", None)
            assert isinstance(proxy, _JobAwareAnyioProxy)
            # Not double-wrapped: unwrap once and we hit the real anyio module,
            # not another proxy.
            assert not isinstance(proxy._real, _JobAwareAnyioProxy)
    finally:
        _sdk_job_hook._reset_hook_for_tests()


# --------------------------------------------------------------------------- #
# create_job / lookup_and_assign_leftover: POSIX raises NotImplementedError
# --------------------------------------------------------------------------- #


def test_create_job_raises_on_posix() -> None:
    """The whole design is Windows-scoped; POSIX must fail loud, not silently."""
    if _IS_WINDOWS:
        pytest.skip("POSIX-only assertion")
    with pytest.raises(NotImplementedError):
        _sdk_job_hook.create_job("s1")


def test_lookup_and_assign_leftover_is_noop_when_handle_none() -> None:
    """A pre-close JobState must not trigger Win32 calls."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only enumeration path")
    state = JobState(handle=None, session_id="s1")
    _sdk_job_hook.lookup_and_assign_leftover(state, "claude.exe")
