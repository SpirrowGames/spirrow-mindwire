"""Unit 2a as-user launcher (T-executor-pool-under-one-conductor msg-7104 / 7106 / 7108).

The Win32 surface is a recording fake, so every ordering and fail-closed rule is checked on any
platform. The real ``CreateProcessWithLogonW`` path is the operator spike's job.
"""

from __future__ import annotations

import ast
import ctypes
import io
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.turn_isolation import launcher as launcher_mod
from spirrow_mindwire.turn_isolation.errors import (
    SECRETS_STOP_EXIT_CODE,
    TurnIsolationStop,
    parse_stop_line,
)
from spirrow_mindwire.turn_isolation.launcher import (
    IMPLEMENTER_PAT_CREDENTIAL,
    TURN_USER_CREDENTIAL,
    TurnUser,
    launch,
    main,
)
from spirrow_mindwire.turn_isolation.win32 import (
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    LOGON32_LOGON_INTERACTIVE,
    LOGON_WITH_PROFILE,
    STARTF_USESTDHANDLES,
    SecretBuffer,
    SpawnedProcess,
    Win32Error,
)

PASSWORD = "turn-user-pa55word-SENTINEL"
PAT = "ghp_implementerPAT_SENTINEL"
OWNER_NAYSAYER_TOKEN = "naysayer-token-SENTINEL"

TURN_DEFAULT_ENV = {
    "USERPROFILE": r"C:\Users\mindwire-turn",
    "APPDATA": r"C:\Users\mindwire-turn\AppData\Roaming",
    "TEMP": r"C:\Users\mindwire-turn\AppData\Local\Temp",
    "Path": r"C:\Windows\system32;C:\Windows",
}

TOKEN, JOB, PROC, THREAD = 101, 202, 303, 404
STD = (11, 12, 13)


class FakeApi:
    def __init__(
        self,
        *,
        creds: dict[str, str | None] | None = None,
        fail: dict[str, int] | None = None,
        exit_code: int = 0,
    ) -> None:
        self.creds: dict[str, str | None] = (
            creds
            if creds is not None
            else {TURN_USER_CREDENTIAL: PASSWORD, IMPLEMENTER_PAT_CREDENTIAL: PAT}
        )
        self.fail = fail or {}
        self.exit_code = exit_code
        self.calls: list[str] = []
        self.spawn_kwargs: dict[str, Any] | None = None
        self.spawn_env_raw: str | None = None
        self.spawn_password_seen: str | None = None
        self.secrets: list[SecretBuffer] = []
        self.closed: list[int] = []
        self.terminated: list[int] = []
        self.logon_type: int | None = None
        self.job_closed_kills_child = False

    def _maybe_fail(self, api: str) -> None:
        if api in self.fail:
            raise Win32Error(api, self.fail[api])

    def cred_read(self, target: str) -> SecretBuffer | None:
        self.calls.append(f"cred_read:{target}")
        self._maybe_fail("CredReadW")
        value = self.creds.get(target)
        if value is None:
            return None
        sb = SecretBuffer.from_text(value)
        self.secrets.append(sb)
        return sb

    def logon_user(self, user: str, domain: str, password: SecretBuffer, logon_type: int) -> int:
        self.calls.append("logon_user")
        self.logon_type = logon_type
        self._maybe_fail("LogonUserW")
        return TOKEN

    def create_environment_block(self, token: int) -> dict[str, str]:
        assert token == TOKEN
        self.calls.append("create_environment_block")
        self._maybe_fail("CreateEnvironmentBlock")
        return dict(TURN_DEFAULT_ENV)

    def create_kill_on_close_job(self) -> int:
        self.calls.append("create_job")
        self._maybe_fail("CreateJobObjectW")
        return JOB

    def std_handles(self) -> tuple[int, int, int]:
        return STD

    def create_process_with_logon(self, **kwargs: Any) -> SpawnedProcess:
        self.calls.append("spawn")
        self.spawn_kwargs = kwargs
        self.spawn_env_raw = kwargs["environment"].raw()
        self.spawn_password_seen = kwargs["password"].reveal()
        self._maybe_fail("CreateProcessWithLogonW")
        return SpawnedProcess(PROC, THREAD, 4242)

    def assign_process_to_job(self, job: int, process: int) -> None:
        assert (job, process) == (JOB, PROC)
        self.calls.append("assign")
        self._maybe_fail("AssignProcessToJobObject")

    def resume_thread(self, thread: int) -> None:
        assert thread == THREAD
        self.calls.append("resume")
        self._maybe_fail("ResumeThread")

    def terminate_process(self, process: int, exit_code: int) -> None:
        self.calls.append("terminate")
        self.terminated.append(process)

    def wait_exit_code(self, process: int) -> int:
        self.calls.append("wait")
        return self.exit_code

    def close_handle(self, handle: int) -> None:
        if handle:
            self.closed.append(handle)
            if handle == JOB and PROC not in self.terminated and "spawn" in self.calls:
                self.job_closed_kills_child = True  # KILL_ON_JOB_CLOSE semantics


USER = TurnUser.parse("mindwire-turn")


def _launch(api: FakeApi, argv: list[str] | None = None) -> int:
    return launch(argv or ["claude", "--print", "hi"], USER, api=api, current_directory=r"C:\c")


def _stop(api: FakeApi) -> TurnIsolationStop:
    with pytest.raises(TurnIsolationStop) as info:
        _launch(api)
    assert info.value.exit_code == SECRETS_STOP_EXIT_CODE
    return info.value


def _env_of(raw: str) -> dict[str, str]:
    return dict(e.split("=", 1) for e in raw.split("\0") if e)


# --- happy path -------------------------------------------------------------------------------


def test_happy_path_order_flags_and_exit_code() -> None:
    api = FakeApi(exit_code=7)
    assert _launch(api, ["claude", "a b"]) == 7
    order = [c for c in api.calls if not c.startswith("cred_read")]
    assert order == [
        "create_job",
        "logon_user",
        "create_environment_block",
        "spawn",
        "assign",
        "resume",
        "wait",
    ]
    kw = api.spawn_kwargs
    assert kw is not None
    assert api.logon_type == LOGON32_LOGON_INTERACTIVE
    assert kw["logon_flags"] == LOGON_WITH_PROFILE
    assert kw["creation_flags"] == CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED
    assert kw["startup_flags"] == STARTF_USESTDHANDLES
    assert kw["std_handles"] == STD  # the launcher's own stdio, passed through
    assert kw["desktop"] is None  # msg-7108: seclogon grants winsta/desktop only for NULL
    assert kw["environment"] is not None  # never a NULL lpEnvironment
    assert kw["command_line"] == 'claude "a b"'
    assert (kw["user"], kw["domain"]) == ("mindwire-turn", ".")
    assert api.spawn_password_seen == PASSWORD


def test_environment_is_turn_users_block_plus_gh_token_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINDWIRE_NAYSAYER_GITHUB_TOKEN", OWNER_NAYSAYER_TOKEN)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\owner")
    monkeypatch.setenv("APPDATA", r"C:\Users\owner\AppData\Roaming")
    api = FakeApi()
    _launch(api)
    assert api.spawn_env_raw is not None
    env = _env_of(api.spawn_env_raw)
    assert env == {**TURN_DEFAULT_ENV, "GH_TOKEN": PAT}
    assert OWNER_NAYSAYER_TOKEN not in api.spawn_env_raw
    assert PASSWORD not in api.spawn_env_raw


def test_password_never_in_argv_env_or_output(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi()
    _launch(api)
    kw = api.spawn_kwargs
    assert kw is not None
    assert PASSWORD not in kw["command_line"]
    assert api.spawn_env_raw is not None and PASSWORD not in api.spawn_env_raw
    assert PASSWORD not in repr(kw)  # a SecretBuffer prints redacted
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert PAT not in out.out + out.err


def test_secrets_are_wiped_after_spawn() -> None:
    api = FakeApi()
    _launch(api)
    assert api.secrets and all(sb.wiped for sb in api.secrets)
    assert api.spawn_kwargs is not None and api.spawn_kwargs["environment"].wiped
    assert TOKEN in api.closed and JOB in api.closed


# --- credentials ------------------------------------------------------------------------------


def test_missing_turn_user_credential_stops_before_anything() -> None:
    api = FakeApi(creds={IMPLEMENTER_PAT_CREDENTIAL: PAT})
    stop = _stop(api)
    assert stop.reason == "turn user credential missing: mindwire/turn-user"
    assert "logon_user" not in api.calls and "spawn" not in api.calls


def test_missing_pat_credential_stops_and_wipes_password() -> None:
    api = FakeApi(creds={TURN_USER_CREDENTIAL: PASSWORD})
    stop = _stop(api)
    assert stop.reason == "implementer PAT credential missing: mindwire/implementer-pat"
    assert "spawn" not in api.calls
    assert all(sb.wiped for sb in api.secrets)


def test_empty_credential_counts_as_missing() -> None:
    api = FakeApi(creds={TURN_USER_CREDENTIAL: "", IMPLEMENTER_PAT_CREDENTIAL: PAT})
    assert _stop(api).reason.startswith("turn user credential missing")


# --- logon / environment / spawn failures (msg-7104 fail closed, msg-7106 mapping) -------------


@pytest.mark.parametrize(
    ("api_name", "code", "reason"),
    [
        (
            "LogonUserW",
            1385,
            "turn user logon failed: logon right not granted (Allow log on locally)",
        ),
        ("LogonUserW", 1326, "turn user logon failed: bad credential"),
        ("LogonUserW", 1331, "turn user logon failed: account disabled/locked"),
        ("LogonUserW", 1909, "turn user logon failed: account disabled/locked"),
        ("LogonUserW", 5, "turn user logon failed: LogonUserW (5)"),
        ("CreateEnvironmentBlock", 8, "turn user logon failed: CreateEnvironmentBlock (8)"),
        (
            "CreateProcessWithLogonW",
            1385,
            "turn user logon failed: logon right not granted (Allow log on locally)",
        ),
        ("CreateProcessWithLogonW", 2, "turn user logon failed: CreateProcessWithLogonW (2)"),
    ],
)
def test_each_step_failure_stops_without_a_running_child(
    api_name: str, code: int, reason: str
) -> None:
    api = FakeApi(fail={api_name: code})
    assert _stop(api).reason == reason
    assert "resume" not in api.calls
    if api_name != "CreateProcessWithLogonW":
        assert "spawn" not in api.calls
    assert all(sb.wiped for sb in api.secrets)


def test_job_creation_failure_stops_before_logon() -> None:
    api = FakeApi(fail={"CreateJobObjectW": 5})
    assert _stop(api).reason == "turn job creation failed (5)"
    assert "logon_user" not in api.calls


# --- job object (msg-7104 objection 2) ---------------------------------------------------------


def test_resume_only_after_successful_assignment() -> None:
    api = FakeApi()
    _launch(api)
    assert api.calls.index("assign") < api.calls.index("resume")


def test_assignment_failure_terminates_and_never_resumes() -> None:
    api = FakeApi(fail={"AssignProcessToJobObject": 5})
    assert _stop(api).reason == "turn job assignment failed"
    assert "resume" not in api.calls
    assert api.terminated == [PROC]


def test_closing_the_job_handle_kills_the_child() -> None:
    api = FakeApi()
    _launch(api)
    assert api.job_closed_kills_child


# --- entry point ------------------------------------------------------------------------------


def test_main_prints_one_stop_line_and_returns_stop_code() -> None:
    api = FakeApi(fail={"LogonUserW": 1326})
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["claude"], api_factory=lambda: api, configured_user=lambda: "mindwire-turn")
    assert rc == SECRETS_STOP_EXIT_CODE
    assert parse_stop_line(err.getvalue()) == "turn user logon failed: bad credential"
    assert PASSWORD not in err.getvalue()


def test_main_without_configured_user_refuses() -> None:
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["claude"], api_factory=FakeApi, configured_user=lambda: None)
    assert rc == SECRETS_STOP_EXIT_CODE
    assert "[turn.isolation] user is not set" in err.getvalue()


def test_turn_user_parse() -> None:
    assert TurnUser.parse("mindwire-turn") == TurnUser("mindwire-turn", ".")
    assert TurnUser.parse(r"HOST\mindwire-turn") == TurnUser("mindwire-turn", "HOST")


def test_launcher_changes_no_dacl() -> None:
    """msg-7108: the stdio objection was declined; no DACL-changing API may appear."""
    src_dir = Path(launcher_mod.__file__).parent
    names: set[str] = set()
    for path in src_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
    assert not names & {"SetSecurityInfo", "SetKernelObjectSecurity", "SetNamedSecurityInfoW"}


@pytest.mark.parametrize("size", range(10))
def test_from_blob_fits_odd_sizes_and_keeps_a_terminator(size: int) -> None:
    """PR #485 review: an odd CredentialBlobSize must never overrun the buffer."""
    blob = (ctypes.c_ubyte * max(size, 1))(*([0x41] * size))
    sb = SecretBuffer.from_blob(ctypes.addressof(blob), size)
    assert ctypes.sizeof(sb.buffer) >= size + ctypes.sizeof(ctypes.c_wchar)
    assert sb.buffer[len(sb.buffer) - 1] == chr(0)
    assert bytes(ctypes.string_at(ctypes.addressof(sb.buffer), size)) == b"A" * size


def test_from_blob_utf16_round_trip() -> None:
    raw = "pa55-word".encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    sb = SecretBuffer.from_blob(ctypes.addressof(blob), len(raw))
    if ctypes.sizeof(ctypes.c_wchar) == 2:  # Windows: the blob is the text
        assert sb.reveal() == "pa55-word"
