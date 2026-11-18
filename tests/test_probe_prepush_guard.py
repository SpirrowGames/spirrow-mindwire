"""probe_prepush_guard — cases (p)-(t) of design msg-9133 §4″-3 (approved msg-9134).

Each test builds a real clone in ``tmp_path`` whose ``core.hooksPath`` points at a
hooks dir. The guard used here is a minimal stand-in with the same contract as
spirrow-docs ``githooks/pre-push`` (marker, refuse non-FF with ``mindwire-guard`` on
stderr, else hand the saved stdin to ``git lfs pre-push``); the real hook's own
behaviour is tested in spirrow-docs. Branch names never come from
``init.defaultBranch``: the probe ref is made with ``update-ref`` (msg-9586 §1).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import probe_prepush_guard as mod

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GUARD = """#!/bin/sh
# mindwire-guard: non-ff-expect v1
tmp=$(mktemp) || exit 1
cat > "$tmp" || exit 1
while read lref lsha rref rsha; do
    case $rsha in
        *[!0]*)
            git merge-base --is-ancestor "$rsha" "$lsha" 2>/dev/null || {
                echo "mindwire-guard: refusing non-fast-forward $rref" >&2
                rm -f "$tmp"; exit 1; } ;;
    esac
done < "$tmp"
git lfs pre-push "$@" < "$tmp"
rc=$?
rm -f "$tmp"
exit $rc
"""

LFS_ONLY = """#!/bin/sh
command -v git-lfs >/dev/null 2>&1 || { echo >&2 "git-lfs missing"; exit 2; }
git lfs pre-push "$@"
"""


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path: Path, monkeypatch: Any) -> None:
    empty = tmp_path / "empty-global.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone(tmp_path: Path, hook_text: str = GUARD) -> Path:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text(hook_text, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    for k, v in (
        ("user.name", "t"),
        ("user.email", "t@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        _git(clone, "config", k, v)
    _git(clone, "commit", "-q", "--allow-empty", "--no-verify", "-m", "root")
    return clone


def _refs(clone: Path) -> str:
    return _git(clone, "for-each-ref", "--format=%(refname) %(objectname)")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], env: dict[str, str]) -> Any:
        self.calls.append(list(args))
        return mod._run_git(args, env)

    def pushes(self) -> list[list[str]]:
        return [a for a in self.calls if "push" in a]


# (p)
def test_probe_passes_on_an_armed_clone(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    before = _refs(clone)
    rec = Recorder()
    res = mod.probe(clone, run_git=rec, tmp_parent=tmp_path)
    assert res.passed, res.steps
    assert res.summary() == "pass"
    assert [n for n, _ok, _d in res.steps] == list(mod.STEPS)
    assert res.steps[0][0] == "transport"
    assert len(rec.pushes()) == 2
    assert _refs(clone) == before  # the clone's refs are untouched
    assert not list(tmp_path.glob("mindwire-probe-*"))  # step 7 cleaned up


def test_probe_works_on_a_root_commit_clone(tmp_path: Path) -> None:
    """C -> HEAD needs no HEAD~1, so a single-commit clone is probed fully."""
    clone = _clone(tmp_path)
    assert _git(clone, "rev-list", "--count", "HEAD") == "1"
    assert mod.probe(clone, tmp_parent=tmp_path).passed


# (q)
def test_probe_stops_before_any_push_when_canary_fails(tmp_path: Path) -> None:
    """The stub losing to a real git-lfs: `git lfs version` answers something else."""

    def impostor(_rec: Path) -> str:
        return "#!/bin/sh\necho 'git-lfs/3.7.1 (GitHub; windows amd64; go 1.24)'\nexit 0\n"

    clone = _clone(tmp_path)
    rec = Recorder()
    res = mod.probe(clone, run_git=rec, make_stub=impostor, tmp_parent=tmp_path)
    assert not res.passed
    assert res.steps[-1][0] == "canary"
    assert rec.pushes() == []  # no push ran, so real LFS never had a chance


# (r)
def test_probe_incomplete_when_lfs_fails_on_the_ff_push(tmp_path: Path) -> None:
    def failing(rec: Path) -> str:
        return mod.stub_text(rec).replace('exit "${MINDWIRE_LFS_STUB_EXIT:-0}"', "exit 1")

    clone = _clone(tmp_path)
    rec = Recorder()
    res = mod.probe(clone, run_git=rec, make_stub=failing, tmp_parent=tmp_path)
    assert not res.passed
    assert res.steps[-1][0] == "ff"
    assert len(rec.pushes()) == 1  # step 5 was not reached
    assert "incomplete at ff" in res.summary()


# (s)
def test_probe_incomplete_when_the_hook_is_lfs_only(tmp_path: Path) -> None:
    clone = _clone(tmp_path, LFS_ONLY)
    res = mod.probe(clone, tmp_parent=tmp_path)
    assert not res.passed
    name, ok, detail = res.steps[-1]
    assert (name, ok) == ("non-ff", False)
    assert "rc=0" in detail  # the non-FF push went through: no guard


# (t)
def test_probe_incomplete_when_update_ref_does_not_read_back(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    rec = Recorder()

    def tampered(args: Sequence[str], env: dict[str, str]) -> Any:
        proc = rec(args, env)
        if "--git-dir" in args and "rev-parse" in args and len(rec.pushes()) == 0:
            return subprocess.CompletedProcess(proc.args, 0, b"0" * 40 + b"\n", b"")
        return proc

    res = mod.probe(clone, run_git=tampered, tmp_parent=tmp_path)
    assert not res.passed
    assert [n for n, _ok, _d in res.steps] == ["transport", "probe-remote"]
    assert res.steps[-1][:2] == ("probe-remote", False)
    assert rec.pushes() == []


def test_probe_ignores_an_expectation_in_the_callers_env(tmp_path: Path, monkeypatch: Any) -> None:
    """MINDWIRE_PUSH_EXPECT in the operator's shell must not let the non-FF through."""
    monkeypatch.setenv("MINDWIRE_PUSH_EXPECT", "refs/heads/probe=" + "f" * 40)
    assert mod.probe(_clone(tmp_path), tmp_parent=tmp_path).passed


def test_probe_incomplete_when_clone_has_no_head(tmp_path: Path) -> None:
    clone = tmp_path / "empty"
    clone.mkdir()
    _git(clone, "init", "-q")
    res = mod.probe(clone, tmp_parent=tmp_path)
    assert not res.passed
    assert res.steps[-1][:2] == ("probe-remote", False)


def test_main_reports_p3_and_probe_and_exits_nonzero_on_failure(
    tmp_path: Path, capsys: Any
) -> None:
    clone = _clone(tmp_path, LFS_ONLY)
    rc = mod.main([str(clone)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "| clone | P3" in out
    assert "FAIL" in out  # P3: no pre-commit, no marker
    assert "incomplete at non-ff" in out


def test_main_without_args_is_usage(capsys: Any) -> None:
    assert mod.main([]) == 2
    assert "usage" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="documents the Windows stub resolution")
def test_stub_wins_path_resolution_on_this_host(tmp_path: Path) -> None:
    """The canary is what makes this safe; this records that it holds on Windows."""
    res = mod.probe(_clone(tmp_path), tmp_parent=tmp_path)
    canary = [d for n, _ok, d in res.steps if n == "canary"]
    assert canary and mod.STUB_TOKEN in canary[0]


# PR #587 gate round 1, Obj-1: no transport other than the local file one may start.
def _ssh_recorder(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point GIT_SSH_COMMAND at a script that leaves a mark if git ever starts ssh."""
    mark = tmp_path / "ssh-started"
    script = tmp_path / "fake-ssh.sh"
    script.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(mark.as_posix())}\nexit 1\n", encoding="utf-8", newline="\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", f"sh {shlex.quote(script.as_posix())}")
    return mark


@pytest.mark.parametrize("key", ["pushInsteadOf", "insteadOf"])
def test_probe_refuses_when_an_inherited_url_rewrite_is_in_force(
    tmp_path: Path, monkeypatch: Any, key: str
) -> None:
    """A global rule rewriting the temp path to ssh stops the probe before any transport."""
    clone = _clone(tmp_path)
    mark = _ssh_recorder(tmp_path, monkeypatch)
    glob = Path(os.environ["GIT_CONFIG_GLOBAL"])
    for prefix in {tmp_path.as_posix(), str(tmp_path)}:
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(glob),
                "--add",
                f"url.ssh://probe.example.invalid/x.{key}",
                prefix,
            ],
            check=True,
        )
    rec = Recorder()
    res = mod.probe(clone, run_git=rec, tmp_parent=tmp_path)
    assert not res.passed
    assert [n for n, _ok, _d in res.steps] == ["transport"]
    assert "probe.example.invalid" in res.steps[0][2]
    assert rec.pushes() == []
    assert not any("clone" in a for a in rec.calls)
    assert not mark.exists()


def test_probe_refuses_a_rewrite_injected_through_the_environment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    clone = _clone(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.ssh://probe.example.invalid/x.pushInsteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", tmp_path.as_posix())
    res = mod.probe(clone, tmp_parent=tmp_path)
    assert [n for n, _ok, _d in res.steps] == ["transport"]
    assert not res.passed


def test_file_only_transport_backstops_a_rewrite_the_check_missed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If a rewrite reached the clone / push anyway, GIT_ALLOW_PROTOCOL=file refuses ssh."""
    clone = _clone(tmp_path)
    mark = _ssh_recorder(tmp_path, monkeypatch)
    rewrite: list[str] = []
    for prefix in (tmp_path.as_posix(), str(tmp_path)):
        rewrite += ["-c", f"url.ssh://probe.example.invalid/x.pushInsteadOf={prefix}"]
        rewrite += ["-c", f"url.ssh://probe.example.invalid/x.insteadOf={prefix}"]
    rec = Recorder()

    def sneaky(args: Sequence[str], env: dict[str, str]) -> Any:
        if "push" in args or "clone" in args:
            assert env.get("GIT_ALLOW_PROTOCOL") == "file"
            return rec([*rewrite, *args], env)
        return rec(args, env)

    res = mod.probe(clone, run_git=sneaky, tmp_parent=tmp_path)
    assert not res.passed
    assert res.steps[-1][:2] == ("probe-remote", False)
    assert "not allowed" in res.steps[-1][2]
    assert not mark.exists()


# PR #587 gate round 1, Obj-2: the stub takes its recording path literally.
@pytest.mark.parametrize("name", ["a$cache", "b`touch pwned`", "c$(touch pwned)", "d'q", "e space"])
def test_probe_passes_under_a_temp_parent_with_shell_metacharacters(
    tmp_path: Path, monkeypatch: Any, name: str
) -> None:
    monkeypatch.setenv("cache", "elsewhere")
    parent = tmp_path / name
    parent.mkdir()
    clone = _clone(tmp_path)
    monkeypatch.chdir(tmp_path)
    res = mod.probe(clone, tmp_parent=parent)
    assert res.passed, res.steps
    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "elsewhere").exists()
    assert not list(parent.glob("mindwire-probe-*"))


def test_stub_text_quotes_the_record_dir_once() -> None:
    text = mod.stub_text(Path("/tmp/$x`y`/rec"))
    assert "R='/tmp/$x`y`/rec'\n" in text
    assert "/tmp/" not in text.split("\n", 2)[2]  # only the R= line names the path
