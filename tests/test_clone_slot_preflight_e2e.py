"""A borrowed slot passes every preflight check (T-ephemeral-impl-clone-slot-per-thread A8).

Production found the slot's preflight failures one at a time, each on the first role turn after a
re-enable: ``check=config`` (msg-10049, fixed by A7) and then ``check=P3`` (msg-10086). Bohr's
msg-10088 traced a third, P2, behind P3: every slot had a ``mirror`` remote with a local URL. These
tests join the real :meth:`SlotPrep.borrow` to the real :func:`preflight_gate`, with real git and
the real remote / hooks / toplevel readers; only GitHub's API answers (P1) are stubbed.

The upstream is a local bare repo, and the bare mirror is made from it before the first borrow, so
nothing is fetched from GitHub: ``origin`` carries the GitHub URL P2 asks for, and is only ever
added as a remote. A quarantine here is always ``provision_failed`` (a slot that is not a
repository), which pushes nothing; a rescue would push to that GitHub URL.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import make_guard_hooks

from spirrow_mindwire.clone_guard import CloneGuard
from spirrow_mindwire.clone_retreat import git_env
from spirrow_mindwire.clone_slot import PARK_PROVISION_FAILED, mirror_path
from spirrow_mindwire.clone_slot_lease import LeaseStore
from spirrow_mindwire.clone_slot_prep import PrepOutcome, PrepResult, SlotPrep
from spirrow_mindwire.pin_ignore import ensure_pin_ignored
from spirrow_mindwire.preflight import PreflightError, preflight_gate

REPO = "sample"
ORIGIN_URL = f"https://github.com/SpirrowGames/{REPO}.git"
NOW = datetime(2026, 11, 27, 1, 2, 3, tzinfo=UTC)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=git_env()
    ).stdout


def _healthy_api() -> dict[str, Any]:
    """P1's answers for a default branch under the org ruleset (as in tests/test_preflight.py)."""
    rule = {
        "ruleset_id": 1,
        "ruleset_source_type": "Organization",
        "ruleset_source": "SpirrowGames",
    }
    return {
        f"repos/SpirrowGames/{REPO}": {"default_branch": "main"},
        f"repos/SpirrowGames/{REPO}/rules/branches/main": [
            {"type": t, **rule} for t in ("deletion", "non_fast_forward", "pull_request")
        ],
        f"repos/SpirrowGames/{REPO}/rulesets/1": {
            "id": 1,
            "name": "guard-default-branch",
            "source_type": "Organization",
            "source": "SpirrowGames",
            "enforcement": "active",
            "current_user_can_bypass": "never",
        },
    }


def _api(endpoint: str, timeout_s: float = 30.0) -> Any:
    answers = _healthy_api()
    if endpoint not in answers:
        raise RuntimeError(f"no stub for {endpoint}")
    return answers[endpoint]


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A local bare repo standing in for GitHub, and the bare mirror made from it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    mirror = mirror_path(tmp_path / "slots", REPO)
    mirror.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--mirror", str(bare), str(mirror)], check=True)
    return bare


def _prep(tmp_path: Path, hooks_path: str) -> SlotPrep:
    return SlotPrep(
        slots_root=tmp_path / "slots",
        repo=REPO,
        origin_url=ORIGIN_URL,
        hooks_path=hooks_path,
        store=LeaseStore(tmp_path / "state", alive=lambda pid: False),
        slots_per_repo=1,
        clock=lambda: NOW,
    )


def _borrow(prep: SlotPrep, thread: str, turn: str) -> PrepResult:
    return prep.borrow(
        thread_id=thread, turn_id=turn, branch=f"loop/{thread}", base="main", wrapper_pid=1
    )


def _preflight(slot: Path, tmp_path: Path) -> list[str]:
    daemon = tmp_path / "daemon"
    daemon.mkdir(exist_ok=True)
    return preflight_gate(slot, daemon_root=daemon, api_caller=_api)


def _assert_launchable(slot: Path, tmp_path: Path) -> None:
    """Every check run against ``[loop].repo_dir`` before a role starts, in order: preflight
    P0/P3/P2/P1, then the dispatcher's ``ensure_pin_ignored`` and clone guard (slot mode)."""
    assert _preflight(slot, tmp_path) == [f"SpirrowGames/{REPO}"]
    ensure_pin_ignored(slot)
    CloneGuard(slot, slot_mode=True).check()


def test_new_slot_passes_every_preflight_check(
    tmp_path: Path, upstream: Path, slot_hooks: Path
) -> None:
    res = _borrow(_prep(tmp_path, str(slot_hooks)), "T-a", "t1")
    assert res.outcome is PrepOutcome.READY, res.detail
    slot = res.slot.path  # type: ignore[union-attr]
    assert res.provisioned
    assert _git(slot, "config", "--local", "--get", "core.hooksPath").strip() == str(slot_hooks)
    assert _git(slot, "remote", "-v").split() == [
        "origin", ORIGIN_URL, "(fetch)", "origin", ORIGIN_URL, "(push)"
    ]  # fmt: skip
    assert not _git(slot, "config", "--local", "--get-regexp", r"^branch\..*\.remote$", check=False)
    assert _git(slot, "rev-parse", "mirror/main") == _git(upstream, "rev-parse", "main")
    _assert_launchable(slot, tmp_path)


def _pre_a8_slot(tmp_path: Path) -> Path:
    """A slot made the way the code before A8 made one (the 9 slots in production, msg-10086):
    ``clone --origin mirror``, ``origin`` added, switched to a thread branch; no core.hooksPath."""
    slot = tmp_path / "slots" / REPO / "slot-01"
    mirror = mirror_path(tmp_path / "slots", REPO)
    slot.parent.mkdir(parents=True)
    _git(slot.parent, "clone", "-q", "--no-checkout", "--origin", "mirror", str(mirror), slot.name)
    _git(slot, "remote", "add", "origin", ORIGIN_URL)
    _git(
        slot, "fetch", "-q", "--prune", "--no-tags", "mirror", "+refs/heads/*:refs/remotes/mirror/*"
    )
    _git(slot, "switch", "-q", "--no-track", "-C", "loop/T-old", "mirror/main")
    return slot


def test_slot_made_before_a8_fails_p3_then_p2_and_one_borrow_fixes_both(
    tmp_path: Path, upstream: Path, slot_hooks: Path
) -> None:
    slot = _pre_a8_slot(tmp_path)
    assert "mirror" in _git(slot, "remote").split()
    assert _git(slot, "config", "--get-regexp", r"^branch\..*\.remote$").split() == [
        "branch.main.remote", "mirror"
    ]  # fmt: skip

    # What production saw (msg-10086): P3, because nothing set core.hooksPath.
    with pytest.raises(PreflightError) as p3:
        _preflight(slot, tmp_path)
    assert p3.value.check == "P3" and "has no core.hooksPath set" in str(p3.value)

    # With the hooks armed and nothing else, P2 refuses the mirror remote (msg-10088): the reason
    # this PR removes the remote rather than arming the hooks only.
    _git(slot, "config", "--local", "core.hooksPath", str(slot_hooks))
    with pytest.raises(PreflightError) as p2:
        _preflight(slot, tmp_path)
    assert p2.value.check == "P2" and "mirror" in str(p2.value)

    # A stale or different hooks path is replaced too; one borrow fixes the slot in place.
    _git(slot, "config", "--local", "core.hooksPath", str(tmp_path / "elsewhere"))
    res = _borrow(_prep(tmp_path, str(slot_hooks)), "T-b", "t2")
    assert res.outcome is PrepOutcome.READY, res.detail
    assert not res.provisioned and res.quarantine is None
    assert res.slot.path == slot  # type: ignore[union-attr]
    assert _git(slot, "remote").split() == ["origin"]
    assert not _git(slot, "config", "--get-regexp", r"^branch\..*\.remote$", check=False)
    _assert_launchable(slot, tmp_path)


def test_quarantined_and_rebuilt_slot_passes_every_preflight_check(
    tmp_path: Path, upstream: Path, slot_hooks: Path
) -> None:
    prep = _prep(tmp_path, str(slot_hooks))
    first = _borrow(prep, "T-a", "t1")
    assert first.outcome is PrepOutcome.READY, first.detail
    slot = first.slot.path  # type: ignore[union-attr]
    prep.store.release(REPO, "slot-01", "t1")
    # Break the slot so the borrow-time check quarantines it: no longer a repository.
    os.rename(slot, tmp_path / "moved-away")
    slot.mkdir()
    (slot / "stray.txt").write_text("x\n", encoding="utf-8")

    res = _borrow(prep, "T-b", "t2")
    assert res.outcome is PrepOutcome.READY, res.detail
    assert res.quarantine is not None and res.quarantine.reason == "provision_failed"
    assert res.quarantine.quarantine_path is not None
    assert res.provisioned and res.slot.path == slot  # type: ignore[union-attr]
    _assert_launchable(slot, tmp_path)


def _hooks_inside_slots_root(tmp_path: Path) -> str:
    return str(make_guard_hooks(tmp_path / "slots" / "hooks"))


def _hooks_without_pre_commit(tmp_path: Path) -> str:
    hooks = make_guard_hooks(tmp_path / "lfs-only")
    (hooks / "pre-commit").unlink()
    return str(hooks)


@pytest.mark.parametrize(
    ("make", "words"),
    [
        (lambda tmp: "", "no core.hooksPath"),
        (lambda tmp: "   ", "no core.hooksPath"),
        (lambda tmp: "githooks", "is relative"),
        (_hooks_inside_slots_root, "inside slots_root"),
        (_hooks_without_pre_commit, "no pre-commit file"),
    ],
    ids=["empty", "blank", "relative", "inside-slots-root", "no-pre-commit"],
)
def test_unusable_hooks_path_parks_before_anything_runs(
    tmp_path: Path, upstream: Path, slot_hooks: Path, make: Any, words: str
) -> None:
    prep = _prep(tmp_path, make(tmp_path))
    res = _borrow(prep, "T-a", "t1")
    assert res.outcome is PrepOutcome.PARKED
    assert res.park_code == PARK_PROVISION_FAILED and words in res.detail
    assert not (tmp_path / "slots" / REPO / "slot-01").exists()  # refused before the clone
    # The lease went back: the one slot can be borrowed with a usable hooks path.
    prep.hooks_path = str(slot_hooks)
    again = _borrow(prep, "T-a", "t2")
    assert again.outcome is PrepOutcome.READY, again.detail
