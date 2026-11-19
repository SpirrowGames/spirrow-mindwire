"""``mindwire slot-prep`` / ``slot-lease`` (T-ephemeral-impl-clone-slot-per-thread A2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from spirrow_mindwire import clone_slot_cli
from spirrow_mindwire.cli import main
from spirrow_mindwire.clone_retreat import git_env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=git_env()
    ).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("a\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def _prep_argv(tmp_path: Path, origin: Path, thread: str, turn: str) -> list[str]:
    return [
        "slot-prep",
        "--slots-root", str(tmp_path / "slots"),
        "--repo", "sample",
        "--origin-url", str(origin),
        "--state-dir", str(tmp_path / "state"),
        "--slots-per-repo", "1",
        "--thread-id", thread,
        "--turn-id", turn,
        "--branch", f"feature/{thread}",
        "--base", "main",
        "--wrapper-pid", "1",
    ]  # fmt: skip


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out = capsys.readouterr()
    return int(exc.value.code or 0), out.out, out.err


def _row(out: str) -> dict[str, object]:
    [line] = [ln for ln in out.splitlines() if ln.startswith(clone_slot_cli.PREP_PREFIX)]
    return json.loads(line.removeprefix(clone_slot_cli.PREP_PREFIX))  # type: ignore[no-any-return]


def test_prep_ready_waiting_then_lease_moves(
    tmp_path: Path,
    origin: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clone_slot_cli, "_trusted", lambda p: True)
    code, out, _ = _run(_prep_argv(tmp_path, origin, "T-a", "t1"), capsys)
    assert code == 0 and _row(out)["slot_name"] == "slot-01"
    code, out, _ = _run(_prep_argv(tmp_path, origin, "T-b", "t2"), capsys)
    assert code == 3 and _row(out)["waiting_reason"] == "no_free_slot"

    base = ["--state-dir", str(tmp_path / "state"), "--repo", "sample", "--slot", "slot-01"]
    assert _run(["slot-lease", "spawning", *base, "--turn-id", "t1"], capsys)[0] == 0
    assert _run(["slot-lease", "running", *base, "--turn-id", "t1"], capsys)[0] == 1
    assert (
        _run(["slot-lease", "running", *base, "--turn-id", "t1", "--child-pid", "7"], capsys)[0]
        == 0
    )
    assert _run(["slot-lease", "release", *base, "--turn-id", "wrong"], capsys)[0] == 1
    assert _run(["slot-lease", "release", *base, "--turn-id", "t1"], capsys)[0] == 0
    code, out, _ = _run(_prep_argv(tmp_path, origin, "T-b", "t3"), capsys)
    assert code == 0


def test_quarantine_is_recorded_with_recoverable(
    tmp_path: Path,
    origin: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clone_slot_cli, "_trusted", lambda p: True)
    assert _run(_prep_argv(tmp_path, origin, "T-a", "t1"), capsys)[0] == 0
    slot = tmp_path / "slots" / "sample" / "slot-01"
    (slot / "wip.txt").write_text("w\n", encoding="utf-8")
    base = ["--state-dir", str(tmp_path / "state"), "--repo", "sample", "--slot", "slot-01"]
    assert _run(["slot-lease", "release", *base, "--turn-id", "t1"], capsys)[0] == 0
    code, out, _ = _run(_prep_argv(tmp_path, origin, "T-b", "t2"), capsys)
    assert code == 0
    assert _row(out)["quarantine"]["recoverable"] is True  # type: ignore[index]
    [line] = (tmp_path / "state" / "clone-retreats.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(line)
    assert row["kind"] == "slot_quarantine" and row["thread_id"] == "T-a"
    assert row["quarantine_reason"] == "untracked_only" and row["action"] == "rebuilt"
    assert row["recoverable"] is True and row["rescue"]["outcome"] == "rescued"
    assert row["rescue"]["refs"][0]["ref"] == "refs/heads/rescue/T-a/t2/01"


def test_untrusted_parks_with_exit_4(
    tmp_path: Path,
    origin: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clone_slot_cli, "_trusted", lambda p: False)
    code, out, _ = _run(_prep_argv(tmp_path, origin, "T-a", "t1"), capsys)
    assert code == 4 and _row(out)["park_code"] == "workspace-untrusted"


def test_prep_failure_exits_1_with_reason(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "slot-leases.json").write_text("not json", encoding="utf-8")
    code, out, err = _run(_prep_argv(tmp_path, origin, "T-a", "t1"), capsys)
    assert code == 1 and "slot-prep failed" in err and not out.strip()
