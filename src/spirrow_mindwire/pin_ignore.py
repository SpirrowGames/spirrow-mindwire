"""Keep ``.mindwire/`` git-ignored in every clone the dispatcher writes a pin into.

Thread T-clone-guard-pin-ignored-only-in-mindwire, design v2.1 D-1 (Bohr msg-6162 / msg-6164,
endorsed by Einstein msg-6165). The dispatcher writes ``.mindwire/pin`` into ``[loop].repo_dir``
on every implementer / naysayer dispatch, but only the spirrow-mindwire repo carries
``.mindwire/`` in its ``.gitignore``. In every other shared clone the pin showed up as
``?? .mindwire/`` and the pre-dispatch clone guard refused the clone as ``dirty_tree`` forever.

:func:`ensure_pin_ignored` appends ``/.mindwire/`` to the clone's *local* exclude file
(``git rev-parse --git-path info/exclude``: per-clone, never committed, shared by worktrees) unless
a line with that text is already there. It is a separate component from
:class:`~spirrow_mindwire.clone_guard.CloneGuard`, which stays read-only: this touches no working
tree, no index, no ref — one reversible line in ``info/exclude`` that ignores mindwire's own
artefact. The whole directory is ignored (not just ``pin``) so a temp file left by the pin
writer's atomic write is ignored too.

Failure raises :class:`~spirrow_mindwire.clone_guard.DirtyCloneError` with reason
``pin_not_ignored`` (exit 8 through ``loop_runner.main``), carrying the underlying git / OS error
in ``detail`` (Einstein msg-6163 objection 2).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .clone_guard import (
    DirtyCloneError,
    DirtyCloneReason,
    GitRunner,
    _subprocess_git,
)

PIN_EXCLUDE_LINE = "/.mindwire/"


def _error(repo_dir: Path, detail: str) -> DirtyCloneError:
    return DirtyCloneError(
        repo_dir=repo_dir, reason=DirtyCloneReason.PIN_NOT_IGNORED, head=None, detail=detail
    )


def exclude_path(repo_dir: Path, *, git: GitRunner = _subprocess_git) -> Path:
    """The clone's ``info/exclude`` path as git resolves it (worktree-safe)."""
    args = ["rev-parse", "--git-path", "info/exclude"]
    cmd = f"git {' '.join(args)}"
    try:
        res = git(repo_dir, args)
    except subprocess.TimeoutExpired:
        raise _error(repo_dir, f"{cmd} timed out") from None
    except OSError as exc:
        raise _error(repo_dir, f"{cmd} failed to run: {type(exc).__name__}: {exc}") from exc
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip().splitlines()
        raise _error(repo_dir, f"{cmd} exit {res.returncode}: {err[0] if err else '-'}")
    raw = res.stdout.strip()
    if not raw:
        raise _error(repo_dir, f"{cmd} printed no path")
    path = Path(raw)
    # --git-path answers relative to the directory git ran in (``-C repo_dir``).
    return path if path.is_absolute() else repo_dir / path


def ensure_pin_ignored(repo_dir: Path, *, git: GitRunner = _subprocess_git) -> bool:
    """Make sure ``/.mindwire/`` is a line of the clone's ``info/exclude``. Idempotent.

    Returns ``True`` when the line was appended, ``False`` when it was already present.
    Raises :class:`DirtyCloneError` (``pin_not_ignored``) on any git or file-system failure.
    """
    path = exclude_path(repo_dir, git=git)
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if any(line.strip() == PIN_EXCLUDE_LINE for line in current.splitlines()):
            return False
        prefix = "" if current == "" or current.endswith("\n") else "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{prefix}{PIN_EXCLUDE_LINE}\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise _error(repo_dir, f"cannot update {path}: {type(exc).__name__}: {exc}") from exc
    return True


__all__ = ["PIN_EXCLUDE_LINE", "ensure_pin_ignored", "exclude_path"]
