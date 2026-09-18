"""Windows Job Object hook for the Claude Agent SDK spawn path (T-auto-backgrounded-hang, v12).

Background — the failure this exists to prevent
-----------------------------------------------
On 2026-09-18 the conductor stalled for 4.08 h because the implementer session
ran a foreground ``gh api ... | ...`` shell that the Claude CLI silently moved
to a background task after its 120 s timeout. The CLI held the SDK turn open
(no ``ResultMessage`` while the background task was alive) so
``receive_response()`` drained forever; the Task Scheduler's 4 h wall killed
the daemon wrapper but the ``claude.exe`` subtree survived, orphaned, with a
child ``bash`` polling ``gh api`` every three seconds. Full evidence and
receipts live in the design thread ``T-auto-backgrounded-command-hangs-\
conductor-4h`` (msg-3532; proposal v12).

What this module gives the adapter
-----------------------------------
A Windows-only, one-time install that lets the implementer's ``spawn`` bind
every ``claude.exe`` (and every descendant it spawns, transitively) into a Job
Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. When the Job's last
handle is closed — normally via ``halt`` or a ``finally`` cleanup in ``spawn``
— the OS terminates the whole tree atomically. That is the invariant the
outer 4 h wall could never provide: an OS-level reaper that fires without
Python's cooperation.

Three separate mechanisms hold the invariant together and each is
load-bearing (v11+v12 exchange):

1. **contextvars task isolation** — ``_JOB_HANDLE_CTX`` is a ``ContextVar``.
   Only the coroutine that ``set()`` it sees a non-``None`` handle; every
   other asyncio task in the same event loop inherits the ``None`` default
   (``asyncio.create_task`` copies the current context). A telemetry task
   that calls ``anyio.open_process`` alongside our spawn is guaranteed to
   see ``None`` in its own context copy and pass through unchanged. This is
   why we can install the proxy globally without collateral damage.

2. **Namespace-scoped proxy** — we replace ``subprocess_cli.anyio`` (the
   module attribute the SDK's transport reads from) with a thin proxy that
   only intercepts ``open_process``. ``sys.modules['anyio']`` stays intact:
   anything that imports anyio directly runs on the real module. The proxy
   forwards every other attribute (``sleep``, ``move_on_after`` …) to the
   real module via ``__getattr__``.

3. **Idempotent close** — ``_close_job_handle`` sets its ``job_handle``
   sentinel to ``None`` *before* it calls ``TerminateJobObject`` /
   ``CloseHandle``. A second call sees ``None`` and returns. Windows
   recycles handle values immediately after close, so a double-close can
   destroy an unrelated resource; the sentinel makes that class of bug
   structurally impossible even if two cleanup paths race.

POSIX
-----
The Job Object primitive does not exist. ``create_job`` and ``close_handle``
raise ``NotImplementedError`` on POSIX; ``install_hook`` is a no-op so the
module can be imported anywhere without breaking. The design intent (see
proposal v12) is that only the Windows daemon uses this path; a POSIX host
never spawns the implementer role in the current deployment.

Naming
------
Everything in this module is prefixed with an underscore because it is
adapter-internal. The only symbol the adapter itself calls is
``install_hook``; the rest is orchestrated via the ``_JOB_HANDLE_CTX``
ContextVar and the per-session helper functions.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The one ContextVar that gates the proxy. Set to a job handle by the
# implementer adapter's spawn coroutine, reset in a ``finally`` before the
# coroutine returns / raises. Any other coroutine sees the default (``None``)
# because asyncio task creation copies the current context — see the module
# docstring for why this makes the proxy safe to install globally.
_JOB_HANDLE_CTX: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_mindwire_sdk_job_handle", default=None
)

# Guards against double-installing the proxy. ``install_hook`` is called at
# daemon startup and MUST be idempotent — reimporting a module never
# re-executes its side effects, but startup code paths change and we would
# rather this be safe than debug a rare double-wrap in production.
_HOOK_INSTALLED: bool = False

_IS_WINDOWS: bool = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# Win32 constants (only referenced on Windows; kept as module-level names so
# tests can spy on them and code paths do not carry the ``if _IS_WINDOWS``
# branch inside every function).
# --------------------------------------------------------------------------- #
# PROCESS_TERMINATE (0x0001) — allow the Job to terminate the process.
# PROCESS_SET_QUOTA (0x0100) — required by AssignProcessToJobObject.
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) — the whole point of the Job:
#   when the last handle closes, the OS kills every process in the Job.
# JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x0800) — set to 0 so no child can escape
#   the Job via ``CREATE_BREAKAWAY_FROM_JOB``. The 0 bit is enforced in
#   ``BasicLimitInformation`` (see ``create_job``).
# JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (0x1000) — same, for silent breakaway.
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# BREAKAWAY_OK and SILENT_BREAKAWAY_OK are opt-in flags: leaving them cleared
# in ExtendedLimitInformation is what enforces "child cannot escape the Job".
# The design (v12) names them explicitly to document the intent.


# --------------------------------------------------------------------------- #
# Session-scoped state
# --------------------------------------------------------------------------- #


@dataclass
class JobState:
    """Per-session Windows Job state, held by the adapter's ``_Session``.

    ``handle`` is a Win32 HANDLE (an integer). ``None`` means "closed" and is
    the sentinel that makes ``close_handle`` idempotent — see
    ``close_handle``'s docstring.

    ``session_id`` is carried purely for diagnostic log lines; the OS does
    not know about it.
    """

    handle: int | None
    session_id: str


# --------------------------------------------------------------------------- #
# Job lifecycle — thin wrappers around pywin32 so tests can substitute them.
# --------------------------------------------------------------------------- #


def create_job(session_id: str) -> JobState:
    """Create an unnamed Windows Job Object and configure it for the adapter.

    Returns a :class:`JobState`. The Job is configured with:

    * ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — atomic terminate when the last
      handle closes (this is what the whole design turns on).
    * ``BREAKAWAY_OK`` and ``SILENT_BREAKAWAY_OK`` **cleared** — children
      cannot escape.

    Raises :class:`NotImplementedError` on POSIX.
    """
    if not _IS_WINDOWS:
        raise NotImplementedError(
            "Windows Job Objects are a Win32-only primitive; the implementer "
            "SDK spawn hook only runs on win32."
        )

    import win32job  # local import so POSIX can import the module

    # Unnamed Job — no cross-process collision risk. SecurityAttributes=None,
    # Name="" means "create private, not inheritable, not named".
    handle = win32job.CreateJobObject(None, "")

    # Query the current ExtendedLimitInformation, set KILL_ON_JOB_CLOSE and
    # leave the two BREAKAWAY bits cleared. QueryInformation returns a dict on
    # pywin32; we mutate it in place and hand it back. (The SDK's own tests
    # would catch a schema drift here — we exercise the same path.)
    info = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
    basic = info["BasicLimitInformation"]
    basic["LimitFlags"] = basic.get("LimitFlags", 0) | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    # Explicitly clear the two breakaway bits — a stray inherited flag would
    # let a child escape the Job and defeat the whole design.
    basic["LimitFlags"] &= ~0x0800  # JOB_OBJECT_LIMIT_BREAKAWAY_OK
    basic["LimitFlags"] &= ~0x1000  # JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    info["BasicLimitInformation"] = basic
    win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)
    return JobState(handle=handle, session_id=session_id)


def close_handle(state: JobState) -> None:
    """Idempotent close for a :class:`JobState` — safe under any race.

    The sentinel — setting ``state.handle`` to ``None`` **before** the Win32
    API calls — is what makes this safe to call from multiple cleanup paths.
    Windows recycles handle values immediately after close, so a second
    ``CloseHandle`` on the same integer can destroy an unrelated resource
    (an OS-level bug, not a Python exception you can catch after the fact).

    ``TerminateJobObject`` failures are logged and swallowed; ``CloseHandle``
    then runs. ``CloseHandle`` failures are logged and swallowed too — a
    leaked Job handle is reclaimed by the OS at daemon exit, and losing the
    original exception (spawn timeout / cancel) to a cleanup diagnostic is
    strictly worse than a leak we can measure.
    """
    handle = state.handle
    if handle is None:
        return
    # SET THE SENTINEL FIRST — this is what makes the helper idempotent
    # against re-entry / concurrent cleanup. Do not move this line.
    state.handle = None
    if not _IS_WINDOWS:
        # On POSIX we should not get here (create_job raises before a
        # JobState can be built), but be defensive against a test injecting
        # one.
        raise NotImplementedError("close_handle is Win32-only")

    import pywintypes  # local import so POSIX can import the module
    import win32api
    import win32job

    try:
        win32job.TerminateJobObject(handle, 1)
    except pywintypes.error as exc:
        logger.debug(
            "adapter.job_terminate_failed session=%s winerror=%s",
            state.session_id,
            getattr(exc, "winerror", None),
        )
    try:
        win32api.CloseHandle(handle)
    except pywintypes.error as exc:
        logger.debug(
            "adapter.job_close_failed session=%s winerror=%s",
            state.session_id,
            getattr(exc, "winerror", None),
        )


def is_process_in_job(pid: int, job_state: JobState) -> bool:
    """Return whether ``pid`` is a member of ``job_state``'s Job.

    Used by the adapter's spawn to verify — after ``connect()`` returns —
    that the proxy actually caught the ``claude.exe`` spawn. A ``False``
    result is a fail-loud invariant violation (``adapter.job_assign_missed``
    per v12 §fail-loud), because we would otherwise have a process outside
    our Job and no way to reap it via ``KILL_ON_JOB_CLOSE``.
    """
    if not _IS_WINDOWS:
        raise NotImplementedError("is_process_in_job is Win32-only")
    if job_state.handle is None:
        return False

    import pywintypes
    import win32api
    import win32job

    try:
        hproc = win32api.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
    except pywintypes.error:
        return False
    try:
        return bool(win32job.IsProcessInJob(hproc, job_state.handle))
    finally:
        with contextlib.suppress(pywintypes.error):
            win32api.CloseHandle(hproc)


def lookup_and_assign_leftover(job_state: JobState, exe_absolute_path: str) -> None:
    """Assign any of our direct ``claude.exe`` children not already in the Job.

    The proxy has a tiny race between the SDK calling ``open_process`` and
    the ContextVar being read — see v12 §race. This is the "belt" that
    catches the miss: after a failed spawn (timeout, cancel, verify) we walk
    the current process's direct children, look for the SDK executable, and
    assign any that are not yet in our Job. The caller then closes the Job,
    and ``KILL_ON_JOB_CLOSE`` reaps them.

    Silent skip on ``NoSuchProcess`` / ``AccessDenied`` / Win32 error at
    per-child granularity (v5): the enumeration itself is best-effort and
    must not abort on a transient TOCTOU race. If the whole helper fails,
    the caller in the adapter's ``finally`` catches it, logs it as
    ``adapter.lookup_leftover_failed``, and continues to ``close_handle``.
    Losing a leftover to a diagnostic beats losing the original failure
    reason.
    """
    if not _IS_WINDOWS:
        raise NotImplementedError("lookup_and_assign_leftover is Win32-only")
    if job_state.handle is None:
        return

    import psutil  # local import so POSIX users of the module don't take it
    import pywintypes
    import win32api
    import win32job

    normalized_target = os.path.normcase(os.path.abspath(exe_absolute_path))
    try:
        children = psutil.Process(os.getpid()).children()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return

    for child in children:
        try:
            child_exe = child.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if os.path.normcase(os.path.abspath(child_exe)) != normalized_target:
            continue
        try:
            hproc = win32api.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, child.pid)
        except pywintypes.error:
            continue
        try:
            try:
                already_in = bool(win32job.IsProcessInJob(hproc, job_state.handle))
            except pywintypes.error:
                continue
            if already_in:
                continue
            try:
                win32job.AssignProcessToJobObject(job_state.handle, hproc)
            except pywintypes.error:
                # Transient — the child may have exited, or the Job may
                # already be terminating. Either way the caller's next
                # step is to close the Job, so nothing more we can do.
                continue
        finally:
            with contextlib.suppress(pywintypes.error):
                win32api.CloseHandle(hproc)


# --------------------------------------------------------------------------- #
# The proxy that goes into ``subprocess_cli.anyio``
# --------------------------------------------------------------------------- #


class _JobAwareAnyioProxy:
    """A thin proxy over the real ``anyio`` module that intercepts ``open_process``.

    Only ``open_process`` is overridden. Every other attribute (``sleep``,
    ``move_on_after``, the type namespaces …) is forwarded via
    ``__getattr__`` — the SDK sees an object that is behaviourally
    indistinguishable from the real module.

    On ``open_process``: read the ContextVar. ``None`` (default) means the
    caller is not our spawn — pass through, do nothing. A non-``None`` handle
    means "assign the just-created process to this Job atomically" — no
    ``await`` between spawn and assign, so no window for the child to run
    a grandchild the proxy would miss.
    """

    def __init__(self, real_anyio: Any) -> None:
        self._real = real_anyio

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def open_process(self, *args: Any, **kwargs: Any) -> Any:
        process = await self._real.open_process(*args, **kwargs)
        handle = _JOB_HANDLE_CTX.get()
        if handle is None:
            return process
        # No ``await`` from here to the Assign call — a single synchronous
        # frame is what closes the TOCTOU window.
        if not _IS_WINDOWS:
            # We should never enter this branch on POSIX because nothing
            # sets the ContextVar there, but be defensive.
            return process
        import pywintypes
        import win32api
        import win32job

        try:
            hproc = win32api.OpenProcess(
                _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, process.pid
            )
        except pywintypes.error:
            # Cannot open the process handle — attempt to terminate the
            # child directly so it does not leak, then re-raise.
            try:
                process.terminate()
            finally:
                raise
        try:
            try:
                win32job.AssignProcessToJobObject(handle, hproc)
            except pywintypes.error:
                # Assign failed — the child is not in our Job so
                # KILL_ON_JOB_CLOSE will not reap it. Terminate now,
                # re-raise, and let the caller's ``finally`` clean up
                # the Job.
                try:
                    process.terminate()
                finally:
                    raise
        finally:
            with contextlib.suppress(pywintypes.error):
                win32api.CloseHandle(hproc)
        return process


def install_hook() -> None:
    """Install ``_JobAwareAnyioProxy`` in the SDK transport module.

    Idempotent — safe to call multiple times at startup. Does nothing on
    POSIX (the ContextVar stays at its ``None`` default and the SDK's real
    ``anyio`` is never proxied).

    Called once from the daemon's composition root (added in v12). Import
    order does not matter as long as this runs before the first
    ``ImplementerSdkAdapter.spawn``.
    """
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    if not _IS_WINDOWS:
        _HOOK_INSTALLED = True
        return
    # Late import to keep the module importable on hosts that do not have
    # the SDK installed (e.g. a docs-only checkout).
    from claude_agent_sdk._internal.transport import subprocess_cli

    sdk_transport: Any = subprocess_cli  # narrow mypy's view of a dynamic attr
    current: Any = getattr(sdk_transport, "anyio", None)
    if isinstance(current, _JobAwareAnyioProxy):
        # Someone else won the race (or a reimport happened) — do nothing.
        _HOOK_INSTALLED = True
        return
    sdk_transport.anyio = _JobAwareAnyioProxy(current)
    _HOOK_INSTALLED = True


def _reset_hook_for_tests() -> None:
    """Undo the install for a test run.

    Uninstalls the proxy from ``subprocess_cli`` and clears the idempotence
    flag. Intended for use only from tests that install and then need to
    restore the module to its pristine state; production code never calls
    this.
    """
    global _HOOK_INSTALLED
    if not _IS_WINDOWS:
        _HOOK_INSTALLED = False
        return
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
    except ImportError:
        _HOOK_INSTALLED = False
        return
    sdk_transport: Any = subprocess_cli
    proxy: Any = getattr(sdk_transport, "anyio", None)
    if isinstance(proxy, _JobAwareAnyioProxy):
        sdk_transport.anyio = proxy._real
    _HOOK_INSTALLED = False


__all__ = [
    "_JOB_HANDLE_CTX",
    "JobState",
    "_JobAwareAnyioProxy",
    "_reset_hook_for_tests",
    "close_handle",
    "create_job",
    "install_hook",
    "is_process_in_job",
    "lookup_and_assign_leftover",
]
