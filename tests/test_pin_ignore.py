"""``.mindwire/`` kept ignored in every clone; ``pin_not_ignored``; ``mindwire clone-check``.

Thread T-clone-guard-pin-ignored-only-in-mindwire, design v2.1 (Bohr msg-6162 work order +
msg-6164 delta, endorsed by Einstein msg-6165). Real git repositories in ``tmp_path``; an injected
runner / patched file API only for the failures a healthy git cannot be made to produce.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import cli, clone_guard, loop_runner
from spirrow_mindwire.clone_guard import (
    DIRTY_CLONE_EXIT_CODE,
    DIRTY_CLONE_PAYLOAD_PREFIX,
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    GitResult,
)
from spirrow_mindwire.pin_ignore import PIN_EXCLUDE_LINE, ensure_pin_ignored

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A clean clone on ``main`` whose ``.gitignore`` does NOT list ``.mindwire/`` (msg-6109)."""
    r = tmp_path / "clone"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _exclude(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def _write_pin(repo: Path) -> None:
    (repo / ".mindwire").mkdir(exist_ok=True)
    (repo / ".mindwire" / "pin").write_text("schema_version: 1\nmode: bootstrap\n", "utf-8")


def _lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- ensure_pin_ignored


def test_ensure_creates_a_missing_exclude_file_and_is_idempotent(bare_repo: Path) -> None:
    ex = _exclude(bare_repo)
    if ex.exists():
        ex.unlink()
    assert ensure_pin_ignored(bare_repo) is True
    assert ensure_pin_ignored(bare_repo) is False
    assert _lines(ex).count(PIN_EXCLUDE_LINE) == 1


def test_ensure_leaves_an_exclude_that_already_has_the_line_byte_identical(
    bare_repo: Path,
) -> None:
    ex = _exclude(bare_repo)
    ex.write_text("# local\n  /.mindwire/  \n*.log\n", encoding="utf-8")
    before = ex.read_bytes()
    assert ensure_pin_ignored(bare_repo) is False
    assert ex.read_bytes() == before


def test_ensure_appends_on_a_new_line_when_the_file_lacks_a_final_newline(
    bare_repo: Path,
) -> None:
    ex = _exclude(bare_repo)
    ex.write_text("*.log", encoding="utf-8")
    assert ensure_pin_ignored(bare_repo) is True
    assert ex.read_text(encoding="utf-8") == f"*.log\n{PIN_EXCLUDE_LINE}\n"
    assert ensure_pin_ignored(bare_repo) is False
    assert _lines(ex) == ["*.log", PIN_EXCLUDE_LINE]


def test_ensure_from_a_worktree_writes_the_shared_exclude(bare_repo: Path, tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    _git(bare_repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert ensure_pin_ignored(wt) is True
    assert ensure_pin_ignored(wt) is False
    assert PIN_EXCLUDE_LINE in _lines(_exclude(bare_repo))  # the common dir, not a per-worktree one
    _write_pin(wt)
    assert _git(wt, "status", "--porcelain=v1") == ""
    assert ensure_pin_ignored(bare_repo) is False


def test_pin_in_a_repo_without_gitignore_passes_after_ensure(bare_repo: Path) -> None:
    _write_pin(bare_repo)
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(bare_repo).check()
    assert ei.value.reason == DirtyCloneReason.PIN_NOT_IGNORED
    assert ei.value.detail == "exclude not effective"
    ensure_pin_ignored(bare_repo)
    CloneGuard(bare_repo).check()  # passes: the pin is ignored, the tree is clean


def test_guard_without_any_pin_still_requires_the_ignore(bare_repo: Path) -> None:
    # The check is on the path, not on the file: a clone that would dirty itself on the next pin
    # write is refused before that happens, under its own reason rather than as dirty_tree.
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(bare_repo).check()
    assert ei.value.reason == DirtyCloneReason.PIN_NOT_IGNORED


def test_tracked_pin_is_pin_not_ignored_with_its_own_detail(bare_repo: Path) -> None:
    _write_pin(bare_repo)
    _git(bare_repo, "add", ".mindwire/pin")
    _git(bare_repo, "commit", "-q", "-m", "oops")
    ensure_pin_ignored(bare_repo)  # an exclude line does not untrack a tracked file
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(bare_repo).check()
    assert ei.value.reason == DirtyCloneReason.PIN_NOT_IGNORED
    assert ei.value.detail == "pin is tracked in this repo"


def test_dirty_tree_still_reported_once_the_pin_is_ignored(bare_repo: Path) -> None:
    ensure_pin_ignored(bare_repo)
    _write_pin(bare_repo)
    (bare_repo / "u.txt").write_text("u\n", encoding="utf-8")
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(bare_repo).check()
    assert ei.value.reason == DirtyCloneReason.DIRTY_TREE
    assert ei.value.porcelain == ("?? u.txt",)


def test_ensure_git_failure_is_pin_not_ignored_carrying_the_git_error(tmp_path: Path) -> None:
    def runner(_r: Path, _a: Sequence[str]) -> GitResult:
        return GitResult(128, "", "fatal: not a git repository (or any parent)\nmore")

    with pytest.raises(DirtyCloneError) as ei:
        ensure_pin_ignored(tmp_path, git=runner)
    assert ei.value.reason == DirtyCloneReason.PIN_NOT_IGNORED
    assert ei.value.detail == (
        "git rev-parse --git-path info/exclude exit 128: "
        "fatal: not a git repository (or any parent)"
    )


def test_ensure_os_error_is_pin_not_ignored_carrying_the_os_error(
    bare_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(self: Path, *a: Any, **kw: Any) -> Any:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "open", _boom)
    with pytest.raises(DirtyCloneError) as ei:
        ensure_pin_ignored(bare_repo)
    assert ei.value.reason == DirtyCloneReason.PIN_NOT_IGNORED
    assert ei.value.detail.startswith("cannot update ")
    assert "PermissionError" in ei.value.detail and "Access is denied" in ei.value.detail
    assert isinstance(ei.value.__cause__, PermissionError)


# --------------------------------------------------------------------------- loop_runner.main


def test_pin_not_ignored_maps_to_the_dirty_clone_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from spirrow_mindwire.config import MindwireSettings

    async def _fake_run_conductor(_settings: MindwireSettings, **_kw: object) -> None:
        raise DirtyCloneError(
            repo_dir=tmp_path,
            reason=DirtyCloneReason.PIN_NOT_IGNORED,
            head=None,
            detail="cannot update x: OSError: disk full",
        )

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == DIRTY_CLONE_EXIT_CODE
    (row,) = [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith(DIRTY_CLONE_PAYLOAD_PREFIX)
    ]
    payload = json.loads(row[len(DIRTY_CLONE_PAYLOAD_PREFIX) :])
    assert payload["reason"] == "pin_not_ignored"
    assert "disk full" in payload["detail"]


# --------------------------------------------------------------------------- clone-check CLI


def test_clone_check_clean_exits_0_and_prints_nothing(
    bare_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pin(bare_repo)  # a pin in a repo without the ignore: clone-check's ensure fixes it
    with pytest.raises(SystemExit) as ei:
        cli.main(["clone-check", "--repo-dir", str(bare_repo)])
    assert ei.value.code == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_clone_check_dirty_exits_8_with_the_daemon_payload_row(
    bare_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(bare_repo, "switch", "-q", "-c", "feature/x")
    with pytest.raises(SystemExit) as ei:
        cli.main(["clone-check", "--repo-dir", str(bare_repo)])
    assert ei.value.code == DIRTY_CLONE_EXIT_CODE == 8
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(DIRTY_CLONE_PAYLOAD_PREFIX)
    payload = json.loads(lines[0][len(DIRTY_CLONE_PAYLOAD_PREFIX) :])
    assert payload == {
        "repo_dir": str(bare_repo),
        "reason": "wrong_head",
        "head": "feature/x",
        "porcelain": [],
        "detail": "HEAD is on 'feature/x', default branch is 'main'",
    }


def test_clone_check_unexpected_error_exits_1_with_a_one_line_summary(
    bare_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(self: CloneGuard) -> None:
        raise RuntimeError("boom\nsecond line")

    monkeypatch.setattr(CloneGuard, "check", _boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["clone-check", "--repo-dir", str(bare_repo)])
    assert ei.value.code == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.splitlines() == [
        f"clone-check: cannot judge {bare_repo}: RuntimeError: boom second line"
    ]


def test_clone_check_runs_as_a_module(bare_repo: Path) -> None:
    # The sweep wrapper runs `python -m spirrow_mindwire.cli clone-check` (Invoke-CloneCheck).
    _git(bare_repo, "switch", "-q", "-c", "feature/y")
    proc = subprocess.run(
        [sys.executable, "-m", "spirrow_mindwire.cli", "clone-check", "--repo-dir", str(bare_repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 8
    assert proc.stdout.startswith(DIRTY_CLONE_PAYLOAD_PREFIX)


def test_daemon_and_clone_check_share_one_payload_writer() -> None:
    # msg-6164 D-3c: one function, two callers — the wrapper parses one format.
    assert vars(loop_runner)["emit_dirty_clone_payload"] is clone_guard.emit_dirty_clone_payload
    assert not hasattr(loop_runner, "_emit_dirty_clone_payload")
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert "emit_dirty_clone_payload(exc)" in src
    assert "DIRTY_CLONE_PAYLOAD_PREFIX" not in src  # no second writer of the row


# ------------------------------------------------------------------------- head_skip revert-launch


def _load_head_skip_cli() -> Any:
    path = _REPO_ROOT / "scripts" / "head_skip_decide.py"
    spec = importlib.util.spec_from_file_location("_head_skip_cli_revert_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HS = _load_head_skip_cli()
_PAYLOAD = {
    "thread_id": "T-a",
    "head_msg_id": "msg-10",
    "token": "einstein",
    "token_raw": "Einstein",
    "attempts_after": 1,
    "control_state": "run",
    "head_fetched": True,
}


def _commit(state: Path, now: datetime, capsys: pytest.CaptureFixture[str]) -> str:
    args = _HS.argparse.Namespace(
        payload=json.dumps(_PAYLOAD),
        payload_file=None,
        state_file=str(state),
        now_iso=now.isoformat(),
    )
    assert _HS._main_commit_launch(args) == 0
    return capsys.readouterr().out.strip()


def _revert(state: Path, output: str) -> int:
    args = _HS.argparse.Namespace(payload=output, payload_file=None, state_file=str(state))
    return int(_HS._main_revert_launch(args))


def test_revert_launch_restores_the_prior_record_and_three_refusals_never_reach_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "head_skip.json"
    t = datetime(2026, 10, 3, tzinfo=UTC)
    first = json.loads(_commit(state, t, capsys))
    assert first["prior_record"] is None
    assert first["record"]["launches_same_head"] == 1
    snapshot = state.read_text(encoding="utf-8")
    # Three exit-8 runs on the same head: each commit is reverted.
    for _ in range(3):
        out = _commit(state, t, capsys)
        assert json.loads(out)["record"]["launches_same_head"] == 2
        assert _revert(state, out) == 0
        capsys.readouterr()
        assert state.read_text(encoding="utf-8") == snapshot
    # The clone is clean again: the next real launch counts 2, never 4 (T42 fires at 3).
    assert json.loads(_commit(state, t, capsys))["record"]["launches_same_head"] == 2


def test_revert_launch_of_a_first_ever_launch_removes_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "head_skip.json"
    out = _commit(state, datetime(2026, 10, 3, tzinfo=UTC), capsys)
    assert _revert(state, out) == 0
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    # ...so the clean relaunch starts at launches_same_head = 1.
    capsys.readouterr()
    again = _commit(state, datetime(2026, 10, 3, tzinfo=UTC), capsys)
    assert json.loads(again)["record"]["launches_same_head"] == 1


def test_revert_launch_refuses_when_the_record_moved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "head_skip.json"
    t = datetime(2026, 10, 3, tzinfo=UTC)
    out = _commit(state, t, capsys)
    _commit(state, t, capsys)  # a second commit moved the record past the one we would revert
    moved = state.read_text(encoding="utf-8")
    assert _revert(state, out) == 1
    assert "revert-launch failed" in capsys.readouterr().err
    assert state.read_text(encoding="utf-8") == moved
