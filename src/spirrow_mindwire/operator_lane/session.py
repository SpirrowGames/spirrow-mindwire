"""Starting processes: the detached runner (D1' Spawner) and the isolated session (D2'', D3⁵).

* :class:`DetachedRunnerSpawner` — the operator pass starts ``run-session`` detached and returns at
  once (D1': the tick is blocked only for the seconds a start takes). Swappable behind
  :class:`Spawner` for ``claude --bg`` once T-spawn-via-claude-bg settles.
* :class:`JobSessionLauncher` — the runner starts ``claude`` SUSPENDED, puts it in a Job Object with
  ``KILL_ON_JOB_CLOSE``, resumes it, waits with a timeout and kills the whole Job past it (D3⁵). If
  the runner dies, the OS closes its Job handle and every process in the Job dies with it, so no
  session can outlive its runner and post late. With ``run_as_user`` set the session is started
  through ``CreateProcessWithLogonW`` as that user (D2'' (b-2)), whose password is read from the
  Windows Credential Manager; the session then has that user's own environment — none of the
  runner's variables (the magickit URL among them) reach it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_LOGON_WITH_PROFILE = 0x00000001
_STARTF_USESTDHANDLES = 0x00000100
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


def _win(ctypes_module: Any) -> Any:
    """``ctypes.windll`` without a platform-dependent type error (Windows-only callers)."""
    return getattr(ctypes_module, "windll")  # noqa: B009 - absent on POSIX type stubs


class Spawner(Protocol):
    def spawn_runner(self, *, run_id: str, data_dir: Path, log_path: Path) -> int:
        """Start ``run-session`` for ``run_id`` detached; return its pid."""


@dataclass(frozen=True)
class DetachedRunnerSpawner:
    python: str = sys.executable

    def argv(self, run_id: str, data_dir: Path) -> list[str]:
        return [
            self.python,
            "-m",
            "spirrow_mindwire.operator_lane",
            "run-session",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
        ]

    def spawn_runner(self, *, run_id: str, data_dir: Path, log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "close_fds": True,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
            }
            if sys.platform == "win32":
                flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
                try:
                    proc = subprocess.Popen(
                        self.argv(run_id, data_dir),
                        creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB,
                        **kwargs,
                    )
                except OSError:
                    # The scheduler's Job may forbid breakaway; detached is still detached.
                    proc = subprocess.Popen(
                        self.argv(run_id, data_dir), creationflags=flags, **kwargs
                    )
            else:
                proc = subprocess.Popen(
                    self.argv(run_id, data_dir), start_new_session=True, **kwargs
                )
        return int(proc.pid)


@dataclass(frozen=True)
class SessionOutcome:
    exit_code: int | None
    timed_out: bool
    error: str | None = None


class SessionLauncher(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        cwd: str | None,
        timeout_sec: int,
    ) -> SessionOutcome:
        """Run ``argv`` to completion (or kill it at ``timeout_sec``); never raise."""


def read_credential(target: str) -> tuple[str, str]:
    """``(user, password)`` of a generic Windows Credential Manager entry (ADR-23 D9 style)."""
    import win32cred

    cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
    blob = cred["CredentialBlob"]
    password = blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)
    return str(cred["UserName"]), password


@dataclass(frozen=True)
class JobSessionLauncher:
    """Windows only. ``run_as_user=""`` starts the session as the runner's own user (tests)."""

    run_as_user: str = ""
    credential_target: str = ""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        cwd: str | None,
        timeout_sec: int,
    ) -> SessionOutcome:
        if sys.platform != "win32":
            return SessionOutcome(None, False, "the session launcher is Windows-only")
        from ..adapters._sdk_job_hook import close_handle, create_job

        try:
            job = create_job(f"operator-lane:{stdout_path.stem}")
        except Exception as exc:
            return SessionOutcome(None, False, f"cannot create the Job Object: {exc}")
        try:
            with (
                open(stdin_path, "rb") as fin,
                open(stdout_path, "wb") as fout,
                open(stderr_path, "wb") as ferr,
            ):
                try:
                    pid = self._start_suspended(argv, fin, fout, ferr, cwd)
                except Exception as exc:
                    return SessionOutcome(None, False, f"cannot start the session: {exc}")
                return _assign_resume_wait(job.handle, pid, timeout_sec)
        finally:
            close_handle(job)

    def _start_suspended(
        self, argv: Sequence[str], fin: Any, fout: Any, ferr: Any, cwd: str | None
    ) -> int:
        if not self.run_as_user:
            proc = subprocess.Popen(
                list(argv),
                stdin=fin,
                stdout=fout,
                stderr=ferr,
                cwd=cwd or None,
                creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW,
            )
            return int(proc.pid)
        user, password = read_credential(self.credential_target)
        return _create_process_with_logon(user, password, argv, fin, fout, ferr, cwd)


def _create_process_with_logon(
    user: str, password: str, argv: Sequence[str], fin: Any, fout: Any, ferr: Any, cwd: str | None
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESSINFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    # Windows-only attributes go through ``Any`` so mypy on a POSIX runner accepts them.
    win_msvcrt: Any = msvcrt
    win_os: Any = os
    win_ctypes: Any = ctypes
    handles = []
    for fh in (fin, fout, ferr):
        h = win_msvcrt.get_osfhandle(fh.fileno())
        win_os.set_handle_inheritable(h, True)
        handles.append(h)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = _STARTF_USESTDHANDLES
    si.hStdInput, si.hStdOutput, si.hStdError = handles
    pi = PROCESSINFORMATION()
    domain = "."
    if "\\" in user:
        domain, user = user.split("\\", 1)
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
    ok = _win(ctypes).advapi32.CreateProcessWithLogonW(
        user,
        domain,
        password,
        _LOGON_WITH_PROFILE,
        None,
        cmdline,
        _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT,
        None,
        cwd or None,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        raise OSError(win_ctypes.GetLastError(), "CreateProcessWithLogonW failed")
    kernel32 = _win(ctypes).kernel32
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    return int(pi.dwProcessId)


def _assign_resume_wait(job_handle: Any, pid: int, timeout_sec: int) -> SessionOutcome:
    """Job-assign the suspended process, resume it, wait; kill the Job on timeout."""
    import ctypes

    import win32api
    import win32job

    kernel32 = _win(ctypes).kernel32
    ntdll = _win(ctypes).ntdll
    access = (
        _PROCESS_TERMINATE
        | _PROCESS_SET_QUOTA
        | _PROCESS_SUSPEND_RESUME
        | _PROCESS_QUERY_LIMITED_INFORMATION
        | _SYNCHRONIZE
    )
    proc = win32api.OpenProcess(access, False, pid)
    try:
        try:
            win32job.AssignProcessToJobObject(job_handle, proc)
        except Exception as exc:
            # Not in the Job means not killable as a tree: never let it run.
            win32api.TerminateProcess(proc, 1)
            return SessionOutcome(None, False, f"cannot assign the session to the Job: {exc}")
        ntdll.NtResumeProcess(int(proc))
        waited = kernel32.WaitForSingleObject(int(proc), int(timeout_sec * 1000))
        if waited == _WAIT_TIMEOUT:
            win32job.TerminateJobObject(job_handle, 1)
            kernel32.WaitForSingleObject(int(proc), 10_000)
            return SessionOutcome(None, True)
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(int(proc), ctypes.byref(code))
        return SessionOutcome(int(code.value), False)
    finally:
        win32api.CloseHandle(proc)


__all__ = [
    "DetachedRunnerSpawner",
    "JobSessionLauncher",
    "SessionLauncher",
    "SessionOutcome",
    "Spawner",
    "read_credential",
]
