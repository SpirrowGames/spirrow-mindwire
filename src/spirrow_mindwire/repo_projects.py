"""Which project a GitHub repo belongs to, read from ``sweep.json``'s ``clones`` (one helper).

T-gate-fires-on-pr-open, Bohr msg-8030 / msg-8032 / msg-8034 (Einstein msg-8031 / msg-8033 /
msg-8035). An unqualified ``Thread:`` / ``Closes-thread:`` id names a thread of "the PR's project".
1c (the auto-gate), 1b (the PR-event relay) and the conductor's settled-PR check all need that
project, and they all get it here, from the same table the auto-gate already built its targets
from: each clone's ``origin`` remote.

The reverse lookup is only trusted when it is provably unique (:meth:`RepoProjectMap.resolve`):

* ``incomplete`` — some clone's ``origin`` could not be read. That clone may point at any repo,
  so no repo's candidate set can be shown complete, and nothing is resolved for the whole pass
  (msg-8034 Obj-5-1). A clone whose ``origin`` reads but is not a GitHub remote is NOT a gap: it
  cannot hold a GitHub PR, so it is simply no candidate.
* ``none`` — no clone points at the repo, or the table could not be built at all.
* ``multiple`` — two or more projects' clones point at the repo (a worktree, an old project name
  during a move, msg-8032 Obj-4-1).

Only a complete table with exactly one candidate yields a project. Every other answer leaves an
unqualified id unresolved; a project-qualified id never needs this table.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

INCOMPLETE = "incomplete"
NONE = "none"
MULTIPLE = "multiple"


def parse_remote_url(url: str) -> tuple[str, str] | None:
    match = _GITHUB_REMOTE_RE.search(url.strip())
    return (match.group("owner"), match.group("repo")) if match else None


def git_origin_url(clone: str) -> str:
    return subprocess.run(
        ["git", "-C", clone, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def repo_key(owner: str, repo: str) -> str:
    """GitHub owner / repo names are case-insensitive."""
    return f"{owner}/{repo}".casefold()


@dataclass(frozen=True)
class RepoTarget:
    project: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class PrProject:
    """The PR's project, or why it has none. ``projects``: the candidates / unread projects."""

    project: str | None
    reason: str | None = None
    projects: tuple[str, ...] = ()

    def describe(self) -> str:
        """``incomplete: app`` / ``multiple: app, hub`` / ``none``."""
        if self.reason is None:
            return self.project or ""
        return f"{self.reason}: {', '.join(self.projects)}" if self.projects else self.reason


@dataclass(frozen=True)
class RepoProjectMap:
    """``clones`` read once: repo -> candidate projects, and which clones could not be read."""

    candidates: Mapping[str, frozenset[str]] = field(default_factory=dict)
    unresolved: frozenset[str] = frozenset()
    targets: tuple[RepoTarget, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.unresolved

    def resolve(self, owner: str, repo: str) -> PrProject:
        if not self.complete:
            return PrProject(None, INCOMPLETE, tuple(sorted(self.unresolved)))
        found = self.candidates.get(repo_key(owner, repo), frozenset())
        if len(found) == 1:
            return PrProject(next(iter(found)))
        if not found:
            return PrProject(None, NONE)
        return PrProject(None, MULTIPLE, tuple(sorted(found)))


def build_repo_project_map(
    clones: object, origin_url: Callable[[str], str] = git_origin_url
) -> RepoProjectMap:
    """Read every clone's ``origin``. Never raises: an unreadable clone is a gap, not a crash.

    A ``clones`` value that is not a non-empty string is also a gap: what it points at is unknown.
    """
    clone_map: Mapping[Any, Any] = clones if isinstance(clones, Mapping) else {}
    candidates: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    targets: list[RepoTarget] = []
    errors: dict[str, str] = {}
    for project in sorted(p for p in clone_map if isinstance(p, str)):
        clone = clone_map[project]
        if not isinstance(clone, str) or not clone:
            unresolved.add(project)
            errors[project] = "no clones entry"
            continue
        try:
            parsed = parse_remote_url(origin_url(clone))
        except (OSError, subprocess.SubprocessError) as exc:
            unresolved.add(project)
            errors[project] = f"origin unreadable: {exc}"
            continue
        if parsed is None:
            errors[project] = "origin is not a GitHub remote"
            continue
        targets.append(RepoTarget(project, parsed[0], parsed[1]))
        candidates.setdefault(repo_key(*parsed), set()).add(project)
    return RepoProjectMap(
        candidates={k: frozenset(v) for k, v in candidates.items()},
        unresolved=frozenset(unresolved),
        targets=tuple(targets),
        errors=errors,
    )


def load_repo_project_map(
    sweep_config: Path | None, origin_url: Callable[[str], str] = git_origin_url
) -> RepoProjectMap:
    """The map from a ``sweep.json`` path; unreadable or absent: empty (every repo ``none``)."""
    if sweep_config is None:
        return RepoProjectMap()
    try:
        sweep = json.loads(sweep_config.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return RepoProjectMap()
    clones = sweep.get("clones") if isinstance(sweep, dict) else None
    return build_repo_project_map(clones, origin_url)


__all__ = [
    "INCOMPLETE",
    "MULTIPLE",
    "NONE",
    "PrProject",
    "RepoProjectMap",
    "RepoTarget",
    "build_repo_project_map",
    "git_origin_url",
    "load_repo_project_map",
    "parse_remote_url",
    "repo_key",
]
