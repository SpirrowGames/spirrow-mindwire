"""ADR-14 §7.3 — design-time inputs to the naysayer tier decision.

Thread T-D8-codex-backend-adr14-15-amendment: tests 20-22 (Bohr msg-6575: the working
side is read at the repository root, referenced paths are made root-relative, no root
→ Gemini), 23-26 (msg-6577: out-of-tree, symlink and unresolvable references → Gemini;
an in-tree absolute path is not blocked), 31-32 (msg-6579: symlinked root, prefix-
colliding sibling), plus the adapter's per-turn call site (tags, trusted-side
notification, test 11's codex path) and the conductor's tag read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.naysayer.n3_design_time import (
    find_repo_root,
    resolve_design_time_paths,
    working_config_reader,
)
from spirrow_mindwire.naysayer.n3_routing import (
    GlobLoad,
    N3Request,
    load_n3_globs,
    route_tier,
)
from spirrow_mindwire.naysayer.principles import (
    CODEX_TIER_PROMPT_CHAR_LIMIT,
    N3_CONFIG_PATH,
    N3_SENSITIVE_MARKER,
    N3_SENSITIVE_PATHS_KEY,
    NAYSAYER_GEMINI_TIER,
    NAYSAYER_MODEL_TIER,
)

_GEMINI = NAYSAYER_GEMINI_TIER
_CODEX = NAYSAYER_MODEL_TIER


def _toml(*globs: str) -> str:
    inner = ", ".join(f'"{g}"' for g in globs)
    return f"{N3_SENSITIVE_PATHS_KEY} = [{inner}]\n"


def _reader(text: str | None):  # type: ignore[no-untyped-def]
    async def read() -> str | None:
        return text

    return read


def _failing_reader(exc: Exception):  # type: ignore[no-untyped-def]
    async def read() -> str | None:
        raise exc

    return read


async def _globs(trusted: str | None, working: str | None = None) -> GlobLoad:
    return await load_n3_globs(trusted=_reader(trusted), working=_reader(working))


def _route(
    globs: GlobLoad,
    *paths: str,
    marker: bool | None = False,
    prompt_chars: int = 1_000,
) -> str:
    return route_tier(
        N3Request(marker=marker, paths=paths, globs=globs, prompt_chars=prompt_chars)
    ).tier


# ---------- 20-26, 31, 32: design-time path resolution --------------------- #


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "backend").mkdir(parents=True)
    (root / "src" / "backend" / "x.py").write_text("x", encoding="utf-8")
    (root / "payments").mkdir()
    (root / "payments" / "keys.py").write_text("k", encoding="utf-8")
    return root


@pytest.mark.anyio
async def test_20_summoned_from_a_subdirectory_reads_the_root_config(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / N3_CONFIG_PATH).write_text(_toml("payments/*"), encoding="utf-8")
    globs = await load_n3_globs(trusted=_reader(_toml()), working=working_config_reader(root))
    paths, problem = resolve_design_time_paths(
        [str(root / "payments" / "keys.py")], summon_dir=root / "src" / "backend", repo_root=root
    )
    assert problem is None
    assert route_tier(N3Request(False, paths, globs, 10, path_problem=problem)).tier == _GEMINI


@pytest.mark.anyio
async def test_21_relative_reference_from_a_subdirectory_is_made_root_relative(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    paths, problem = resolve_design_time_paths(
        ["x.py"], summon_dir=root / "src" / "backend", repo_root=root
    )
    assert problem is None and paths == ("src/backend/x.py",)
    globs = await _globs(_toml("src/backend/**"))
    assert _route(globs, *paths) == _GEMINI


@pytest.mark.anyio
async def test_22_no_repository_root_routes_to_gemini(tmp_path: Path) -> None:
    globs = await load_n3_globs(trusted=_reader(_toml()), working=working_config_reader(None))
    assert globs.globs is None
    assert _route(globs, "src/app.py") == _GEMINI
    _paths, problem = resolve_design_time_paths(["a.py"], summon_dir=tmp_path, repo_root=None)
    assert problem is not None


@pytest.mark.parametrize("ref", ["~/.aws/credentials", "../../../secrets.env"])
def test_23_out_of_tree_reference_routes_to_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    root = _repo(tmp_path)
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("secret", encoding="utf-8")
    (tmp_path / "secrets.env").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    paths, problem = resolve_design_time_paths(
        [ref], summon_dir=root / "src" / "backend", repo_root=root
    )
    assert problem is not None and "outside" in problem
    decision = route_tier(N3Request(False, paths, GlobLoad(frozenset()), 10, path_problem=problem))
    assert decision.tier == _GEMINI


def test_24_symlink_inside_the_tree_pointing_outside_routes_to_gemini(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "creds"
    outside.write_text("secret", encoding="utf-8")
    link = root / "creds"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    _paths, problem = resolve_design_time_paths(["creds"], summon_dir=root, repo_root=root)
    assert problem is not None and "outside" in problem


def test_25_unresolvable_reference_routes_to_gemini(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _paths, problem = resolve_design_time_paths(
        ["no/such/file.py"], summon_dir=root, repo_root=root
    )
    assert problem is not None and "cannot resolve" in problem


@pytest.mark.anyio
async def test_26_absolute_path_inside_the_root_is_not_blocked(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    paths, problem = resolve_design_time_paths(
        [str(root / "src" / "backend" / "x.py")], summon_dir=tmp_path, repo_root=root
    )
    assert problem is None and paths == ("src/backend/x.py",)
    globs = await _globs(_toml("deploy/*"))
    assert route_tier(N3Request(False, paths, globs, 10)).tier == _CODEX


def test_31_root_under_a_symlinked_directory_is_not_a_violation(tmp_path: Path) -> None:
    real = tmp_path / "private"
    root = _repo(real)
    alias = tmp_path / "var"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    aliased_root = alias / root.relative_to(real)
    paths, problem = resolve_design_time_paths(
        [str(root / "src" / "backend" / "x.py")], summon_dir=aliased_root, repo_root=aliased_root
    )
    assert problem is None and paths == ("src/backend/x.py",)


def test_32_prefix_colliding_sibling_is_out_of_tree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    sibling = tmp_path / "repo2"
    sibling.mkdir()
    (sibling / "x").write_text("s", encoding="utf-8")
    _paths, problem = resolve_design_time_paths(
        [str(sibling / "x")], summon_dir=root, repo_root=root
    )
    assert problem is not None and "outside" in problem


# ---------- design-time call site (the adapter's per-turn decision) -------- #


def _adapter(tmp_path: Path, **kwargs: Any) -> Any:
    from spirrow_mindwire.adapters.naysayer_sdk import NaysayerSdkAdapter
    from spirrow_mindwire.obligations import load_manifest

    return NaysayerSdkAdapter(
        cwd=tmp_path, obligations=load_manifest(), inference_base_url="http://x:1", **kwargs
    )


def _event(tags: tuple[str, ...] | None) -> Any:
    from datetime import UTC, datetime

    from spirrow_mindwire.value_objects import (
        ChatroomEvent,
        EventType,
        NewMessagePayload,
        ThreadRef,
    )

    return ChatroomEvent(
        event_id="e1",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=ThreadRef("spirrow-mindwire", "T-design", "mc://t/1"),
        occurred_at=datetime(2026, 10, 4, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="m1", author="Bohr", body="x", parent_msg_id=None),
        thread_tags=tags,
    )


def _root_finder(root: Path | None):  # type: ignore[no-untyped-def]
    async def find(_source: Path) -> Path | None:
        return root

    return find


def _trusted(text: str | None, exc: Exception | None = None):  # type: ignore[no-untyped-def]
    def make(_root: Path | None):  # type: ignore[no-untyped-def]
        async def read() -> str | None:
            if exc is not None:
                raise exc
            return text

        return read

    return make


@pytest.mark.anyio
async def test_design_time_unread_tags_route_to_gemini(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path, repo_root_finder=_root_finder(tmp_path), trusted_reader=_trusted(_toml())
    )
    decision = await adapter._turn_tier(_event(None), "prompt")
    assert decision.tier == _GEMINI


@pytest.mark.anyio
async def test_design_time_tag_marker_routes_to_gemini(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path, repo_root_finder=_root_finder(tmp_path), trusted_reader=_trusted(_toml())
    )
    decision = await adapter._turn_tier(_event(("design", N3_SENSITIVE_MARKER)), "prompt")
    assert decision.tier == _GEMINI


@pytest.mark.anyio
async def test_11_design_time_read_tags_no_marker_populated_list_routes_to_codex(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    adapter = _adapter(
        root,
        repo_root_finder=_root_finder(root),
        trusted_reader=_trusted(_toml("deploy/*")),
        referenced_files=lambda _e: ["src/backend/x.py"],
    )
    decision = await adapter._turn_tier(_event(("design",)), "prompt")
    assert decision.tier == _CODEX, decision.reason


@pytest.mark.anyio
async def test_12_design_time_trusted_failure_routes_to_gemini_and_notifies(
    tmp_path: Path,
) -> None:
    told: list[str] = []

    async def notifier(line: str) -> None:
        told.append(line)

    adapter = _adapter(
        tmp_path,
        repo_root_finder=_root_finder(tmp_path),
        trusted_reader=_trusted(None, RuntimeError("GitHub 502")),
        n3_notifier=notifier,
    )
    decision = await adapter._turn_tier(_event(()), "prompt")
    assert decision.tier == _GEMINI
    assert len(told) == 1 and "GitHub 502" in told[0] and "design-time" in told[0]


@pytest.mark.anyio
async def test_design_time_out_of_tree_reference_routes_to_gemini(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (tmp_path / "secrets.env").write_text("s", encoding="utf-8")
    adapter = _adapter(
        root,
        repo_root_finder=_root_finder(root),
        trusted_reader=_trusted(_toml()),
        referenced_files=lambda _e: ["../secrets.env"],
    )
    decision = await adapter._turn_tier(_event(()), "prompt")
    assert decision.tier == _GEMINI and "outside" in decision.reason


@pytest.mark.anyio
async def test_design_time_over_limit_prompt_routes_to_gemini(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path, repo_root_finder=_root_finder(tmp_path), trusted_reader=_trusted(_toml())
    )
    decision = await adapter._turn_tier(_event(()), "x" * (CODEX_TIER_PROMPT_CHAR_LIMIT + 1))
    assert decision.tier == _GEMINI


@pytest.mark.anyio
async def test_working_side_reads_the_real_git_toplevel(tmp_path: Path) -> None:
    """``find_repo_root`` is ``git rev-parse --show-toplevel`` — from a subdirectory too."""
    import subprocess

    root = _repo(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    found = await find_repo_root(root / "src" / "backend")
    assert found is not None
    assert Path(os.path.realpath(found)) == Path(os.path.realpath(root))
    assert await find_repo_root(tmp_path / "home-not-a-repo") is None


def test_conductor_hands_get_thread_tags_to_the_event() -> None:
    """The measured ``get_thread(mode="full")`` shape carries tags under ``thread``."""
    from spirrow_mindwire.conductor.core import _thread_tags

    assert _thread_tags({"thread": {"tags": ["n3-sensitive"]}, "messages": []}) == ("n3-sensitive",)
    assert _thread_tags({"thread": {"tags": []}}) == ()
    assert _thread_tags({"messages": []}) is None  # not read → the routing treats as marked
    assert _thread_tags({"thread": {"tags": [1]}}) is None
