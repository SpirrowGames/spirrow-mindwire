"""The Win32 surface the as-user launcher needs, behind one injectable protocol (unit 2a).

Everything the launcher does to the OS goes through :class:`Win32Api`, so the launcher's
ordering and fail-closed rules (msg-7104 / msg-7106 / msg-7108) are unit-tested with a fake on
any platform. The real binding, :class:`_CtypesWin32Api`, is plain ``ctypes`` — the same thing
as a P/Invoke, and no new dependency: pywin32 (already a Windows dependency) cannot build the
``STARTUPINFOEX`` handle list the spawn needs, and its ``CredRead`` hands the secret back as an
immutable ``bytes`` that cannot be wiped.

Secrets travel as :class:`SecretBuffer` — a mutable ctypes wide-char buffer the launcher wipes
(``memset`` 0, the ctypes spelling of ``SecureZeroMemory``) as soon as the spawn call returns.
Residual, stated plainly: Python ``str`` objects are immutable, so the one place a secret has to
become a ``str`` (the implementer PAT as it is copied into the environment block) leaves a copy
in the launcher's heap until the process exits. The launcher is a short-lived process that runs
as the owner, so that copy is readable only by the owner, who holds the secret anyway.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

# --- constants the launcher passes; tests pin them by value -----------------------------------
LOGON32_LOGON_INTERACTIVE = 2  # msg-7106: the account holds "Allow log on locally" only
LOGON32_PROVIDER_DEFAULT = 0
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# CreateProcessAsUserW with a token for another user needs these in the caller's token
# (Microsoft docs, CreateProcessAsUserW remarks). Local Security Policy names in the values.
SE_ASSIGNPRIMARYTOKEN_NAME = "SeAssignPrimaryTokenPrivilege"
SE_INCREASE_QUOTA_NAME = "SeIncreaseQuotaPrivilege"
SPAWN_PRIVILEGES: dict[str, str] = {
    SE_ASSIGNPRIMARYTOKEN_NAME: "Replace a process level token",
    SE_INCREASE_QUOTA_NAME: "Adjust memory quotas for a process",
}

ERROR_NOT_FOUND = 1168
ERROR_LOGON_FAILURE = 1326
ERROR_ACCOUNT_RESTRICTION = 1327
ERROR_ACCOUNT_DISABLED = 1331
ERROR_LOGON_TYPE_NOT_GRANTED = 1385
ERROR_PRIVILEGE_NOT_HELD = 1314
ERROR_ACCOUNT_LOCKED_OUT = 1909


class Win32Error(Exception):
    """A Win32 call failed; ``code`` is ``GetLastError()`` and ``api`` the function name."""

    def __init__(self, api: str, code: int) -> None:
        super().__init__(f"{api} failed ({code})")
        self.api = api
        self.code = code


class SecretBuffer:
    """A NUL-terminated wide-char buffer that can be wiped in place."""

    def __init__(self, length: int) -> None:
        self._buf = ctypes.create_unicode_buffer(max(length, 0) + 1)
        self._wiped = False

    @classmethod
    def from_text(cls, text: str) -> SecretBuffer:
        sb = cls(len(text))
        sb._buf.value = text
        return sb

    @classmethod
    def from_blob(cls, address: int, size: int) -> SecretBuffer:
        """Copy ``size`` raw bytes from ``address`` (a ``CredentialBlob``).

        A generic credential's blob is arbitrary bytes, so ``size`` may be odd. The buffer holds
        ``ceil(size / 2)`` characters plus the NUL slot: every byte fits and the last character
        is always a whole zero terminator (PR #485 review, odd ``CredentialBlobSize``).
        """
        if size < 0:
            raise ValueError("negative blob size")
        sb = cls((size + 1) // 2)
        if size:
            ctypes.memmove(sb._buf, address, size)
        return sb

    @classmethod
    def environment_block(cls, env: Mapping[str, str]) -> SecretBuffer:
        """``K=V\\0K=V\\0\\0`` in sorted, case-insensitive order (CreateProcess's rule).

        Where ``wchar_t`` is UTF-16 (Windows) the block is laid out in UTF-16 code units, so a
        character outside the BMP takes its two-unit surrogate pair (PR #485 review round 4). A
        one-element-per-code-point copy raises ``TypeError`` on such a character.
        """
        parts = [f"{k}={env[k]}" for k in sorted(env, key=str.upper)]
        body = "\0".join(parts) + "\0\0"
        if ctypes.sizeof(ctypes.c_wchar) == 2:
            units = body.encode("utf-16-le", "surrogatepass")
            sb = cls(len(units) // 2)
            ctypes.memmove(sb._buf, units, len(units))
            return sb
        sb = cls(len(body))
        sb._buf[: len(body)] = list(body)
        return sb

    @property
    def buffer(self) -> ctypes.Array[ctypes.c_wchar]:
        return self._buf

    @property
    def wiped(self) -> bool:
        return self._wiped

    def reveal_length(self) -> int:
        """Characters before the first NUL, without building a ``str`` of the secret."""
        for i in range(len(self._buf)):
            if self._buf[i] == "\0":
                return i
        return len(self._buf)

    def reveal(self) -> str:
        """The text up to the first NUL. Every call makes an un-wipeable ``str`` copy."""
        return self._buf.value

    def raw(self) -> str:
        """Every character, NULs included (an environment block); surrogate pairs decoded."""
        return ctypes.wstring_at(ctypes.addressof(self._buf), len(self._buf))

    def wipe(self) -> None:
        ctypes.memset(self._buf, 0, ctypes.sizeof(self._buf))
        self._wiped = True

    def __repr__(self) -> str:  # never the value, whatever logs this object
        return "SecretBuffer(<redacted>)"

    __str__ = __repr__


INVALID_HANDLE_VALUE: int = ctypes.c_void_p(-1).value or 0  # (HANDLE)-1 as ctypes returns it


def usable_std_handle(raw: int | None) -> int:
    """``GetStdHandle``'s result as a handle worth duplicating, or 0.

    With ``restype = wintypes.HANDLE`` ctypes hands back a plain ``int`` (or ``None`` for
    NULL), never a ``HANDLE`` object, and ``(HANDLE)-1`` arrives as the unsigned pointer value
    :data:`INVALID_HANDLE_VALUE`. Both NULL (no handle) and that value (the call failed) mean
    "nothing to give the child" (PR #485 review round 5; pinned by tests on the real restype).
    """
    if not raw or raw == INVALID_HANDLE_VALUE:
        return 0
    return int(raw)


def parse_environment_block(address: int) -> dict[str, str]:
    """Read a UTF-16 ``K=V\\0...\\0\\0`` block (``CreateEnvironmentBlock``'s output).

    Walks UTF-16 code units, not decoded characters. A character outside the BMP is one Python
    code point but two units in the block, so advancing by ``len(str)`` would land inside the
    current entry and misparse everything after it (PR #485 review round 4). Entries without a
    name (the ``=C:=C:\\...`` drive entries) are skipped.
    """
    units = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint16))
    env: dict[str, str] = {}
    start = i = 0
    while True:
        if units[i] == 0:
            if i == start:
                return env
            entry = ctypes.string_at(address + start * 2, (i - start) * 2).decode(
                "utf-16-le", "surrogatepass"
            )
            key, sep, value = entry.partition("=")
            if sep and key:
                env[key] = value
            start = i + 1
        i += 1


@dataclass(frozen=True)
class SpawnedProcess:
    process: int
    thread: int
    pid: int


class CredentialReader(Protocol):
    def cred_read(self, target: str) -> SecretBuffer | None:
        """The generic credential's secret, or ``None`` when it does not exist."""


class Win32Api(CredentialReader, Protocol):
    """Each method raises :class:`Win32Error` on failure unless stated otherwise."""

    def logon_user(self, user: str, domain: str, password: SecretBuffer, logon_type: int) -> int:
        """A token handle."""

    def create_environment_block(self, token: int) -> dict[str, str]:
        """``CreateEnvironmentBlock(token, bInherit=FALSE)``, read out and destroyed."""

    def create_kill_on_close_job(self) -> int:
        """A job handle with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``."""

    def enable_privileges(self, names: Sequence[str]) -> list[str]:
        """Enable each privilege in the launcher's own token; return the ones it does not hold.

        Best effort: never raises. A privilege the token lacks cannot be enabled; the spawn then
        fails with ``ERROR_PRIVILEGE_NOT_HELD`` and the launcher names what was missing.
        """

    def inheritable_std_handles(self) -> tuple[int, int, int]:
        """Inheritable duplicates of the launcher's stdin/stdout/stderr (0 where there is none).

        The caller closes them. The launcher's own handles are left untouched.
        """

    def create_process_as_user(
        self,
        *,
        token: int,
        command_line: str,
        creation_flags: int,
        environment: SecretBuffer,
        current_directory: str,
        startup_flags: int,
        desktop: str | None,
        std_handles: tuple[int, int, int],
        inherit_handles: Sequence[int],
    ) -> SpawnedProcess:
        """``CreateProcessAsUserW(token, bInheritHandles=TRUE)``, restricted through a
        ``PROC_THREAD_ATTRIBUTE_HANDLE_LIST`` to exactly ``inherit_handles``: the child inherits
        those and no other inheritable handle the launcher happens to hold."""

    def assign_process_to_job(self, job: int, process: int) -> None: ...

    def resume_thread(self, thread: int) -> None: ...

    def terminate_process(self, process: int, exit_code: int) -> None:
        """Best effort: never raises."""

    def wait_exit_code(self, process: int) -> int: ...

    def close_handle(self, handle: int) -> None:
        """Best effort: never raises."""


def default_api() -> Win32Api:
    """The real binding. Only Windows has one."""
    if sys.platform == "win32":
        return _CtypesWin32Api()
    raise RuntimeError("the as-user launcher runs on Windows only")


if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _uenv = ctypes.WinDLL("userenv", use_last_error=True)
    # Handle- and DWORD-returning functions: the ctypes default restype is a C int, which would
    # truncate a 64-bit HANDLE and turn the DWORD failure sentinel into -1.
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetStdHandle.restype = wintypes.HANDLE
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.WaitForSingleObject.restype = wintypes.DWORD

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    class _STARTUPINFOW(ctypes.Structure):
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

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _TOKEN_PRIVILEGES1(ctypes.Structure):  # noqa: N801 - Win32 name, one entry
        _fields_ = [
            ("PrivilegeCount", wintypes.DWORD),
            ("Luid", _LUID),
            ("Attributes", wintypes.DWORD),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 name
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Win32 name
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 name
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 name
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _CRED_TYPE_GENERIC = 1
    _INFINITE = 0xFFFFFFFF
    _WAIT_FAILED = 0xFFFFFFFF
    _STD_HANDLES = (-10, -11, -12)
    _DUPLICATE_SAME_ACCESS = 0x00000002
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _TOKEN_ADJUST_PRIVILEGES = 0x0020
    _TOKEN_QUERY = 0x0008
    _SE_PRIVILEGE_ENABLED = 0x00000002
    _ERROR_NOT_ALL_ASSIGNED = 1300

    def _fail(api: str) -> Win32Error:
        return Win32Error(api, ctypes.get_last_error())

    class _CtypesWin32Api:
        def cred_read(self, target: str) -> SecretBuffer | None:
            pcred = ctypes.POINTER(_CREDENTIALW)()
            if not _adv.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
                err = ctypes.get_last_error()
                if err == ERROR_NOT_FOUND:
                    return None
                raise Win32Error("CredReadW", err)
            try:
                size = int(pcred.contents.CredentialBlobSize)
                address = ctypes.cast(pcred.contents.CredentialBlob, ctypes.c_void_p).value
                return SecretBuffer.from_blob(int(address or 0), size)
            finally:
                _adv.CredFree(pcred)

        def logon_user(
            self, user: str, domain: str, password: SecretBuffer, logon_type: int
        ) -> int:
            token = wintypes.HANDLE()
            ok = _adv.LogonUserW(
                user,
                domain,
                password.buffer,
                logon_type,
                LOGON32_PROVIDER_DEFAULT,
                ctypes.byref(token),
            )
            if not ok:
                raise _fail("LogonUserW")
            return int(token.value or 0)

        def create_environment_block(self, token: int) -> dict[str, str]:
            block = ctypes.c_void_p()
            if not _uenv.CreateEnvironmentBlock(ctypes.byref(block), wintypes.HANDLE(token), False):
                raise _fail("CreateEnvironmentBlock")
            try:
                return parse_environment_block(int(block.value or 0))
            finally:
                _uenv.DestroyEnvironmentBlock(block)

        def create_kill_on_close_job(self) -> int:
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise _fail("CreateJobObjectW")
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(
                wintypes.HANDLE(job),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                err = _fail("SetInformationJobObject")
                _k32.CloseHandle(wintypes.HANDLE(job))
                raise err
            return int(job)

        def enable_privileges(self, names: Sequence[str]) -> list[str]:
            missing: list[str] = []
            token = wintypes.HANDLE()
            if not _adv.OpenProcessToken(
                wintypes.HANDLE(_k32.GetCurrentProcess()),
                _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY,
                ctypes.byref(token),
            ):
                return list(names)
            try:
                for name in names:
                    tp = _TOKEN_PRIVILEGES1()
                    tp.PrivilegeCount = 1
                    tp.Attributes = _SE_PRIVILEGE_ENABLED
                    if not _adv.LookupPrivilegeValueW(None, name, ctypes.byref(tp.Luid)):
                        missing.append(name)
                        continue
                    ok = _adv.AdjustTokenPrivileges(
                        token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None
                    )
                    if not ok or ctypes.get_last_error() == _ERROR_NOT_ALL_ASSIGNED:
                        missing.append(name)
            finally:
                _k32.CloseHandle(token)
            return missing

        def inheritable_std_handles(self) -> tuple[int, int, int]:
            me = wintypes.HANDLE(_k32.GetCurrentProcess())
            out: list[int] = []
            try:
                for n in _STD_HANDLES:
                    src = usable_std_handle(_k32.GetStdHandle(n))
                    if not src:
                        out.append(0)
                        continue
                    dup = wintypes.HANDLE()
                    if not _k32.DuplicateHandle(
                        me,
                        wintypes.HANDLE(src),
                        me,
                        ctypes.byref(dup),
                        0,
                        True,
                        _DUPLICATE_SAME_ACCESS,
                    ):
                        raise _fail("DuplicateHandle")
                    out.append(int(dup.value or 0))
            except Win32Error:
                for h in out:
                    self.close_handle(h)
                raise
            return (out[0], out[1], out[2])

        def create_process_as_user(
            self,
            *,
            token: int,
            command_line: str,
            creation_flags: int,
            environment: SecretBuffer,
            current_directory: str,
            startup_flags: int,
            desktop: str | None,
            std_handles: tuple[int, int, int],
            inherit_handles: Sequence[int],
        ) -> SpawnedProcess:
            handles = list(dict.fromkeys(h for h in inherit_handles if h))
            six = _STARTUPINFOEXW()
            si = six.StartupInfo
            si.cb = ctypes.sizeof(six) if handles else ctypes.sizeof(si)
            si.dwFlags = startup_flags
            si.lpDesktop = desktop
            si.hStdInput, si.hStdOutput, si.hStdError = std_handles
            handle_array = (wintypes.HANDLE * max(len(handles), 1))(*handles)
            attr_buf = None
            if handles:
                size = ctypes.c_size_t()
                _k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
                attr_buf = ctypes.create_string_buffer(size.value)
                if not _k32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size)):
                    raise _fail("InitializeProcThreadAttributeList")
            try:
                if attr_buf is not None:
                    if not _k32.UpdateProcThreadAttribute(
                        attr_buf,
                        0,
                        ctypes.c_size_t(_PROC_THREAD_ATTRIBUTE_HANDLE_LIST),
                        handle_array,
                        ctypes.c_size_t(ctypes.sizeof(wintypes.HANDLE) * len(handles)),
                        None,
                        None,
                    ):
                        raise _fail("UpdateProcThreadAttribute")
                    six.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)
                    creation_flags |= EXTENDED_STARTUPINFO_PRESENT
                pi = _PROCESS_INFORMATION()
                cmd = ctypes.create_unicode_buffer(command_line)  # the API may write to it
                ok = _adv.CreateProcessAsUserW(
                    wintypes.HANDLE(token),
                    None,
                    cmd,
                    None,
                    None,
                    bool(handles),
                    creation_flags,
                    ctypes.cast(environment.buffer, ctypes.c_void_p),
                    current_directory,
                    ctypes.byref(six),
                    ctypes.byref(pi),
                )
                if not ok:
                    raise _fail("CreateProcessAsUserW")
            finally:
                if attr_buf is not None:
                    _k32.DeleteProcThreadAttributeList(attr_buf)
            return SpawnedProcess(int(pi.hProcess or 0), int(pi.hThread or 0), int(pi.dwProcessId))

        def assign_process_to_job(self, job: int, process: int) -> None:
            if not _k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(process)):
                raise _fail("AssignProcessToJobObject")

        def resume_thread(self, thread: int) -> None:
            if int(_k32.ResumeThread(wintypes.HANDLE(thread))) == _WAIT_FAILED:  # (DWORD)-1
                raise _fail("ResumeThread")

        def terminate_process(self, process: int, exit_code: int) -> None:
            _k32.TerminateProcess(wintypes.HANDLE(process), exit_code)

        def wait_exit_code(self, process: int) -> int:
            if int(_k32.WaitForSingleObject(wintypes.HANDLE(process), _INFINITE)) == _WAIT_FAILED:
                raise _fail("WaitForSingleObject")
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(wintypes.HANDLE(process), ctypes.byref(code)):
                raise _fail("GetExitCodeProcess")
            return int(code.value)

        def close_handle(self, handle: int) -> None:
            if handle:
                _k32.CloseHandle(wintypes.HANDLE(handle))
