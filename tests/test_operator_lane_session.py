"""T-operator-task-lane PR2: the session launcher's Job Object (D3⁵) and the detached spawner (D1').

The Job tests start real processes and are Windows-only: ``KILL_ON_JOB_CLOSE`` is the property
under test. They use ``run_as_user=""`` (the test's own user); the ``CreateProcessWithLogonW``
path needs the ``mw-operator`` account and is covered by the runbook's acceptance check instead.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from spirrow_mindwire.operator_lane.session import DetachedRunnerSpawner, JobSessionLauncher

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Win32-only")


def _child_script(pid_file: Path) -> list[str]:
    code = (
        "import os, time, pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(120)"
    )
    return [sys.executable, "-c", code]


def _wait_for(path: Path, seconds: float = 30) -> int:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared")


def _gone(pid: int, seconds: float = 10) -> bool:
    import psutil

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


@windows_only
def test_killing_the_runner_kills_the_session(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    (tmp_path / "in.txt").write_text("prompt", encoding="utf-8")
    runner = textwrap.dedent(
        f"""
        from pathlib import Path
        from spirrow_mindwire.operator_lane.session import JobSessionLauncher
        d = Path({str(tmp_path)!r})
        JobSessionLauncher().run(
            {_child_script(pid_file)!r},
            stdin_path=d / "in.txt", stdout_path=d / "out.txt", stderr_path=d / "err.txt",
            cwd=None, timeout_sec=300,
        )
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", runner])
    try:
        child = _wait_for(pid_file)
        proc.kill()
        proc.wait(10)
        assert _gone(child), "the session outlived its runner"
    finally:
        if proc.poll() is None:
            proc.kill()


@windows_only
def test_timeout_kills_the_session(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    (tmp_path / "in.txt").write_text("prompt", encoding="utf-8")
    outcome = JobSessionLauncher().run(
        _child_script(pid_file),
        stdin_path=tmp_path / "in.txt",
        stdout_path=tmp_path / "out.txt",
        stderr_path=tmp_path / "err.txt",
        cwd=None,
        timeout_sec=3,
    )
    assert outcome.timed_out
    assert _gone(_wait_for(pid_file, 1))


@windows_only
def test_session_exit_code_and_stdin_are_passed(tmp_path: Path) -> None:
    (tmp_path / "in.txt").write_text("hello", encoding="utf-8")
    code = "import sys; data = sys.stdin.read(); print(data.upper()); sys.exit(3)"
    outcome = JobSessionLauncher().run(
        [sys.executable, "-c", code],
        stdin_path=tmp_path / "in.txt",
        stdout_path=tmp_path / "out.txt",
        stderr_path=tmp_path / "err.txt",
        cwd=None,
        timeout_sec=60,
    )
    assert outcome == type(outcome)(3, False)
    assert (tmp_path / "out.txt").read_text().strip() == "HELLO"


def test_spawner_starts_run_session_for_the_run(tmp_path: Path) -> None:
    argv = DetachedRunnerSpawner(python="py").argv("run0001", tmp_path)
    assert argv == [
        "py",
        "-m",
        "spirrow_mindwire.operator_lane",
        "run-session",
        "--run-id",
        "run0001",
        "--data-dir",
        str(tmp_path),
    ]


def test_spawned_runner_is_detached_and_returns_at_once(tmp_path: Path) -> None:
    """D1': the pass is blocked only for the start, not for the run."""
    spawner = DetachedRunnerSpawner()
    started = time.monotonic()
    pid = spawner.spawn_runner(run_id="nolock", data_dir=tmp_path, log_path=tmp_path / "r.log")
    assert time.monotonic() - started < 10
    assert pid > 0
    # With no lock for this run the runner says so and exits; its output lands in the log.
    end = time.monotonic() + 60
    while time.monotonic() < end and "nothing to do" not in _read(tmp_path / "r.log"):
        time.sleep(0.2)
    assert "nothing to do" in _read(tmp_path / "r.log")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


# --- PR #506 gate: the session's environment is built, never inherited ---------------------------

from spirrow_mindwire.operator_lane import session as sess  # noqa: E402


def test_env_block_round_trips_and_is_sorted_case_insensitively() -> None:
    env = {"b": "2", "=C:": "C:/x", "A": "1=one"}
    block = sess.encode_env_block(env)
    assert block == "=C:=C:/x\0A=1=one\0b=2\0\0"
    assert sess.parse_env_block(block) == env


@pytest.mark.parametrize(
    "env",
    [
        {"MINDWIRE_MAGICKIT_MCP_URL": "http://other"},
        {"mindwire_mcp_api_key": "k"},
        {"SOME_URL": "http://magickit:8000/mcp"},  # the runner's value under another name
    ],
)
def test_isolation_check_refuses_runner_settings(env: dict[str, str]) -> None:
    runner = {"MINDWIRE_MAGICKIT_MCP_URL": "http://magickit:8000/mcp", "PATH": "C:/bin"}
    with pytest.raises(RuntimeError, match="runner settings"):
        sess.check_isolated_env({"PATH": "C:/bin", **env}, runner)


def test_isolation_check_accepts_the_users_own_environment() -> None:
    runner = {"MINDWIRE_MAGICKIT_MCP_URL": "http://magickit:8000/mcp", "MINDWIRE_X": " "}
    sess.check_isolated_env({"PATH": "C:/bin", "USERPROFILE": "C:/Users/mw", "Y": " "}, runner)


class _FakeWin:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        outer = self

        class _Advapi:
            def CreateProcessWithLogonW(self, *args: object) -> int:  # noqa: N802 - Win32 name
                outer.calls.append(args)
                return 1

        class _Kernel:
            def CloseHandle(self, handle: object) -> int:  # noqa: N802 - Win32 name
                return 1

        self.advapi32 = _Advapi()
        self.kernel32 = _Kernel()


@windows_only
def test_logon_start_passes_the_users_own_environment_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeWin()
    monkeypatch.setattr(sess, "_win", lambda _ctypes: fake)
    monkeypatch.setattr(
        sess, "_user_environment", lambda u, d, p: {"USERPROFILE": "C:/Users/mw", "PATH": "C:/b"}
    )
    monkeypatch.setenv("MINDWIRE_MAGICKIT_MCP_URL", "http://magickit:8000/mcp")
    files = [open(tmp_path / n, "wb") for n in ("in", "out", "err")]  # noqa: SIM115
    fin, fout, ferr = files
    try:
        sess._create_process_with_logon("mw-operator", "pw", ["claude"], fin, fout, ferr, None)
    finally:
        for f in files:
            f.close()
    (args,) = fake.calls
    env_block = args[7]
    assert env_block is not None  # never NULL: NULL would leave the environment to the API
    text = env_block[:]  # type: ignore[index]  # the whole c_wchar buffer, NULs included
    assert sess.parse_env_block(text) == {"USERPROFILE": "C:/Users/mw", "PATH": "C:/b"}
    assert "USERPROFILE=C:/Users/mw" in text
    assert "MINDWIRE_" not in text and "magickit" not in text


@windows_only
def test_logon_start_refuses_an_environment_with_runner_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeWin()
    monkeypatch.setattr(sess, "_win", lambda _ctypes: fake)
    monkeypatch.setattr(
        sess, "_user_environment", lambda u, d, p: {"MINDWIRE_MAGICKIT_MCP_URL": "http://m"}
    )
    files = [open(tmp_path / n, "wb") for n in ("in", "out", "err")]  # noqa: SIM115
    fin, fout, ferr = files
    try:
        with pytest.raises(RuntimeError, match="runner settings"):
            sess._create_process_with_logon("mw-operator", "pw", ["claude"], fin, fout, ferr, None)
    finally:
        for f in files:
            f.close()
    assert fake.calls == []  # nothing was started


@pytest.mark.parametrize(
    ("command", "hit"),
    [
        ("rm -rf x", True),
        ("rm -fr x", True),
        ("rm -r -f x", True),
        ("rm -f -R x", True),
        ("rm --recursive --force x", True),
        ("rm -r x", False),
        ("rm x -r; echo -f", False),
    ],
)
def test_recursive_delete_audit_tolerates_split_flags(command: str, hit: bool) -> None:
    from spirrow_mindwire.operator_lane.runner import AUDIT_PATTERNS

    pattern = dict(AUDIT_PATTERNS)["recursive delete"]
    assert bool(pattern.search(command)) is hit
