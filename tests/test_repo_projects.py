"""The repo -> project table (T-gate-fires-on-pr-open, Bohr msg-8030 / msg-8032 / msg-8034).

Einstein msg-8031 (several projects on one repo), msg-8033 (an unread origin) and the msg-8034
review note (a non-GitHub clone is no candidate, and does not block a unique one) are the cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spirrow_mindwire.repo_projects import (
    INCOMPLETE,
    MULTIPLE,
    NONE,
    PrProject,
    RepoTarget,
    build_repo_project_map,
    load_repo_project_map,
)

_ORIGINS = {
    "C:/hub": "https://github.com/acme/widgets.git\n",
    "C:/hub-worktree": "git@github.com:acme/widgets.git\n",
    "C:/lexora": "https://github.com/acme/lexora\n",
    "C:/gitlab": "https://gitlab.com/acme/widgets.git\n",
}


def _origin(clone: str) -> str:
    if clone not in _ORIGINS:
        raise subprocess.CalledProcessError(2, ["git"], stderr="not a git repository")
    return _ORIGINS[clone]


def test_a_repo_with_one_project_resolves_to_it() -> None:
    table = build_repo_project_map({"hub": "C:/hub", "lexora": "C:/lexora"}, _origin)
    assert table.complete
    assert table.resolve("acme", "widgets") == PrProject("hub")
    assert table.resolve("ACME", "Widgets") == PrProject("hub")  # GitHub names ignore case
    assert table.targets == (
        RepoTarget("hub", "acme", "widgets"),
        RepoTarget("lexora", "acme", "lexora"),
    )


def test_two_projects_on_one_repo_resolve_to_none_of_them() -> None:
    # msg-8031 / msg-8032: e.g. a worktree, or an old project name during a move.
    table = build_repo_project_map({"hub": "C:/hub", "app": "C:/hub-worktree"}, _origin)
    assert table.resolve("acme", "widgets") == PrProject(None, MULTIPLE, ("app", "hub"))
    assert table.resolve("acme", "widgets").describe() == "multiple: app, hub"


def test_one_unread_origin_makes_every_repo_unresolved() -> None:
    # msg-8033: app may point at acme/widgets too, so hub alone is not provably unique.
    table = build_repo_project_map({"hub": "C:/hub", "app": "C:/missing"}, _origin)
    assert not table.complete
    assert table.unresolved == frozenset({"app"})
    assert table.resolve("acme", "widgets") == PrProject(None, INCOMPLETE, ("app",))
    assert table.resolve("acme", "widgets").describe() == "incomplete: app"
    assert table.errors["app"].startswith("origin unreadable:")


@pytest.mark.parametrize("value", [None, "", 3, ["C:/hub"]])
def test_a_clones_value_that_is_no_path_is_a_gap(value: object) -> None:
    table = build_repo_project_map({"hub": "C:/hub", "app": value}, _origin)
    assert table.resolve("acme", "widgets").reason == INCOMPLETE


def test_only_non_github_clones_is_complete_with_no_candidate() -> None:
    # msg-8034 review note: complete, but zero candidates, so nothing resolves.
    table = build_repo_project_map({"gl": "C:/gitlab"}, _origin)
    assert table.complete
    assert table.resolve("acme", "widgets") == PrProject(None, NONE)
    assert table.errors == {"gl": "origin is not a GitHub remote"}


def test_a_read_non_github_clone_does_not_block_a_unique_github_one() -> None:
    table = build_repo_project_map({"hub": "C:/hub", "gl": "C:/gitlab"}, _origin)
    assert table.complete
    assert table.resolve("acme", "widgets") == PrProject("hub")


def test_a_repo_no_clone_points_at_is_none() -> None:
    table = build_repo_project_map({"hub": "C:/hub"}, _origin)
    assert table.resolve("acme", "other") == PrProject(None, NONE)


@pytest.mark.parametrize("clones", [None, [], "C:/hub"])
def test_no_clones_table_resolves_nothing(clones: object) -> None:
    assert build_repo_project_map(clones, _origin).resolve("acme", "widgets").reason == NONE


def test_load_reads_the_clones_of_a_sweep_json(tmp_path: Path) -> None:
    sweep = tmp_path / "sweep.json"
    sweep.write_text('{"clones": {"hub": "C:/hub"}}', encoding="utf-8")
    assert load_repo_project_map(sweep, _origin).resolve("acme", "widgets") == PrProject("hub")


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_load_of_an_unusable_sweep_json_resolves_nothing(
    tmp_path: Path, content: str | None
) -> None:
    sweep = tmp_path / "sweep.json"
    if content is not None:
        sweep.write_text(content, encoding="utf-8")
    assert load_repo_project_map(sweep, _origin).resolve("acme", "widgets").reason == NONE
    assert load_repo_project_map(None, _origin).resolve("acme", "widgets").reason == NONE
