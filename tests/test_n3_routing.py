"""ADR-14 §7.3 / §7.5 / §7.6 — the naysayer tier decision and its config loader.

These are the tests that guard the decision ITSELF (§7.6: the attestation derives
its accepted set from the same decision, so it cannot catch a wrong decision; §7.9
step 4 requires this suite). Numbering follows the thread's agreed list
(T-D8-codex-backend-adr14-15-amendment, Bohr msg-6475 1-9, msg-6477 10-13,
msg-6479 14-18; Einstein msg-6480 / msg-6482).

Design-time cases (10-13, 15) exercise ``load_n3_globs`` + ``route_tier`` with
the design-time readers' semantics (trusted = remote default branch, working =
the prompt builder's source). The design-time call site and tests 20-26, 31, 32
are in ``tests/test_n3_design_time.py``.
PR-gate tests 19 and 27-30 are in ``tests/test_pr_review_driver.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from spirrow_mindwire.naysayer import principles
from spirrow_mindwire.naysayer.n3_routing import (
    GlobLoad,
    N3Request,
    has_marker,
    lexical_path_problem,
    load_n3_globs,
    normalize_path,
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


# ---------- design-time call site ----------------------------------------- #


def test_design_time_adapter_is_fail_safe_gemini_until_wired() -> None:
    """With no turn inputs yet (spawn time), the design-time tier is Gemini-only."""
    from spirrow_mindwire.adapters.naysayer_sdk import design_time_tier_decision

    decision = design_time_tier_decision()
    assert decision.tier == _GEMINI
    assert decision.allowed_backends == frozenset({"gemini"})


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
        # msg-6588: holds PROMPT_ASSETS, so widening it is a Gemini-reviewed change.
        "src/spirrow_mindwire/naysayer/n3_design_time.py",
        "src/spirrow_mindwire/naysayer/principles.py",
    ):
        assert _route(load, path) == _GEMINI, path


# ---------- codex limit derivation (thread msg-6612, Einstein msg-6613) ----- #


def test_codex_limit_is_pinned_from_the_three_terms() -> None:
    assert principles.CODEX_CONTEXT_WINDOW_TOKENS == 272_000
    assert principles.CODEX_HARNESS_RESERVE_TOKENS == 60_928
    assert principles.CODEX_OUTPUT_RESERVE_TOKENS == 32_000
    assert CODEX_TIER_PROMPT_CHAR_LIMIT == 179_072
    assert CODEX_TIER_PROMPT_CHAR_LIMIT == (
        principles.CODEX_CONTEXT_WINDOW_TOKENS
        - principles.CODEX_HARNESS_RESERVE_TOKENS
        - principles.CODEX_OUTPUT_RESERVE_TOKENS
    )


def test_output_reserve_covers_the_named_naysayer_requests() -> None:
    from spirrow_mindwire.adapters import naysayer_lexora
    from spirrow_mindwire.naysayer import pr_review, preflight

    for value in (
        pr_review._DEFAULT_MAX_TOKENS,
        pr_review._ADR_POINTER_MAX_TOKENS,
        naysayer_lexora._DEFAULT_MAX_TOKENS,
        preflight.PREFLIGHT_MAX_TOKENS,
    ):
        assert value <= principles.CODEX_OUTPUT_RESERVE_TOKENS
