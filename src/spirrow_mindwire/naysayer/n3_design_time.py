"""Design-time inputs to the naysayer tier decision (ADR-14 §7.3; thread msg-6575 → 6579).

:func:`~.n3_routing.route_tier` stays the only decision point. This module only
prepares what the design-time call site feeds it:

- :func:`resolve_design_time_paths`: referenced files → root-relative POSIX paths,
  with the boundary checks the PR-gate's lexical check cannot do (it has no disk);
- :func:`find_repo_root`: the prompt source's git top level, never the process cwd;
- :func:`working_config_reader`: the working side, ``N3_CONFIG_PATH`` at that root;
- :func:`remote_default_branch_reader`: the trusted side, the HOST_REPO's remote
  default branch through the GitHub contents API.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .n3_routing import ConfigReader
from .principles import N3_CONFIG_PATH


def _cased(path: Path) -> Path:
    return Path(os.path.normcase(str(path)))


def resolve_design_time_paths(
    refs: Sequence[str], *, summon_dir: Path, repo_root: Path | None
) -> tuple[tuple[str, ...], str | None]:
    """Turn a thread's referenced paths into root-relative POSIX paths, or say why not.

    Returns ``(paths, None)`` or ``((), problem)``; a problem routes the WHOLE request to
    Gemini. Each reference has ``~`` expanded, is made absolute against ``summon_dir``, and
    is ``realpath``-ed strictly, so a symlink is judged by where it points and a missing file
    is a problem (msg-6577). The root is ``realpath``-ed too (a clone under a symlinked
    directory, msg-6578 advisory), and containment is :meth:`Path.is_relative_to` on
    ``normcase``-d paths, never a string prefix, which would put ``/repo2/x`` inside
    ``/repo`` (msg-6579). Design-time only: the PR-gate never touches the disk.
    """
    if not refs:
        return (), None
    if repo_root is None:
        return (), "no repository root for the referenced files"
    try:
        root = _cased(Path(os.path.realpath(repo_root, strict=True)))
    except OSError as exc:
        return (), f"cannot resolve the repository root {str(repo_root)!r}: {exc}"
    out: list[str] = []
    for ref in refs:
        candidate = Path(os.path.expanduser(ref))
        if not candidate.is_absolute():
            candidate = summon_dir / candidate
        try:
            resolved = _cased(Path(os.path.realpath(candidate, strict=True)))
        except OSError as exc:
            return (), f"cannot resolve {ref!r}: {exc}"
        if not resolved.is_relative_to(root):
            return (), f"{ref!r} resolves outside the repository root"
        out.append(resolved.relative_to(root).as_posix())
    return tuple(out), None


def _git_toplevel(source_dir: Path) -> Path | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = done.stdout.strip()
    return Path(top) if done.returncode == 0 and top else None


async def find_repo_root(source_dir: Path) -> Path | None:
    """``git rev-parse --show-toplevel`` for ``source_dir``; ``None`` if it is no work tree."""
    return await asyncio.to_thread(_git_toplevel, source_dir)


def working_config_reader(repo_root: Path | None) -> ConfigReader:
    """Read :data:`N3_CONFIG_PATH` at the repository ROOT, never the process cwd (msg-6575).

    No root → raise (the working side is unreadable → Gemini, test 22). Root found but no
    file → ``None`` (adds nothing; the trusted side decides).
    """

    async def read() -> str | None:
        if repo_root is None:
            raise FileNotFoundError("no git repository root for the working-side config")
        try:
            return (repo_root / N3_CONFIG_PATH).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    return read


_GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def _origin_slug(repo_root: Path) -> tuple[str, str] | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _GITHUB_REMOTE.search(done.stdout.strip()) if done.returncode == 0 else None
    return (match.group(1), match.group(2)) if match else None


def remote_default_branch_reader(repo_root: Path | None) -> ConfigReader:
    """Design-time TRUSTED side: :data:`N3_CONFIG_PATH` on the HOST_REPO's remote default
    branch, read through the GitHub contents API at call time — never the local tree or the
    checked-out branch (msg-6477). Any failure raises (→ unresolved → notify + Gemini).
    """

    async def read() -> str | None:
        from ..github.client import GitHubClient, PrRef

        if repo_root is None:
            raise FileNotFoundError("no git repository root, so no HOST_REPO to read")
        slug = await asyncio.to_thread(_origin_slug, repo_root)
        if slug is None:
            raise LookupError(f"origin of {repo_root} is not a github.com remote")
        owner, repo = slug
        async with GitHubClient() as client:
            branch = await client.fetch_default_branch(owner, repo)
            # ``fetch_file_at`` reads ``/repos/{owner}/{repo}/contents/...`` and uses only the
            # owner/repo of its PrRef; there is no PR here, so the number is a placeholder.
            return await client.fetch_file_at(
                PrRef(owner, repo, 0), path=N3_CONFIG_PATH, ref=branch
            )

    return read


__all__ = [
    "find_repo_root",
    "remote_default_branch_reader",
    "resolve_design_time_paths",
    "working_config_reader",
]
