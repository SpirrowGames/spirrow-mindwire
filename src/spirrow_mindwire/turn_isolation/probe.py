"""S8: refuse to run unless the turn user can really use every clone (spec revision 5, msg-7097).

Runs once per tick, **as the turn user**, for every clone:

1. ``git rev-parse --absolute-git-dir --git-common-dir`` with the clone as the working
   directory (the clone is made absolute first; see :func:`probe_clone`). A failure stops with
   ``turn user cannot use clone <path>: safe.directory`` when git refused the repo's ownership
   (CVE-2022-24765), otherwise ``...: git: <first stderr line>``. ``--git-common-dir`` may be
   relative; it is resolved against the (absolute) clone.
2. For each distinct directory from step 1 (one for a plain clone; the worktree's git dir and
   the shared common dir for a worktree; ``.git/modules/<name>`` for a submodule): delete any
   leftover ``mindwire-probe-*``, then write and delete ``mindwire-probe-<pid>``. A failure
   stops with ``turn user cannot use clone <path>: ACL (<directory>)``.
3. Nothing is written to the working tree and no ``.git`` path is hard-coded, so an interrupted
   probe can never leave ``clone_guard`` a dirty tree.

What runs "as the turn user" is :class:`TurnUserOps`. Production uses :class:`LauncherOps`,
which goes through the real as-user launcher; tests inject an implementation that runs as the
current user against real repositories.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import SECRETS_STOP_EXIT_CODE, TurnIsolationStop, parse_stop_line

PROBE_PREFIX = "mindwire-probe-"
_OWNERSHIP_MARKERS = ("dubious ownership", "unsafe repository")


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class TurnUserOps(Protocol):
    def rev_parse(self, clone: Path) -> RunResult:
        """``git rev-parse --absolute-git-dir --git-common-dir`` in ``clone``, as the turn user.

        ``clone`` is always absolute (:func:`probe_clone` resolves it first)."""

    def probe_write(self, directory: Path, pid: int) -> RunResult:
        """Remove leftover probes in ``directory``, then write and delete one, as the turn user."""


def probe_clone(clone: Path, ops: TurnUserOps, *, pid: int | None = None) -> list[Path]:
    """Probe one clone; return the directories it wrote to. Raises :class:`TurnIsolationStop`."""
    pid = os.getpid() if pid is None else pid
    # One absolute path from here on: the launcher's ``--cwd`` and the resolution of a relative
    # ``--git-common-dir`` must not depend on the directory the check was started from
    # (PR #485 review round 7).
    clone = Path(os.path.abspath(clone))
    head = f"turn user cannot use clone {clone}"
    res = ops.rev_parse(clone)
    if res.returncode != 0:
        if any(m in res.stderr for m in _OWNERSHIP_MARKERS):
            raise TurnIsolationStop(f"{head}: safe.directory")
        first = next((ln.strip() for ln in res.stderr.splitlines() if ln.strip()), "-")
        raise TurnIsolationStop(f"{head}: git: {first}")
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) != 2:
        raise TurnIsolationStop(f"{head}: git: unexpected rev-parse output")
    directories: list[Path] = []
    seen: set[str] = set()
    for raw in lines:
        path = Path(raw)
        if not path.is_absolute():
            path = clone / path
        resolved = Path(os.path.abspath(path))
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            directories.append(resolved)
    for directory in directories:
        if ops.probe_write(directory, pid).returncode != 0:
            raise TurnIsolationStop(f"{head}: ACL ({directory})")
    return directories


def probe_clones(clones: Sequence[Path], ops: TurnUserOps) -> None:
    """Probe every clone; the first failure stops the tick."""
    for clone in clones:
        probe_clone(clone, ops)


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def probe_write_script(directory: Path, pid: int) -> str:
    """The PowerShell the turn user runs for step 2 (shown here so a test can read it)."""
    d = _ps_quote(directory)
    return (
        "$ErrorActionPreference='Stop';"
        f"Get-ChildItem -LiteralPath {d} -Filter '{PROBE_PREFIX}*' -Force -File"
        " | Remove-Item -Force;"
        f"$p=Join-Path {d} '{PROBE_PREFIX}{pid}';"
        "[IO.File]::WriteAllBytes($p,[byte[]]@());"
        "Remove-Item -LiteralPath $p -Force"
    )


class LauncherOps:
    """:class:`TurnUserOps` through the real as-user launcher.

    The launcher's own stop (a missing credential, a refused logon) is not a clone fault: it is
    re-raised with the launcher's reason instead of being misread as ``safe.directory`` / ACL.

    ``user`` is the turn user the caller resolved from its own config; it is handed to the
    launcher as ``--user`` so the launcher never falls back to a different config file. Every
    call also names the child's working directory (``--cwd``): the clone for ``rev-parse``, the
    probed directory for the write probe. Both are places the probe requires the turn user to
    reach anyway, so the ambient directory the check was started from never matters
    (PR #485 review round 3).
    """

    def __init__(
        self,
        user: str | None = None,
        launcher: Sequence[str] | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self._user = user
        self._launcher = list(
            launcher
            if launcher is not None
            else [sys.executable, "-m", "spirrow_mindwire.turn_isolation.launcher"]
        )
        self._timeout_s = timeout_s

    def launcher_argv(self, cwd: Path, argv: Sequence[str]) -> list[str]:
        opts = ["--user", self._user] if self._user else []
        return [*self._launcher, *opts, "--cwd", str(cwd), "--", *argv]

    def _run(self, cwd: Path, argv: Sequence[str]) -> RunResult:
        proc = subprocess.run(
            self.launcher_argv(cwd, argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout_s,
            check=False,
        )
        if proc.returncode == SECRETS_STOP_EXIT_CODE:
            reason = parse_stop_line(proc.stderr)
            if reason is not None:
                raise TurnIsolationStop(reason)
        return RunResult(proc.returncode, proc.stdout, proc.stderr)

    def rev_parse(self, clone: Path) -> RunResult:
        # The launcher's ``--cwd`` is the only place the clone is named. Passing it to git as
        # ``-C`` as well would apply a relative path twice (``<clone>/<clone>``; PR #485 review
        # round 7).
        return self._run(clone, ["git", "rev-parse", "--absolute-git-dir", "--git-common-dir"])

    def probe_write(self, directory: Path, pid: int) -> RunResult:
        encoded = base64.b64encode(probe_write_script(directory, pid).encode("utf-16-le"))
        return self._run(
            directory,
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded.decode("ascii"),
            ],
        )
