"""P3 — the secret guard is armed in ``repo_dir`` (R-3' of
T-secret-guard-unarmed-in-every-implementer-clone, msg-5342 §4).

Every autonomous ``*-impl`` clone ran with ``core.hooksPath`` unset while the
operator's clone was armed (msg-2864): ``core.hooksPath`` is per-clone and is
not carried by ``git clone``. P3 makes "armed" a dispatch precondition. These
tests build real throwaway git repos under ``tmp_path`` and read them through
the production reader (``git config --get --path core.hooksPath``), so a
change that stops P3 from going red on an unarmed clone reds this file.

Deliberately NOT here (msg-5342 §4): no test enumerates the real ``*-impl``
clones. The dispatch set changes over time (playproof-impl already left it),
and P3 itself checks the real clone on every dispatch.

Global / system git config is isolated per test (``GIT_CONFIG_GLOBAL`` →
empty file, ``GIT_CONFIG_NOSYSTEM=1``): a developer machine with a global
``core.hooksPath`` would otherwise make the "unset" case pass vacuously.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import preflight as preflight_mod
from spirrow_mindwire.preflight import PreflightError, preflight_gate

_real_reader = preflight_mod._default_hooks_path_reader
_real_toplevel = preflight_mod._default_git_toplevel


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path: Path, monkeypatch: Any) -> None:
    empty = tmp_path / "empty-global.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _p3(repo: Path) -> None:
    preflight_mod._p3_secret_guard_armed(repo, _real_reader, _real_toplevel)


# --------------------------------------------------------------------------- #
# red cases — each is a shape an unarmed clone can actually take
# --------------------------------------------------------------------------- #


def test_p3_unset_hooks_path_halts(tmp_path: Path) -> None:
    """(1) The measured state of every *-impl clone before R-1: core.hooksPath unset."""
    repo = _repo(tmp_path)
    with pytest.raises(PreflightError) as exc:
        _p3(repo)
    msg = str(exc.value)
    assert "P3" in msg
    assert "core.hooksPath" in msg
    assert "install_githooks.sh" in msg  # the message names the repair


def test_p3_hooks_dir_with_only_lfs_pre_push_halts(tmp_path: Path) -> None:
    """(2) The shared githooks/ also holds git-lfs hooks (msg-5342 §3(a)).

    A hooks dir carrying pre-push / post-* but no pre-commit is NOT armed:
    directory existence must not stand in for the named hook.
    """
    repo = _repo(tmp_path)
    hooks = tmp_path / "githooks"
    hooks.mkdir()
    for name in ("pre-push", "post-checkout", "post-commit", "post-merge"):
        (hooks / name).write_text("#!/bin/sh\ngit lfs\n", encoding="utf-8")
    _git(repo, "config", "core.hooksPath", str(hooks))
    with pytest.raises(PreflightError) as exc:
        _p3(repo)
    assert "pre-commit" in str(exc.value)


def test_p3_pre_commit_that_is_a_directory_halts(tmp_path: Path) -> None:
    """(3) A directory named pre-commit is not a hook."""
    repo = _repo(tmp_path)
    hooks = tmp_path / "githooks"
    (hooks / "pre-commit").mkdir(parents=True)
    _git(repo, "config", "core.hooksPath", str(hooks))
    with pytest.raises(PreflightError):
        _p3(repo)


def test_p3_hooks_path_pointing_at_missing_dir_halts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "core.hooksPath", str(tmp_path / "does-not-exist"))
    with pytest.raises(PreflightError):
        _p3(repo)


def test_p3_empty_hooks_path_value_halts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "core.hooksPath", "")
    with pytest.raises(PreflightError):
        _p3(repo)


def test_p3_reader_exception_fails_closed(tmp_path: Path) -> None:
    """(6) An unreadable arming state is unarmed, never a pass."""

    def _boom(_repo: Path) -> str | None:
        raise RuntimeError("git config exploded")

    with pytest.raises(PreflightError) as exc:
        preflight_mod._p3_secret_guard_armed(tmp_path, _boom, _real_toplevel)
    assert "git config exploded" in str(exc.value)


def test_p3_relative_hooks_path_without_toplevel_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PreflightError):
        preflight_mod._p3_secret_guard_armed(tmp_path, lambda _r: "githooks", lambda _p: None)


# --------------------------------------------------------------------------- #
# green cases
# --------------------------------------------------------------------------- #


def test_p3_relative_hooks_path_resolves_against_toplevel(tmp_path: Path) -> None:
    """(4) git runs hooks from the toplevel, so a relative value anchors there —
    even when repo_dir names a subdirectory of the working tree."""
    repo = _repo(tmp_path)
    (repo / "githooks").mkdir()
    (repo / "githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(repo, "config", "core.hooksPath", "githooks")
    _p3(repo)
    sub = repo / "nested" / "dir"
    sub.mkdir(parents=True)
    _p3(sub)


def test_p3_armed_clone_passes(tmp_path: Path) -> None:
    """(5) The post-R-1 shape: absolute core.hooksPath to a dir holding pre-commit
    alongside the git-lfs hooks."""
    repo = _repo(tmp_path)
    hooks = tmp_path / "githooks"
    hooks.mkdir()
    for name in ("pre-commit", "pre-push", "post-checkout", "post-commit", "post-merge"):
        (hooks / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(repo, "config", "core.hooksPath", str(hooks))
    _p3(repo)


def test_p3_default_reader_returns_none_when_unset(tmp_path: Path) -> None:
    assert _real_reader(_repo(tmp_path)) is None


def test_p3_default_reader_raises_on_non_unset_git_failure(tmp_path: Path) -> None:
    # git -C <missing dir> exits 128, not 1: that must raise, never read as "unset".
    with pytest.raises(subprocess.CalledProcessError) as ei:
        _real_reader(tmp_path / "no-such-dir")
    assert ei.value.returncode not in (0, 1)


def test_p3_halts_when_default_reader_fails(tmp_path: Path) -> None:
    # The real reader's raise reaches P3 and fails closed (no mocked exception).
    with pytest.raises(PreflightError, match="P3"):
        preflight_mod._p3_secret_guard_armed(
            tmp_path / "no-such-dir", _real_reader, lambda _p: None
        )


# --------------------------------------------------------------------------- #
# wiring — P3 is part of preflight_gate, after P0 and before P2/P1
# --------------------------------------------------------------------------- #


def _must_not_reach(_x: Any) -> Any:
    raise AssertionError("P2/P1 reached although P3 should have halted first")


def test_preflight_gate_halts_on_unarmed_clone_before_p2_p1(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    daemon = tmp_path / "daemon"
    daemon.mkdir()
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            repo,
            daemon_root=daemon,
            remote_reader=_must_not_reach,
            api_caller=_must_not_reach,
        )
    assert "P3" in str(exc.value)


def test_preflight_gate_proceeds_to_p2_once_armed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    hooks = tmp_path / "githooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(repo, "config", "core.hooksPath", str(hooks))
    daemon = tmp_path / "daemon"
    daemon.mkdir()
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            repo,
            daemon_root=daemon,
            remote_reader=lambda _r: [],  # P2: no remotes → halts, proving P3 passed
            api_caller=_must_not_reach,
        )
    assert "P2" in str(exc.value)
