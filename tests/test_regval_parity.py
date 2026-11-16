"""Parity between the two regval scripts (PR-gate #565 finding 7).

regval_expose_a1.py stays a separate file so it can be reviewed alone; the policy it shares with
regval_investigate.py (output shapes, commit lookup, child environment, fd protection) is pinned
here so the two cannot drift again.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_parity", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rv = _load("regval_investigate")
ea = _load("regval_expose_a1")
SHA = "e" * 40


def test_shared_shapes_are_identical_or_stricter() -> None:
    for kind in ("sha", "repo", "int", "tag"):
        assert ea._SHAPES[kind].pattern == rv._SHAPES[kind].pattern
    # expose-a1 only ever prints UTC Z times; investigate also accepts offsets.
    assert ea.safe("time", "2026-10-02T01:28:37Z") == rv.safe("time", "2026-10-02T01:28:37Z")


def test_child_env_is_identical() -> None:
    base = {"GIT_DIR": "x", "GIT_REPLACE_REF_BASE": "y", "PATH": "p", "GH_TOKEN": "t"}
    assert ea.child_env(base) == rv.child_env(base)
    assert ea.child_env()["GIT_NO_REPLACE_OBJECTS"] == "1"


@pytest.mark.parametrize(
    "answer",
    [
        (0, SHA + "\n", ""),
        (0, "f" * 40, ""),
        (1, "", "gh: Not Found (HTTP 404)"),
        (1, "", "gh: (HTTP 422)"),
        (1, "", "something else"),
        (124, "", ""),
    ],
)
def test_commit_fetchable_agrees(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[int, str, str]
) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return answer

    monkeypatch.setattr(rv, "run3", fake)
    monkeypatch.setattr(ea, "run3", fake)
    assert rv.commit_fetchable("o/r", SHA) == ea.commit_fetchable("o/r", SHA)
    assert seen[0] == seen[1]


def test_rc_tag_agrees() -> None:
    for rc in (0, 1, 3, 124, 128, 5000, -1):
        assert rv.rc_tag(rc) == ea.rc_tag(rc)
