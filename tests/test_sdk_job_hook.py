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
from pathlib import Path
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


# --------------------------------------------------------------------------- #
# PR-gate #299 regression: IsProcessInJob's OpenProcess needs
# PROCESS_QUERY_LIMITED_INFORMATION (0x1000). Omitting the flag raised
# ACCESS_DENIED at IsProcessInJob and crashed spawn on every Windows session
# (verify path) and silently skipped every child (belt path). These tests
# pin the access mask so a future refactor that drops the flag reds here
# instead of in production.
# --------------------------------------------------------------------------- #


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _AccessRightsRecorder:
    """Fake win32api that records the access-rights bitmask passed to OpenProcess.

    Used to verify the caller supplied ``PROCESS_QUERY_LIMITED_INFORMATION``
    before calling ``IsProcessInJob`` — the PR-gate #299 blocker.
    """

    def __init__(self) -> None:
        self.open_access_masks: list[int] = []

    def OpenProcess(  # noqa: N802
        self, access: int, _inherit: bool, _pid: int
    ) -> int:
        self.open_access_masks.append(access)
        return 0xC0DE

    def CloseHandle(self, _handle: int) -> None:  # noqa: N802
        return None


class _FakeJobModuleWithVerify:
    """Fake win32job for is_process_in_job / lookup path."""

    def __init__(self) -> None:
        self.is_in_job_answer = True

    def IsProcessInJob(self, _hproc: int, _job_handle: int) -> bool:  # noqa: N802
        return self.is_in_job_answer

    def AssignProcessToJobObject(  # noqa: N802
        self, _job_handle: int, _hproc: int
    ) -> None:
        return None


def test_is_process_in_job_opens_process_with_query_limited_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-gate #299 blocker: IsProcessInJob's handle needs the query right.

    Without ``PROCESS_QUERY_LIMITED_INFORMATION`` (0x1000) in the
    OpenProcess access mask, ``IsProcessInJob`` raises ACCESS_DENIED at
    runtime. That crashed the spawn verify on every Windows session on the
    first PR-299 revision; this test reds if the flag is ever dropped.
    """
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    class _FakePywintypes:
        class error(Exception):  # noqa: N801, N818
            pass

    rec = _AccessRightsRecorder()
    monkeypatch.setitem(sys.modules, "win32api", rec)
    monkeypatch.setitem(sys.modules, "win32job", _FakeJobModuleWithVerify())
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)

    state = JobState(handle=0xB0B, session_id="s1")
    _sdk_job_hook.is_process_in_job(1234, state)

    assert len(rec.open_access_masks) == 1
    access = rec.open_access_masks[0]
    assert access & _PROCESS_QUERY_LIMITED_INFORMATION, (
        f"OpenProcess called with 0x{access:04X}; must include "
        f"PROCESS_QUERY_LIMITED_INFORMATION (0x{_PROCESS_QUERY_LIMITED_INFORMATION:04X}) "
        f"or IsProcessInJob raises ACCESS_DENIED (PR-gate #299)"
    )


def test_lookup_and_assign_leftover_opens_process_with_query_limited_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-gate #299 blocker: the belt's IsProcessInJob check needs the same flag.

    Without ``PROCESS_QUERY_LIMITED_INFORMATION`` the belt's IsProcessInJob
    raised ACCESS_DENIED and the caller's ``except pywintypes.error`` silently
    swallowed it — the fallback then skipped every child instead of
    Assigning the leftover claude.exe to the Job. That defeated the whole
    v12 §Einstein-round-5 finally-runs-lookup guarantee.
    """
    if not _IS_WINDOWS:
        pytest.skip("Windows-only enumeration path")

    class _FakePywintypes:
        class error(Exception):  # noqa: N801, N818
            pass

    class _FakeChild:
        def __init__(self, pid: int, exe_path: str) -> None:
            self.pid = pid
            self._exe = exe_path

        def exe(self) -> str:
            return self._exe

    class _FakeProc:
        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def children(self) -> list[_FakeChild]:
            return self._children

    class _FakePsutil:
        class NoSuchProcess(Exception):  # noqa: N818
            pass

        class AccessDenied(Exception):  # noqa: N818
            pass

        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def Process(self, _pid: int) -> _FakeProc:  # noqa: N802
            return _FakeProc(self._children)

    import os as _os

    fake_exe = _os.path.abspath("claude.exe")
    fake_child = _FakeChild(pid=9999, exe_path=fake_exe)

    rec = _AccessRightsRecorder()
    fake_job = _FakeJobModuleWithVerify()
    fake_job.is_in_job_answer = False  # force the belt to attempt an Assign
    monkeypatch.setitem(sys.modules, "win32api", rec)
    monkeypatch.setitem(sys.modules, "win32job", fake_job)
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([fake_child]))

    state = JobState(handle=0xB0B, session_id="s1")
    _sdk_job_hook.lookup_and_assign_leftover(state, fake_exe)

    assert len(rec.open_access_masks) == 1
    access = rec.open_access_masks[0]
    assert access & _PROCESS_QUERY_LIMITED_INFORMATION, (
        f"belt OpenProcess called with 0x{access:04X}; must include "
        f"PROCESS_QUERY_LIMITED_INFORMATION (0x{_PROCESS_QUERY_LIMITED_INFORMATION:04X}) "
        f"or IsProcessInJob raises ACCESS_DENIED and every child is skipped "
        f"(PR-gate #299)"
    )


# --------------------------------------------------------------------------- #
# PR-gate #299 round 2 regression: the proxy's ``try/finally: raise`` after
# OpenProcess / Assign failed would swallow the ORIGINAL pywintypes.error
# whenever ``process.terminate()`` itself raised, because Python's ``raise``
# in a ``finally`` re-raises whatever exception is currently active — the
# terminate-time error, not the OpenProcess-time error. The fix uses
# ``contextlib.suppress(Exception)`` around the terminate() call so the
# original error propagates cleanly.
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_proxy_preserves_openprocess_error_when_terminate_also_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-gate #299 round 2: a failing terminate() must not shadow the original."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    class _OriginalOpenProcessError(Exception):
        """The exception that MUST propagate — pytest.raises pins its identity."""

    class _SecondaryTerminateError(Exception):
        """The exception that MUST NOT propagate (would shadow the original)."""

    class _FakePywintypes:
        # Point the real production ``except pywintypes.error:`` at our marker
        # so the OpenProcess failure enters the handler under test.
        error = _OriginalOpenProcessError

    class _AlwaysFailingWin32Api:
        def OpenProcess(self, *_a: Any, **_k: Any) -> int:  # noqa: N802
            raise _OriginalOpenProcessError("cannot open pid — this MUST propagate")

        def CloseHandle(self, *_a: Any, **_k: Any) -> None:  # noqa: N802
            return None

    class _StubWin32Job:
        def AssignProcessToJobObject(self, *_a: Any, **_k: Any) -> None:  # noqa: N802
            return None

    class _FailingProcess:
        pid = 4242

        def terminate(self) -> None:
            raise _SecondaryTerminateError("terminate hiccup — MUST be swallowed")

    class _FakeAnyioSpawningFailing:
        async def open_process(self, *_a: Any, **_k: Any) -> _FailingProcess:
            return _FailingProcess()

    monkeypatch.setitem(sys.modules, "win32api", _AlwaysFailingWin32Api())
    monkeypatch.setitem(sys.modules, "win32job", _StubWin32Job())
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)

    proxy = _JobAwareAnyioProxy(_FakeAnyioSpawningFailing())
    token = _JOB_HANDLE_CTX.set(0xB0B)
    try:
        # ``pytest.raises`` pins the exception identity — the ORIGINAL
        # OpenProcess error must survive, not the terminate() secondary error.
        with pytest.raises(_OriginalOpenProcessError):
            await proxy.open_process("cmd")
    finally:
        _JOB_HANDLE_CTX.reset(token)


@pytest.mark.anyio
async def test_proxy_preserves_assign_error_when_terminate_also_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-gate #299 round 2: same guarantee for the AssignProcessToJobObject branch.

    The Assign path has the same ``try/finally: raise`` shape as the
    OpenProcess path — this pins that path independently, so a partial
    revert of the fix reds here.
    """
    if not _IS_WINDOWS:
        pytest.skip("Windows-only Job Object primitive")

    class _OriginalAssignError(Exception):
        pass

    class _SecondaryTerminateError(Exception):
        pass

    class _FakePywintypes:
        error = _OriginalAssignError

    class _StubWin32Api:
        def OpenProcess(self, *_a: Any, **_k: Any) -> int:  # noqa: N802
            return 0xDEAD

        def CloseHandle(self, *_a: Any, **_k: Any) -> None:  # noqa: N802
            return None

    class _AssignFailingWin32Job:
        def AssignProcessToJobObject(self, *_a: Any, **_k: Any) -> None:  # noqa: N802
            raise _OriginalAssignError("assign failed — this MUST propagate")

    class _FailingProcess:
        pid = 4242

        def terminate(self) -> None:
            raise _SecondaryTerminateError("terminate hiccup — MUST be swallowed")

    class _FakeAnyioSpawning:
        async def open_process(self, *_a: Any, **_k: Any) -> _FailingProcess:
            return _FailingProcess()

    monkeypatch.setitem(sys.modules, "win32api", _StubWin32Api())
    monkeypatch.setitem(sys.modules, "win32job", _AssignFailingWin32Job())
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)

    proxy = _JobAwareAnyioProxy(_FakeAnyioSpawning())
    token = _JOB_HANDLE_CTX.set(0xB0B)
    try:
        with pytest.raises(_OriginalAssignError):
            await proxy.open_process("cmd")
    finally:
        _JOB_HANDLE_CTX.reset(token)


# --------------------------------------------------------------------------- #
# PR-gate #299 round 3 regression: when ``exe_absolute_path`` is a bare name
# like ``"claude"`` (the fallback of ``_default_sdk_executable_path`` when
# the bundled binary is missing AND ``shutil.which`` returns None), the
# belt's matching logic must NOT compare ``os.path.abspath("claude")``
# (which prepends CWD, guaranteeing a mismatch) against the child's real
# absolute path. It must fall back to a case-insensitive basename check.
# The naysayer showed the pre-fix code silently skipped every child under
# this condition, defeating the entire finally-runs-lookup safety net.
# --------------------------------------------------------------------------- #


def test_lookup_and_assign_leftover_matches_child_via_basename_when_target_is_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-gate #299 round 3: bare "claude" target must still match a child."""
    if not _IS_WINDOWS:
        pytest.skip("Windows-only enumeration path")

    class _FakePywintypes:
        class error(Exception):  # noqa: N801, N818
            pass

    class _FakeChild:
        def __init__(self, pid: int, exe_path: str) -> None:
            self.pid = pid
            self._exe = exe_path

        def exe(self) -> str:
            return self._exe

    class _FakeProc:
        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def children(self) -> list[_FakeChild]:
            return self._children

    class _FakePsutil:
        class NoSuchProcess(Exception):  # noqa: N818
            pass

        class AccessDenied(Exception):  # noqa: N818
            pass

        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def Process(self, _pid: int) -> _FakeProc:  # noqa: N802
            return _FakeProc(self._children)

    # The child's REAL absolute path (a location the daemon's CWD will never
    # match: PATH-installed Python scripts directory).
    real_child_exe = r"C:\Python311\Scripts\claude.exe"
    fake_child = _FakeChild(pid=9999, exe_path=real_child_exe)

    rec = _AccessRightsRecorder()
    fake_job = _FakeJobModuleWithVerify()
    fake_job.is_in_job_answer = False  # force the Assign path
    monkeypatch.setitem(sys.modules, "win32api", rec)
    monkeypatch.setitem(sys.modules, "win32job", fake_job)
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([fake_child]))

    state = JobState(handle=0xB0B, session_id="s1")
    # Pass the bare-name fallback — the exact string
    # ``_default_sdk_executable_path`` returns when nothing resolves.
    _sdk_job_hook.lookup_and_assign_leftover(state, "claude")

    # Pre-fix behaviour: 0 OpenProcess calls (silent skip because
    # abspath("claude") != C:\Python311\Scripts\claude.exe).
    # Post-fix behaviour: exactly 1 OpenProcess call (basename match hit).
    assert len(rec.open_access_masks) == 1, (
        "belt matched 0 children — bare-name target ('claude') failed to hit "
        r"the child C:\Python311\Scripts\claude.exe via basename fallback "
        "(PR-gate #299 round 3)"
    )


def test_lookup_and_assign_leftover_matches_claude_exe_when_target_bare_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``"claude"`` target must also match a ``claude.exe`` child.

    Windows treats ``claude`` and ``claude.exe`` as the same command; the
    belt normalizes the target basename by appending ``.exe`` if absent so
    the two forms compare equal.
    """
    if not _IS_WINDOWS:
        pytest.skip("Windows-only enumeration path")

    class _FakePywintypes:
        class error(Exception):  # noqa: N801, N818
            pass

    class _FakeChild:
        def __init__(self, pid: int, exe_path: str) -> None:
            self.pid = pid
            self._exe = exe_path

        def exe(self) -> str:
            return self._exe

    class _FakeProc:
        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def children(self) -> list[_FakeChild]:
            return self._children

    class _FakePsutil:
        class NoSuchProcess(Exception):  # noqa: N818
            pass

        class AccessDenied(Exception):  # noqa: N818
            pass

        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def Process(self, _pid: int) -> _FakeProc:  # noqa: N802
            return _FakeProc(self._children)

    # Mix: one matching CLAUDE.EXE (case-insensitive), one unrelated child.
    matching = _FakeChild(pid=1000, exe_path=r"C:\opt\node\CLAUDE.EXE")
    unrelated = _FakeChild(pid=1001, exe_path=r"C:\Windows\System32\cmd.exe")

    rec = _AccessRightsRecorder()
    fake_job = _FakeJobModuleWithVerify()
    fake_job.is_in_job_answer = False
    monkeypatch.setitem(sys.modules, "win32api", rec)
    monkeypatch.setitem(sys.modules, "win32job", fake_job)
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([matching, unrelated]))

    state = JobState(handle=0xB0B, session_id="s1")
    _sdk_job_hook.lookup_and_assign_leftover(state, "claude")

    # Only the matching child should have been OpenProcess'd.
    assert len(rec.open_access_masks) == 1, (
        "belt matched wrong number of children — bare 'claude' should match "
        "CLAUDE.EXE (case-insensitive) but skip cmd.exe"
    )


def test_lookup_and_assign_leftover_absolute_target_still_uses_full_path_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sanity: when the target IS a real absolute path, full-path matching
    still applies — the basename fallback must NOT loosen behaviour for
    real absolute targets. A child with the same basename but a different
    absolute path is correctly skipped.
    """
    if not _IS_WINDOWS:
        pytest.skip("Windows-only enumeration path")

    class _FakePywintypes:
        class error(Exception):  # noqa: N801, N818
            pass

    class _FakeChild:
        def __init__(self, pid: int, exe_path: str) -> None:
            self.pid = pid
            self._exe = exe_path

        def exe(self) -> str:
            return self._exe

    class _FakeProc:
        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def children(self) -> list[_FakeChild]:
            return self._children

    class _FakePsutil:
        class NoSuchProcess(Exception):  # noqa: N818
            pass

        class AccessDenied(Exception):  # noqa: N818
            pass

        def __init__(self, children: list[_FakeChild]) -> None:
            self._children = children

        def Process(self, _pid: int) -> _FakeProc:  # noqa: N802
            return _FakeProc(self._children)

    # A real, existing absolute path — we use tmp_path to guarantee it
    # exists at test time (so ``os.path.exists`` returns True and the belt
    # takes the STRICT full-path branch).
    real_target = tmp_path / "claude.exe"
    real_target.write_bytes(b"")

    # A different absolute path with the SAME basename — must be skipped.
    other_claude = _FakeChild(pid=1000, exe_path=r"C:\Somewhere\Else\claude.exe")

    rec = _AccessRightsRecorder()
    fake_job = _FakeJobModuleWithVerify()
    fake_job.is_in_job_answer = False
    monkeypatch.setitem(sys.modules, "win32api", rec)
    monkeypatch.setitem(sys.modules, "win32job", fake_job)
    monkeypatch.setitem(sys.modules, "pywintypes", _FakePywintypes)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([other_claude]))

    state = JobState(handle=0xB0B, session_id="s1")
    _sdk_job_hook.lookup_and_assign_leftover(state, str(real_target))

    # 0 matches — the real absolute target does NOT match the differently
    # located claude.exe child. This is the correct behaviour: strict
    # matching when we have a real path, loose matching only when we don't.
    assert len(rec.open_access_masks) == 0, (
        "belt loosened matching for a real absolute target — the basename "
        "fallback must ONLY apply when the target is not a real absolute path"
    )
