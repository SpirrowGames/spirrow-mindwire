"""``mindwire slot-prep`` (clone_slot_cli): exit codes, payload row, mirror cadence, sessions.

Thread T-ephemeral-impl-clone-slot-per-thread, PR-A wiring (msg-8229 / msg-8231). Real git
repositories under ``tmp_path``; the procedure itself is covered by ``test_clone_slot_prep``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import cli
from spirrow_mindwire.clone_slot import PARK_OWNER_UNKNOWN, choose_slot, mirror_path
from spirrow_mindwire.clone_slot_cli import (
    EXIT_FAILED,
    EXIT_LAUNCH,
    EXIT_PARK,
    EXIT_SKIP,
    SLOT_PREP_PAYLOAD_PREFIX,
    claude_sessions_under,
    count_sessions_under,
    run,
)
from spirrow_mindwire.clone_slot_prep import SessionsUnknown, SlotPrep

REPO = "demo"
THREAD = "T-slot-cli-demo"
TB = "loop/t-slot-cli-demo"


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    _git(seed, "switch", "-q", "-c", "main")
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", "origin", "main")
    return {"origin": origin, "seed": seed, "slots": tmp_path / "slots", "state": tmp_path / "st"}


def _prep(world: dict[str, Path], **kw: Any) -> SlotPrep:
    args: dict[str, Any] = {
        "slots_root": world["slots"],
        "repo": REPO,
        "thread_id": THREAD,
        "origin_url": str(world["origin"]),
        "thread_branch": TB,
        "default_branch": "main",
        "state_dir": world["state"],
        "sessions_under": lambda _p: 0,
    }
    args.update(kw)
    return SlotPrep(**args)


def _payload(out: str) -> dict[str, Any]:
    rows = [ln for ln in out.splitlines() if ln.startswith(SLOT_PREP_PAYLOAD_PREFIX + " ")]
    assert len(rows) == 1, out
    data = json.loads(rows[0][len(SLOT_PREP_PAYLOAD_PREFIX) + 1 :])
    assert isinstance(data, dict)
    return data


def test_first_run_creates_mirror_and_launches_with_slot_dir(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(_prep(world)) == EXIT_LAUNCH  # no --refresh-mirror: a missing mirror is created
    p = _payload(capsys.readouterr().out)
    assert p["action"] == "launch" and p["park_code"] is None
    slot = Path(p["slot_dir"])
    assert slot.is_dir() and _git(slot, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert mirror_path(world["slots"], REPO).is_dir()
    assert p["mirror_refreshed"] is True and isinstance(p["mirror_seconds"], float)
    assert isinstance(p["prep_seconds"], float)


def test_existing_mirror_is_not_fetched_without_the_flag(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(_prep(world)) == EXIT_LAUNCH
    capsys.readouterr()
    assert run(_prep(world)) == EXIT_LAUNCH
    p = _payload(capsys.readouterr().out)
    assert p["mirror_refreshed"] is None and p["mirror_seconds"] is None


def test_refresh_flag_fetches_new_commits_into_the_mirror(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(_prep(world)) == EXIT_LAUNCH
    capsys.readouterr()
    (world["seed"] / "b.txt").write_text("b\n", encoding="utf-8")
    _git(world["seed"], "add", "b.txt")
    _git(world["seed"], "commit", "-q", "-m", "b")
    _git(world["seed"], "push", "-q", "origin", "main")
    tip = _git(world["seed"], "rev-parse", "HEAD")
    assert run(_prep(world), refresh=True) == EXIT_LAUNCH
    p = _payload(capsys.readouterr().out)
    assert p["mirror_refreshed"] is True
    assert _git(mirror_path(world["slots"], REPO), "rev-parse", "refs/heads/main") == tip


def test_failed_fetch_keeps_old_mirror_and_still_decides(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(_prep(world)) == EXIT_LAUNCH
    capsys.readouterr()
    bad = _prep(world)
    _git(mirror_path(world["slots"], REPO), "remote", "set-url", "origin", str(world["seed"] / "x"))
    code = run(bad, refresh=True)
    p = _payload(capsys.readouterr().out)
    assert p["mirror_refreshed"] is False  # msg-8061: a failed mirror fetch parks nobody
    assert code in (EXIT_LAUNCH, EXIT_SKIP) and p["action"] != "park"


def test_park_maps_to_exit_4_with_park_code(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    paths = choose_slot(world["slots"], REPO, THREAD).paths
    paths.repo_root.mkdir(parents=True, exist_ok=True)
    paths.owner_file.write_text('{"repo": ', encoding="utf-8")  # unreadable owner file
    assert run(_prep(world)) == EXIT_PARK
    p = _payload(capsys.readouterr().out)
    assert p["action"] == "park" and p["park_code"] == PARK_OWNER_UNKNOWN


def test_raising_procedure_is_exit_1_without_payload(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_p: Path) -> int:
        raise RuntimeError("agent list\nexploded")

    prep = _prep(world)
    object.__setattr__(prep, "run", lambda: boom(Path()))
    assert run(prep) == EXIT_FAILED
    out = capsys.readouterr()
    assert SLOT_PREP_PAYLOAD_PREFIX not in out.out
    assert "slot-prep: cannot prepare demo/T-slot-cli-demo: RuntimeError: agent list exploded" in (
        out.err
    )


def test_count_sessions_under_matches_slot_and_children_only(tmp_path: Path) -> None:
    slot = tmp_path / "demo" / "abc"
    agents = [
        {"id": "a1", "cwd": str(slot)},
        {"id": "a2", "cwd": str(slot / "sub")},
        {"id": "a3", "cwd": str(tmp_path / "demo" / "abcd")},  # sibling prefix, not under
        {"id": "a4", "cwd": None},
        {"cwd": str(slot)},  # interactive (no id): dropped by parse_agent_list
    ]
    assert count_sessions_under(slot, json.dumps(agents)) == 2


def test_unreadable_agent_list_counts_as_a_live_session(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-claude.exe")
    got = claude_sessions_under(missing)(tmp_path)
    assert isinstance(got, SessionsUnknown) and got.detail


def test_cli_slot_prep_end_to_end(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import spirrow_mindwire.clone_slot_cli as mod

    monkeypatch.setattr(mod, "claude_sessions_under", lambda: lambda _p: 0)
    argv = [
        "slot-prep",
        "--slots-root",
        str(world["slots"]),
        "--repo",
        REPO,
        "--thread-id",
        THREAD,
        "--thread-branch",
        TB,
        "--origin-url",
        str(world["origin"]),
        "--state-dir",
        str(world["state"]),
        "--refresh-mirror",
    ]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == EXIT_LAUNCH
    assert _payload(capsys.readouterr().out)["action"] == "launch"


def test_wrapper_crash_park_code_matches_clone_slot_constant() -> None:
    """The wrapper parks a crashed slot-prep with clone_slot.PARK_PREP_CRASHED (msg-8236)."""
    from spirrow_mindwire.clone_slot import PARK_PREP_CRASHED

    root = Path(__file__).resolve().parents[1]
    text = (root / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    assert f"$SlotPrepCrashedParkCode = '{PARK_PREP_CRASHED}'" in text


def test_sweep_task_registration_spec_keeps_ignore_new() -> None:
    """msg-8246 premise: the slot check-to-rename window is safe because sweep ticks never overlap.

    There is no registration script for the sweep task; docs/deploy.md's settings table is the
    registration spec, so it is what this pins. It cannot see the live task: the operational
    premise (one registered task, IgnoreNew, no direct runs of the wrapper while slots are in
    use) is recorded in the PR read-back (Einstein advisory after msg-8246).
    """
    root = Path(__file__).resolve().parents[1]
    deploy = (root / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "| `MultipleInstancesPolicy` | `IgnoreNew` |" in deploy
    wrapper = (root / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    assert "MultipleInstancesPolicy=IgnoreNew" in wrapper
