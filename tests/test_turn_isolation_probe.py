"""S8 probe, spec revision 5 (T-executor-pool-under-one-conductor msg-7097).

Real git repositories; the "as the turn user" runner is injected (CI has no second user), so
these run as the current user with failures injected per directory.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from spirrow_mindwire.turn_isolation.check import check_turn_isolation, run
from spirrow_mindwire.turn_isolation.errors import (
    SECRETS_STOP_EXIT_CODE,
    TurnIsolationStop,
)
from spirrow_mindwire.turn_isolation.launcher import (
    IMPLEMENTER_PAT_CREDENTIAL,
    TURN_USER_CREDENTIAL,
)
from spirrow_mindwire.turn_isolation.probe import (
    PROBE_PREFIX,
    LauncherOps,
    RunResult,
    probe_clone,
    probe_write_script,
)
from spirrow_mindwire.turn_isolation.win32 import SecretBuffer

PID = 31337


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


class LocalOps:
    """Runs as the current user. ``fail_dirs`` makes the write probe fail there."""

    def __init__(
        self, *, fail_dirs: set[Path] | None = None, rev_parse: RunResult | None = None
    ) -> None:
        self.fail_dirs = {Path(os.path.normcase(str(d))) for d in (fail_dirs or set())}
        self.rev_parse_override = rev_parse
        self.written: list[Path] = []

    def rev_parse(self, clone: Path) -> RunResult:
        if self.rev_parse_override is not None:
            return self.rev_parse_override
        p = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--absolute-git-dir", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        return RunResult(p.returncode, p.stdout, p.stderr)

    def probe_write(self, directory: Path, pid: int) -> RunResult:
        if Path(os.path.normcase(str(directory))) in self.fail_dirs:
            return RunResult(1, "", "Access to the path is denied.")
        for leftover in directory.glob(f"{PROBE_PREFIX}*"):
            leftover.unlink()
        probe = directory / f"{PROBE_PREFIX}{pid}"
        probe.write_bytes(b"")
        probe.unlink()
        self.written.append(directory)
        return RunResult(0, "", "")


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _status_clean(repo: Path) -> None:
    assert _git(repo, "status", "--porcelain", "--ignored") == ""


def test_plain_clone_probes_only_dot_git(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "plain")
    ops = LocalOps()
    dirs = probe_clone(repo, ops, pid=PID)
    assert len(dirs) == 1 and _same(dirs[0], repo / ".git")
    _status_clean(repo)


def test_worktree_probes_its_git_dir_and_the_common_dir(tmp_path: Path) -> None:
    main = _repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "side")
    dirs = probe_clone(wt, LocalOps(), pid=PID)
    assert len(dirs) == 2
    assert _same(dirs[0], main / ".git" / "worktrees" / "wt")
    assert _same(dirs[1], main / ".git")
    assert (wt / ".git").is_file()  # the pointer file was never treated as a directory
    _status_clean(wt)
    _status_clean(main)


@pytest.mark.parametrize("which", [0, 1])
def test_worktree_write_failure_names_that_directory(tmp_path: Path, which: int) -> None:
    main = _repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "side")
    targets = [main / ".git" / "worktrees" / "wt", main / ".git"]
    bad = Path(os.path.abspath(targets[which].resolve()))
    with pytest.raises(TurnIsolationStop) as info:
        probe_clone(wt, LocalOps(fail_dirs={bad}), pid=PID)
    assert info.value.reason.startswith(f"turn user cannot use clone {wt}: ACL (")
    assert _same(Path(info.value.reason.split("ACL (", 1)[1].rstrip(")")), bad)


def test_submodule_probes_modules_dir_not_the_pointer_file(tmp_path: Path) -> None:
    sub_src = _repo(tmp_path / "subsrc")
    top = _repo(tmp_path / "top")
    _git(top, "submodule", "add", "-q", str(sub_src), "sm")
    _git(top, "commit", "-q", "-m", "sm")
    sm = top / "sm"
    assert (sm / ".git").is_file()
    dirs = probe_clone(sm, LocalOps(), pid=PID)
    assert len(dirs) == 1 and _same(dirs[0], top / ".git" / "modules" / "sm")
    _status_clean(sm)
    _status_clean(top)


def test_leftover_probe_files_are_removed_and_tree_stays_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "plain")
    stale = repo / ".git" / f"{PROBE_PREFIX}999"
    stale.write_bytes(b"")  # an interrupted earlier probe
    _status_clean(repo)  # clone_guard would see a clean tree even before cleanup
    probe_clone(repo, LocalOps(), pid=PID)
    assert not stale.exists()
    assert not list((repo / ".git").glob(f"{PROBE_PREFIX}*"))


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: detected dubious ownership in repository at 'C:/c'\n",
        "fatal: unsafe repository ('C:/c' is owned by someone else)\n",
    ],
)
def test_ownership_refusal_is_safe_directory(tmp_path: Path, stderr: str) -> None:
    clone = tmp_path / "c"
    ops = LocalOps(rev_parse=RunResult(128, "", stderr))
    with pytest.raises(TurnIsolationStop) as info:
        probe_clone(clone, ops, pid=PID)
    assert info.value.reason == f"turn user cannot use clone {clone}: safe.directory"


def test_other_git_failure_carries_first_stderr_line(tmp_path: Path) -> None:
    clone = tmp_path / "missing"
    with pytest.raises(TurnIsolationStop) as info:
        probe_clone(clone, LocalOps(), pid=PID)
    assert info.value.reason.startswith(f"turn user cannot use clone {clone}: git: ")
    assert "\n" not in info.value.reason


def test_relative_common_dir_is_resolved_against_the_clone(tmp_path: Path) -> None:
    clone = _repo(tmp_path / "plain")
    abs_git = str(clone / ".git")
    ops = LocalOps(rev_parse=RunResult(0, f"{abs_git}\n.git\n", ""))
    dirs = probe_clone(clone, ops, pid=PID)
    assert len(dirs) == 1  # the relative ".git" resolved to the same directory, deduplicated


def test_probe_script_targets_only_the_given_directory() -> None:
    script = probe_write_script(Path(r"C:\it's\.git"), PID)
    assert "'C:\\it''s\\.git'" in script  # single quote doubled for PowerShell
    assert f"'{PROBE_PREFIX}{PID}'" in script
    assert f"-Filter '{PROBE_PREFIX}*'" in script


def test_launcher_ops_reraises_the_launchers_own_stop(tmp_path: Path) -> None:
    script = tmp_path / "fake_launcher.py"
    script.write_text(
        "import sys\n"
        "print('turn-isolation: STOP turn user logon failed: bad credential', file=sys.stderr)\n"
        f"sys.exit({SECRETS_STOP_EXIT_CODE})\n",
        encoding="utf-8",
    )
    import sys

    ops = LauncherOps(launcher=[sys.executable, str(script)])
    with pytest.raises(TurnIsolationStop) as info:
        ops.rev_parse(tmp_path)
    assert info.value.reason == "turn user logon failed: bad credential"


def test_launcher_ops_encodes_the_probe_script(tmp_path: Path) -> None:
    script = tmp_path / "echo_launcher.py"
    script.write_text("import sys\nprint(sys.argv[-1])\n", encoding="utf-8")
    import sys

    res = LauncherOps(launcher=[sys.executable, str(script)]).probe_write(tmp_path, PID)
    decoded = base64.b64decode(res.stdout.strip()).decode("utf-16-le")
    assert decoded == probe_write_script(tmp_path, PID)


# --- check_turn_isolation (owner-side S8) -------------------------------------------------------


class CredApi:
    def __init__(self, creds: dict[str, str]) -> None:
        self.creds = creds
        self.read: list[SecretBuffer] = []

    def cred_read(self, target: str) -> SecretBuffer | None:
        if target not in self.creds:
            return None
        sb = SecretBuffer.from_text(self.creds[target])
        self.read.append(sb)
        return sb


def test_isolation_off_does_nothing(tmp_path: Path) -> None:
    def boom() -> NoReturn:
        raise AssertionError("must not be called when isolation is off")

    assert check_turn_isolation(None, [tmp_path], api_factory=boom, ops_factory=boom) is False


def test_check_passes_and_wipes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "plain")
    api = CredApi({TURN_USER_CREDENTIAL: "pw", IMPLEMENTER_PAT_CREDENTIAL: "pat"})
    assert check_turn_isolation(
        "mindwire-turn",
        [repo],
        api_factory=lambda: api,
        ops_factory=LocalOps,
    )
    assert len(api.read) == 2 and all(sb.wiped for sb in api.read)


def test_check_missing_credential_is_stop_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = CredApi({IMPLEMENTER_PAT_CREDENTIAL: "pat"})
    rc = run("mindwire-turn", [tmp_path], api_factory=lambda: api, ops_factory=LocalOps)
    assert rc == SECRETS_STOP_EXIT_CODE
    assert "turn-isolation: STOP turn user credential missing" in capsys.readouterr().err
