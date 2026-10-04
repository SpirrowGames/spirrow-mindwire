"""ADR-14 §7.3 / §7.5 / §7.6 — the naysayer tier decision and its config loader.

These are the tests that guard the decision ITSELF (§7.6: the attestation derives
its accepted set from the same decision, so it cannot catch a wrong decision; §7.9
step 4 requires this suite). Numbering follows the thread's agreed list
(T-D8-codex-backend-adr14-15-amendment, Bohr msg-6475 1-9, msg-6477 10-13,
msg-6479 14-18; Einstein msg-6480 / msg-6482).

Design-time cases (10-13, 15) exercise ``load_n3_globs`` + ``route_tier`` with
the design-time readers' semantics (trusted = remote default branch, working =
the prompt builder's source). Their call site is not wired yet — the design-time
adapter routes fail-safe to the Gemini tier until it is
(``test_design_time_adapter_is_fail_safe_gemini_until_wired``).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.naysayer import principles
from spirrow_mindwire.naysayer.n3_routing import (
    GlobLoad,
    N3Request,
    find_repo_root,
    has_marker,
    lexical_path_problem,
    load_n3_globs,
    normalize_path,
    resolve_design_time_paths,
    route_tier,
    working_config_reader,
)
from spirrow_mindwire.naysayer.principles import (
    CODEX_TIER_PROMPT_CHAR_LIMIT,
    N3_CONFIG_PATH,
    N3_SENSITIVE_MARKER,
    N3_SENSITIVE_PATHS_KEY,
    NAYSAYER_GEMINI_TIER,
    NAYSAYER_MODEL_TIER,
)

_REPO = Path(__file__).resolve().parents[1]
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


# ---------- 1-9: PR-gate (trusted = base, working = head) ------------------ #


@pytest.mark.anyio
async def test_01_marker_alone_routes_to_gemini() -> None:
    globs = await _globs(_toml("secrets/**"))
    assert _route(globs, "src/app.py", marker=True) == _GEMINI


@pytest.mark.anyio
async def test_02_glob_match_alone_routes_to_gemini() -> None:
    globs = await _globs(_toml("deploy/*"))
    assert _route(globs, "deploy/firewall.ps1") == _GEMINI


@pytest.mark.anyio
async def test_03_populated_list_no_match_no_marker_routes_to_codex() -> None:
    """Einstein msg-6474: the glob engine's negative case is the codex tier's main path."""
    globs = await _globs(_toml("deploy/*", "secrets/**"))
    assert _route(globs, "src/app.py", "tests/test_app.py") == _CODEX


@pytest.mark.anyio
async def test_04_explicit_empty_list_no_marker_routes_to_codex() -> None:
    globs = await _globs(_toml())
    assert globs.globs == frozenset()
    assert _route(globs, "src/app.py") == _CODEX


@pytest.mark.anyio
async def test_05_key_missing_at_base_routes_to_gemini() -> None:
    globs = await _globs("other_key = 1\n")
    assert globs.trusted_unresolved
    assert _route(globs, "src/app.py") == _GEMINI


@pytest.mark.anyio
async def test_06_file_missing_at_base_routes_to_gemini() -> None:
    globs = await _globs(None)
    assert globs.trusted_unresolved
    assert _route(globs, "src/app.py") == _GEMINI


@pytest.mark.anyio
async def test_07_pr_touching_the_config_file_routes_to_gemini_even_with_empty_list() -> None:
    globs = await _globs(_toml(), _toml())
    assert _route(globs, "src/app.py", N3_CONFIG_PATH) == _GEMINI


@pytest.mark.anyio
async def test_08_head_dropping_a_glob_is_still_judged_by_base() -> None:
    globs = await _globs(trusted=_toml("auth/*"), working=_toml())
    assert _route(globs, "auth/login.py") == _GEMINI


@pytest.mark.anyio
async def test_09_over_the_codex_limit_routes_to_gemini() -> None:
    globs = await _globs(_toml())
    assert _route(globs, "src/app.py", prompt_chars=CODEX_TIER_PROMPT_CHAR_LIMIT) == _CODEX
    assert _route(globs, "src/app.py", prompt_chars=CODEX_TIER_PROMPT_CHAR_LIMIT + 1) == _GEMINI


# ---------- 10-13: design-time (trusted = remote default branch) ----------- #


@pytest.mark.anyio
async def test_10_design_time_local_edit_cannot_remove_a_default_branch_glob() -> None:
    globs = await _globs(trusted=_toml("secrets/**"), working=_toml())
    assert _route(globs, "secrets/vault.toml") == _GEMINI


@pytest.mark.anyio
async def test_11_design_time_populated_list_no_match_no_tag_routes_to_codex() -> None:
    globs = await _globs(trusted=_toml("secrets/**"), working=None)
    assert _route(globs, "docs/architecture.md") == _CODEX


@pytest.mark.anyio
async def test_12_design_time_default_branch_unresolvable_routes_to_gemini_and_flags() -> None:
    globs = await load_n3_globs(
        trusted=_failing_reader(RuntimeError("GitHub 502")), working=_reader(_toml())
    )
    assert globs.trusted_unresolved  # the caller's cue to notify (msg-6477)
    assert globs.failure is not None and "GitHub 502" in globs.failure
    assert _route(globs, "docs/architecture.md") == _GEMINI


@pytest.mark.anyio
async def test_13_design_time_reference_to_the_config_file_routes_to_gemini() -> None:
    globs = await _globs(trusted=_toml())
    assert _route(globs, N3_CONFIG_PATH) == _GEMINI


# ---------- 14-18: the union loader --------------------------------------- #


@pytest.mark.anyio
async def test_14_head_glob_matching_a_new_file_counts_even_if_base_lacks_it() -> None:
    globs = await _globs(trusted=_toml("deploy/*"), working=_toml("deploy/*", "keys/*"))
    assert globs.globs == frozenset({"deploy/*", "keys/*"})
    assert _route(globs, "keys/new.pem") == _GEMINI


@pytest.mark.anyio
async def test_15_design_time_working_glob_counts_without_referencing_the_config() -> None:
    """Einstein msg-6478's scenario."""
    globs = await _globs(trusted=_toml("deploy/*"), working=_toml("payments/*"))
    assert _route(globs, "payments/keys.py") == _GEMINI


@pytest.mark.anyio
async def test_16_working_side_removing_a_trusted_glob_still_matches() -> None:
    globs = await _globs(trusted=_toml("auth/*", "deploy/*"), working=_toml("deploy/*"))
    assert _route(globs, "auth/token.py") == _GEMINI


@pytest.mark.anyio
@pytest.mark.parametrize(
    "working",
    [
        "this is = = not toml",
        f'{N3_SENSITIVE_PATHS_KEY} = "auth/*"\n',
        f"{N3_SENSITIVE_PATHS_KEY} = [1]\n",
    ],
)
async def test_17_malformed_working_config_routes_to_gemini(working: str) -> None:
    globs = await _globs(trusted=_toml(), working=working)
    assert globs.globs is None and not globs.trusted_unresolved
    assert _route(globs, "src/app.py") == _GEMINI


@pytest.mark.anyio
async def test_17b_unreadable_working_config_routes_to_gemini() -> None:
    globs = await load_n3_globs(
        trusted=_reader(_toml()), working=_failing_reader(RuntimeError("boom"))
    )
    assert _route(globs, "src/app.py") == _GEMINI


@pytest.mark.anyio
@pytest.mark.parametrize("working", [None, "other_key = 1\n"])
async def test_18_absent_working_side_does_not_fail_closed(working: str | None) -> None:
    globs = await _globs(trusted=_toml("deploy/*"), working=working)
    assert globs.globs == frozenset({"deploy/*"})
    assert _route(globs, "src/app.py") == _CODEX


@pytest.mark.anyio
async def test_malformed_trusted_config_routes_to_gemini() -> None:
    globs = await _globs(trusted="[[[")
    assert globs.trusted_unresolved
    assert _route(globs, "src/app.py") == _GEMINI


def test_unread_marker_routes_to_gemini() -> None:
    assert _route(GlobLoad(frozenset()), "src/app.py", marker=None) == _GEMINI


# ---------- Einstein msg-6482 advisory: path normalisation ---------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "deploy\\firewall.ps1",
        "./deploy/firewall.ps1",
        "/deploy/firewall.ps1",
        "Deploy/Firewall.PS1",
        "deploy//firewall.ps1",
    ],
)
async def test_paths_are_normalised_before_glob_matching(path: str) -> None:
    globs = await _globs(_toml("deploy/*.ps1"))
    assert _route(globs, path) == _GEMINI


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path", [N3_CONFIG_PATH.upper(), "./" + N3_CONFIG_PATH, "\\" + N3_CONFIG_PATH]
)
async def test_config_file_match_is_normalised(path: str) -> None:
    globs = await _globs(_toml())
    assert _route(globs, path) == _GEMINI


@pytest.mark.anyio
async def test_globs_are_normalised_too() -> None:
    globs = await _globs(_toml("Deploy\\*"))
    assert _route(globs, "deploy/x.ps1") == _GEMINI


def test_normalize_path() -> None:
    assert normalize_path(".\\A\\\\b/C.py") == "a/b/c.py"


# ---------- the decision's other outputs ---------------------------------- #


@pytest.mark.anyio
async def test_decision_carries_the_allowed_set_from_the_same_table() -> None:
    codex = route_tier(N3Request(False, ("a.py",), await _globs(_toml()), 10))
    gemini = route_tier(N3Request(True, ("a.py",), await _globs(_toml()), 10))
    assert codex.allowed_backends == frozenset({"codex", "gemini-fallback"})
    assert gemini.allowed_backends == frozenset({"gemini"})


def test_has_marker() -> None:
    assert has_marker(["bug", N3_SENSITIVE_MARKER])
    assert has_marker([" N3-Sensitive "])
    assert not has_marker(["bug", "n3"])


# ---------- msg-6573 (c): normalisation precedes BOTH matches -------------- #


@pytest.mark.anyio
async def test_c_normalisation_precedes_the_config_match_and_the_glob_match() -> None:
    """Each spelling only matches after normalisation; both matches see the normalised form."""
    empty = await _globs(_toml())
    assert _route(empty, ".\\" + N3_CONFIG_PATH.upper()) == _GEMINI  # config-file match
    listed = await _globs(_toml("Secrets/*"))
    assert _route(listed, "secrets\\KEY.pem") == _GEMINI  # glob match
    assert lexical_path_problem("secrets\\KEY.pem") is None  # neither was a boundary hit


# ---------- msg-6579: the PR-gate's lexical boundary ----------------------- #


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ("src/x.py", False),
        ("deleted/never/on/disk.py", False),
        ("../x.py", True),
        ("a/../../x.py", True),
        ("/etc/passwd", True),
        ("C:/Windows/x", True),
        ("c:\\x", True),
        ("", True),
    ],
)
def test_lexical_path_problem(path: str, bad: bool) -> None:
    assert (lexical_path_problem(path) is not None) is bad


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


def test_spawn_time_decision_is_fail_safe_gemini() -> None:
    from spirrow_mindwire.adapters.naysayer_sdk import design_time_tier_decision

    decision = design_time_tier_decision()
    assert decision.tier == _GEMINI
    assert decision.allowed_backends == frozenset({"gemini"})


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


# ---------- structure: one name, one place (§7.3 "names drifting apart") --- #

_SRC = _REPO / "src" / "spirrow_mindwire"
_NAMES = {
    "N3_SENSITIVE_MARKER": N3_SENSITIVE_MARKER,
    "N3_CONFIG_PATH": N3_CONFIG_PATH,
    "N3_SENSITIVE_PATHS_KEY": N3_SENSITIVE_PATHS_KEY,
    "NAYSAYER_GEMINI_TIER": NAYSAYER_GEMINI_TIER,
    "GEMINI_FALLBACK_BACKEND": principles.GEMINI_FALLBACK_BACKEND,
}


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


@pytest.mark.parametrize("name", sorted(_NAMES))
def test_no_src_file_restates_a_routing_name_as_a_literal(name: str) -> None:
    """Every routing name exists as a string literal ONLY in principles.py.

    A literal elsewhere (the routing function, the label/tag read, the config loader,
    an adapter) would be a second copy free to drift — exactly what §7.3's structure
    test exists to catch. Docstrings/comments may mention the name; only a string
    constant whose WHOLE value equals it counts.
    """
    value = _NAMES[name]
    owner = _SRC / "naysayer" / "principles.py"
    assert value in _string_literals(owner)
    offenders = [
        str(p.relative_to(_REPO))
        for p in _SRC.rglob("*.py")
        if p != owner and value in _string_literals(p)
    ]
    assert offenders == [], f"{name}={value!r} restated as a literal in {offenders}"


def test_routing_module_reads_the_constants() -> None:
    text = (_SRC / "naysayer" / "n3_routing.py").read_text(encoding="utf-8")
    for name in ("N3_SENSITIVE_MARKER", "N3_CONFIG_PATH", "N3_SENSITIVE_PATHS_KEY"):
        assert name in text


def test_operator_doc_names_match_the_constants() -> None:
    """docs/deploy.md is where an operator learns the names; it must spell them as the code."""
    doc = (_REPO / "docs" / "deploy.md").read_text(encoding="utf-8")
    for value in (
        N3_SENSITIVE_MARKER,
        N3_CONFIG_PATH,
        N3_SENSITIVE_PATHS_KEY,
        NAYSAYER_GEMINI_TIER,
    ):
        assert f"`{value}`" in doc, value


def test_this_repo_carries_a_resolvable_n3_config() -> None:
    """mindwire's own config parses and covers the routing code and its constants (msg-6475)."""
    import tomllib

    data = tomllib.loads((_REPO / N3_CONFIG_PATH).read_text(encoding="utf-8"))
    globs = data[N3_SENSITIVE_PATHS_KEY]
    assert isinstance(globs, list) and globs
    load = GlobLoad(frozenset(normalize_path(g) for g in globs))
    assert N3_CONFIG_PATH in globs  # msg-6573: listed even though matched unconditionally
    for path in (
        "src/spirrow_mindwire/naysayer/n3_routing.py",
        "src/spirrow_mindwire/naysayer/principles.py",
    ):
        assert _route(load, path) == _GEMINI, path


def test_conductor_hands_get_thread_tags_to_the_event() -> None:
    """The measured ``get_thread(mode="full")`` shape carries tags under ``thread``."""
    from spirrow_mindwire.conductor.core import _thread_tags

    assert _thread_tags({"thread": {"tags": ["n3-sensitive"]}, "messages": []}) == ("n3-sensitive",)
    assert _thread_tags({"thread": {"tags": []}}) == ()
    assert _thread_tags({"messages": []}) is None  # not read → the routing treats as marked
    assert _thread_tags({"thread": {"tags": [1]}}) is None
