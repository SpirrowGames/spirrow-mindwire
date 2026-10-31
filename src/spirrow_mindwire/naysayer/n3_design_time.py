"""Design-time inputs to the naysayer tier decision (ADR-14 §7.3; thread msg-6575 → 6579).

:func:`~.n3_routing.route_tier` stays the only decision point. This module only
prepares what the design-time call site feeds it:

- :func:`resolve_design_time_paths`: referenced files → root-relative POSIX paths,
  with the boundary checks the PR-gate's lexical check cannot do (it has no disk);
- :func:`resolve_prompt_files`: the prompt's host files plus MindWire's own system assets
  (:data:`PROMPT_ASSETS`, checked against :data:`MINDWIRE_ROOT`, msg-6588);
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
from dataclasses import dataclass
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


# --------------------------------------------------------------------------------------- #
# What the design-time prompt carries (thread msg-6582 gap 2, corrected by msg-6588;
# Einstein msg-6587 / the msg-6588 review).
#
# The prompt carries two kinds of file. HOST files come from the reviewed repository and go
# through :func:`resolve_design_time_paths` and the host's globs like any reference. SYSTEM
# ASSETS ship with MindWire itself (the principles SOT and the ADR manifest, read from
# MindWire's own install root, NOT the host): judged against the host root they would always
# be "outside the tree" and send every non-mindwire request to Gemini. They are instead
# checked against :data:`MINDWIRE_ROOT` and the closed set :data:`PROMPT_ASSETS`.
# --------------------------------------------------------------------------------------- #

# n3_design_time.py -> naysayer -> spirrow_mindwire -> src -> <repo root>: the same idiom
# (and so the same directory) as ``principles._REPO_ROOT`` and ``adr_index._REPO_ROOT``, the
# roots the two loaders read their files from.
MINDWIRE_ROOT = Path(__file__).resolve().parents[3]

# The ONLY MindWire files the design-time prompt may carry, as POSIX paths relative to
# :data:`MINDWIRE_ROOT`. Closed on purpose: anything else passed as a system asset (an
# override of ``MINDWIRE_NAYSAYER_PRINCIPLES_PATH`` pointing elsewhere, a new file a future
# builder starts reading) routes the whole request to Gemini until this constant is changed.
# This module is on mindwire's own ``.mindwire-n3.toml`` list, so that change is itself a
# Gemini-reviewed PR (msg-6588).
PROMPT_ASSETS: frozenset[str] = frozenset({"spec/NAYSAYER_PRINCIPLES.md", "spec/adr_index.yaml"})


@dataclass(frozen=True)
class PromptFiles:
    """The files a design-time prompt carries, split by where they come from (msg-6588)."""

    host_files: tuple[str, ...]
    system_assets: tuple[Path, ...]


def _realpath_cased(path: Path) -> Path:
    return _cased(Path(os.path.realpath(path, strict=True)))


def _check_system_assets(assets: Sequence[Path], mindwire_root: Path) -> str | None:
    """``None`` when every asset is a :data:`PROMPT_ASSETS` file under ``mindwire_root``."""
    if not assets:
        return None
    try:
        root = _realpath_cased(mindwire_root)
    except OSError as exc:
        return f"cannot resolve the MindWire root {str(mindwire_root)!r}: {exc}"
    allowed = {_cased(Path(a)).as_posix() for a in PROMPT_ASSETS}
    for asset in assets:
        try:
            resolved = _realpath_cased(Path(asset))
        except OSError as exc:
            return f"cannot resolve system asset {str(asset)!r}: {exc}"
        if not resolved.is_relative_to(root):
            return f"system asset {str(asset)!r} resolves outside the MindWire root"
        if resolved.relative_to(root).as_posix() not in allowed:
            return f"system asset {str(asset)!r} is not in PROMPT_ASSETS"
    return None


def resolve_prompt_files(
    *,
    host_files: Sequence[str],
    system_assets: Sequence[Path],
    summon_dir: Path,
    repo_root: Path | None,
    mindwire_root: Path = MINDWIRE_ROOT,
) -> tuple[tuple[str, ...], str | None]:
    """Root-relative host paths to match against the host's globs, or why the request is
    Gemini. Both file kinds are REQUIRED keyword arguments with no default (msg-6588), so a
    prompt builder that starts carrying a new file cannot reach routing without saying so.

    - ``system_assets`` must each resolve under ``realpath(mindwire_root)`` to a path in
      :data:`PROMPT_ASSETS`; anything else is a problem (→ Gemini). They are NOT matched
      against the host's globs, being no host content.
    - When the host IS mindwire (the two roots resolve equal), the assets are ALSO treated as
      host files, so mindwire's own globs still apply to them (msg-6588, test 36).
    - ``host_files`` go through :func:`resolve_design_time_paths`.
    """
    problem = _check_system_assets(system_assets, mindwire_root)
    if problem is not None:
        return (), problem
    refs: list[str] = list(host_files)
    if system_assets and repo_root is not None:
        try:
            self_hosted = _realpath_cased(repo_root) == _realpath_cased(mindwire_root)
        except OSError as exc:
            return (), f"cannot resolve the repository root {str(repo_root)!r}: {exc}"
        if self_hosted:
            refs.extend(str(asset) for asset in system_assets)
    return resolve_design_time_paths(tuple(refs), summon_dir=summon_dir, repo_root=repo_root)


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
    "MINDWIRE_ROOT",
    "PROMPT_ASSETS",
    "PromptFiles",
    "find_repo_root",
    "remote_default_branch_reader",
    "resolve_design_time_paths",
    "resolve_prompt_files",
    "working_config_reader",
]
