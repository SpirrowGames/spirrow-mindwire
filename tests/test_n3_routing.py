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
import re
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.naysayer import principles
from spirrow_mindwire.naysayer.n3_routing import (
    DEFAULT_GLOBS,
    GlobLoad,
    N3Request,
    TierDecision,
    classify_n3,
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
    # The §7.3 classification itself (what the old rule routed where). Since ADR-14 §10
    # ``route_tier`` sends every request to the codex tier; ``test_route_tier_*`` below pin that.
    return classify_n3(
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
    assert globs.globs == DEFAULT_GLOBS  # "[]" adds nothing beyond the org defaults (ADR-14 §8)
    assert not globs.repo_config_absent
    assert _route(globs, "src/app.py") == _CODEX


# 05 / 06 changed meaning with ADR-14 §8 (2026-11-07, Takahito): a trusted side with no key or
# no file is judged by the org defaults instead of sending everything to Gemini.


@pytest.mark.anyio
async def test_05_key_missing_at_base_uses_the_org_defaults() -> None:
    globs = await _globs("other_key = 1\n")
    assert not globs.trusted_unresolved
    assert globs.repo_config_absent
    assert globs.globs == DEFAULT_GLOBS
    assert _route(globs, "src/app.py") == _CODEX
    assert _route(globs, "deploy/app.service") == _GEMINI


@pytest.mark.anyio
async def test_06_file_missing_at_base_uses_the_org_defaults() -> None:
    globs = await _globs(None)
    assert not globs.trusted_unresolved
    assert globs.repo_config_absent
    assert globs.globs == DEFAULT_GLOBS
    assert _route(globs, "src/app.py") == _CODEX
    assert _route(globs, ".github/workflows/ci.yml") == _GEMINI


@pytest.mark.anyio
async def test_06b_defaults_only_decision_says_so() -> None:
    """The reason names the defaults-only case, so the PR-gate's routing log line shows it."""
    absent = route_tier(
        N3Request(marker=False, paths=("src/app.py",), globs=await _globs(None), prompt_chars=1)
    )
    present = route_tier(
        N3Request(marker=False, paths=("src/app.py",), globs=await _globs(_toml()), prompt_chars=1)
    )
    assert absent.tier == present.tier == _CODEX
    assert "org default globs only" in absent.reason
    assert present.reason == "codex tier"


@pytest.mark.anyio
async def test_06c_head_adding_a_file_where_base_has_none_still_counts() -> None:
    """The working side may only add, also on top of the defaults alone."""
    globs = await _globs(trusted=None, working=_toml("keys/*"))
    assert globs.repo_config_absent
    assert _route(globs, "keys/signing.txt") == _GEMINI


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "deploy/lexora.service",
        "deploy/systemd/spirrow-conclair-backup.timer",
        "spec/deploy/per-project-deploy-rule.md",
        ".github/workflows/ci.yml",
        "spirrow-magickit-mcp.service",
        ".env",
        ".env.example",
        "config/prod.env",
        "tests/test_secrets.py",
        "src/app/credentials.json",
        "certs/server.pem",
        "certs/server.key",
        "home/.ssh/authorized_keys",
        "src/magickit/auth/jwt.py",
        "app/auth.py",
        "scripts/init_google_auth.py",
        "etc/sudoers.d/sgadmin",
        "etc/apparmor.d/bwrap",
        "infra/firewall.ps1",
        "docs/tailscale-serve.md",
        "ops/vaultwarden/compose.yml",
    ],
)
async def test_org_defaults_route_common_sensitive_paths_to_gemini(path: str) -> None:
    globs = await _globs(_toml())  # the repo adds nothing: the defaults alone decide
    assert _route(globs, path) == _GEMINI, path


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "src/lexora/services/cost_tracker.py",
        "tests/test_author.py",
        "README.md",
        "docs/architecture.md",
        "pyproject.toml",
        "src/magickit/dashboard/templates/prs.html",
    ],
)
async def test_org_defaults_leave_ordinary_source_on_codex(path: str) -> None:
    globs = await _globs(None)
    assert _route(globs, path) == _CODEX, path


def test_org_defaults_live_in_principles_only() -> None:
    """One copy (principles.py): the routing module reads it, and every default is normalised."""
    assert principles.N3_DEFAULT_SENSITIVE_PATHS
    expected = frozenset(normalize_path(g) for g in principles.N3_DEFAULT_SENSITIVE_PATHS)
    assert expected == DEFAULT_GLOBS
    text = (_SRC / "naysayer" / "n3_routing.py").read_text(encoding="utf-8")
    assert "N3_DEFAULT_SENSITIVE_PATHS" in text


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
    assert globs.globs == frozenset({"deploy/*", "keys/*"}) | DEFAULT_GLOBS
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
async def test_16b_working_side_negation_pattern_cannot_cancel_a_trusted_match() -> None:
    """PR-gate on #456 (security): the union is of MATCHED PATH SETS, not a pattern list.

    A gitignore-style ``!pattern`` appended on the working side (design time: the
    config file itself is not referenced, so rule 2 does not fire) must not strip a
    trusted match. ADR-14 §7.3 pins: no exclusion syntax; ``!`` is a literal.
    """
    # ``signing.txt``, not ``secret.pem``: the org defaults (ADR-14 §8) already match the latter,
    # which would make the second half pass for the wrong reason.
    globs = await _globs(trusted=_toml("keys/*"), working=_toml("keys/*", "!keys/signing.txt"))
    assert _route(globs, "keys/signing.txt") == _GEMINI
    # the `!` pattern is a literal: it only ever ADDS a match, for a path spelt with `!`
    globs = await _globs(trusted=_toml(), working=_toml("!keys/signing.txt"))
    assert _route(globs, "keys/signing.txt") == _CODEX
    assert _route(globs, "!keys/signing.txt") == _GEMINI
    # nor can a `!` pattern cancel an org-default match
    globs = await _globs(trusted=_toml(), working=_toml("!keys/secret.pem"))
    assert _route(globs, "keys/secret.pem") == _GEMINI


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
    assert globs.globs == frozenset({"deploy/*"}) | DEFAULT_GLOBS
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
    n3 = route_tier(N3Request(True, ("a.py",), await _globs(_toml()), 10))
    # ADR-14 §10: codex only, N-3 included; ``gemini-fallback`` is no longer accepted.
    assert codex.allowed_backends == n3.allowed_backends == frozenset({"codex"})
    assert classify_n3(N3Request(True, ("a.py",), await _globs(_toml()), 10)).allowed_backends == (
        frozenset({"gemini"})
    )


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


def test_design_time_adapter_routes_to_codex_even_with_nothing_read() -> None:
    """With no turn inputs yet (spawn time) the old rule chose Gemini (marker not read);
    since ADR-14 §10 the tier is codex and the reason keeps the classification."""
    from spirrow_mindwire.adapters.naysayer_sdk import design_time_tier_decision

    decision = design_time_tier_decision()
    assert decision.tier == _CODEX
    assert decision.allowed_backends == frozenset({"codex"})
    assert "N-3 classified" in decision.reason and "marker not read" in decision.reason


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
    assert principles.CODEX_OUTPUT_RESERVE_TOKENS == 65_536
    assert CODEX_TIER_PROMPT_CHAR_LIMIT == 145_536
    assert CODEX_TIER_PROMPT_CHAR_LIMIT == (
        principles.CODEX_CONTEXT_WINDOW_TOKENS
        - principles.CODEX_HARNESS_RESERVE_TOKENS
        - principles.CODEX_OUTPUT_RESERVE_TOKENS
    )


def test_output_reserve_covers_the_named_naysayer_requests() -> None:
    from spirrow_mindwire.adapters import naysayer_lexora
    from spirrow_mindwire.naysayer import pr_review, preflight

    for value in (
        pr_review.DEFAULT_REVIEW_MAX_TOKENS,
        pr_review._ADR_POINTER_MAX_TOKENS,
        naysayer_lexora._DEFAULT_MAX_TOKENS,
        preflight.PREFLIGHT_MAX_TOKENS,
    ):
        assert value <= principles.CODEX_OUTPUT_RESERVE_TOKENS


def _int_literal(value: ast.expr | None) -> int | None:
    if isinstance(value, ast.Constant) and type(value.value) is int:
        return value.value
    return None


def _max_tokens_literals(src_root: Path) -> list[tuple[str, int, int]]:
    """Every int literal bound to a ``*max_tokens*`` name, keyword argument or parameter default."""
    found: list[tuple[str, int, int]] = []

    def hit(path: Path, name: str, lineno: int, value: ast.expr | None) -> None:
        literal = _int_literal(value)
        if literal is not None and "max_tokens" in name.lower() and not name.startswith("CODEX_"):
            found.append((str(path), lineno, literal))

    for path in sorted(src_root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        hit(path, target.id, node.lineno, node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                hit(path, node.target.id, node.lineno, node.value)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg is not None:
                        hit(path, kw.arg, node.lineno, kw.value)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                positional = node.args.posonlyargs + node.args.args
                padded: list[ast.expr | None] = [None] * (len(positional) - len(node.args.defaults))
                pairs = zip(
                    positional + node.args.kwonlyargs,
                    padded + list(node.args.defaults) + list(node.args.kw_defaults),
                    strict=True,
                )
                for arg, default in pairs:
                    hit(path, arg.arg, node.lineno, default)
    return found


def test_output_reserve_covers_every_max_tokens_literal_in_src() -> None:
    """Discovers request budgets instead of listing them (Einstein msg-6613 advisory 1).

    A new request type whose ``max_tokens`` exceeds the codex output reserve reds this test
    without anyone having to remember to add it to an import list.
    """
    src_root = Path(principles.__file__).resolve().parents[1]
    found = _max_tokens_literals(src_root)
    assert len(found) >= 4, found  # the four named constants above are discovered
    over = [f for f in found if f[2] > principles.CODEX_OUTPUT_RESERVE_TOKENS]
    assert over == [], over


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (principles.NAYSAYER_MODEL_TIER, principles.CODEX_OUTPUT_RESERVE_TOKENS),
        (principles.NAYSAYER_GEMINI_TIER, 65536),
    ],
)
async def test_pass_one_budget_is_clamped_on_the_codex_tier_only(tier: str, expected: int) -> None:
    from spirrow_mindwire.naysayer.pr_review import DiffView, NaysayerPrReviewDriver

    sent: list[int] = []

    class _Lexora:
        async def chat_completion(self, *, max_tokens: int, **_: object) -> object:
            sent.append(max_tokens)
            raise RuntimeError("stop after recording the request")

    driver = NaysayerPrReviewDriver(lexora=_Lexora(), github=object())  # type: ignore[arg-type]
    view = DiffView(text="diff", original_chars=4, limit=100, warn_threshold=90)
    await driver._run_two_passes(view, "o/r#1", tier=tier, n3_routed=True)
    assert max(sent) == expected


# ---------- output reserve at 65,536 and the band it creates (Bohr msg-7759) ----- #


class _RecordingLexora:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat_completion(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        raise RuntimeError("stop after recording the request")


async def _sent_requests(
    tier: str, *, max_tokens: int | None = None, trace_id: str | None = None
) -> list[dict[str, object]]:
    from spirrow_mindwire.naysayer.pr_review import DiffView, NaysayerPrReviewDriver

    lexora = _RecordingLexora()
    kwargs = {} if max_tokens is None else {"max_tokens": max_tokens}
    driver = NaysayerPrReviewDriver(lexora=lexora, github=object(), **kwargs)  # type: ignore[arg-type]
    view = DiffView(text="diff", original_chars=4, limit=100, warn_threshold=90)
    await driver._run_two_passes(view, "o/r#1", tier=tier, n3_routed=True, trace_id=trace_id)
    return lexora.calls


@pytest.mark.anyio
async def test_default_budget_reaches_the_request_lexora_would_fall_back_with() -> None:
    """The codex-tier request is what Lexora hands to Gemini on fallback (msg-7709).

    This pins the mindwire side only; that Lexora forwards the limit was read in its code.
    """
    from spirrow_mindwire.naysayer.pr_review import DEFAULT_REVIEW_MAX_TOKENS

    calls = await _sent_requests(principles.NAYSAYER_MODEL_TIER)
    assert DEFAULT_REVIEW_MAX_TOKENS == 65_536
    assert max(int(str(c["max_tokens"])) for c in calls) == 65_536


@pytest.mark.anyio
async def test_a_larger_configured_budget_is_clamped_on_the_codex_tier_only() -> None:
    codex = await _sent_requests(principles.NAYSAYER_MODEL_TIER, max_tokens=100_000)
    gemini = await _sent_requests(principles.NAYSAYER_GEMINI_TIER, max_tokens=100_000)
    assert max(int(str(c["max_tokens"])) for c in codex) == principles.CODEX_OUTPUT_RESERVE_TOKENS
    assert max(int(str(c["max_tokens"])) for c in gemini) == 100_000


@pytest.mark.anyio
async def test_both_passes_carry_the_band_trace_so_it_counts_one_review() -> None:
    from spirrow_mindwire.naysayer.n3_routing import codex_limit_band_trace_id

    trace = codex_limit_band_trace_id()
    calls = await _sent_requests(principles.NAYSAYER_GEMINI_TIER, trace_id=trace)
    assert [c["trace_id"] for c in calls] == [trace, trace]


def _band_decision(prompt_chars: int, *, globs: frozenset[str] = frozenset()) -> TierDecision:
    return classify_n3(
        N3Request(
            marker=False, paths=("src/app.py",), globs=GlobLoad(globs), prompt_chars=prompt_chars
        )
    )


@pytest.mark.parametrize(
    ("prompt_chars", "tier", "in_band"),
    [
        (CODEX_TIER_PROMPT_CHAR_LIMIT, principles.NAYSAYER_MODEL_TIER, False),
        (CODEX_TIER_PROMPT_CHAR_LIMIT + 1, principles.NAYSAYER_GEMINI_TIER, True),
        (principles.CODEX_TIER_PRE_RAISE_PROMPT_CHAR_LIMIT, principles.NAYSAYER_GEMINI_TIER, True),
        (
            principles.CODEX_TIER_PRE_RAISE_PROMPT_CHAR_LIMIT + 1,
            principles.NAYSAYER_GEMINI_TIER,
            False,
        ),
    ],
)
def test_band_boundaries(prompt_chars: int, tier: str, in_band: bool) -> None:
    decision = _band_decision(prompt_chars)
    assert decision.tier == tier
    assert decision.in_codex_limit_band is in_band


def test_an_n3_prompt_inside_the_band_is_not_counted() -> None:
    size = CODEX_TIER_PROMPT_CHAR_LIMIT + 1
    decision = _band_decision(size, globs=frozenset({"src/*"}))
    assert decision.tier == principles.NAYSAYER_GEMINI_TIER
    assert decision.in_codex_limit_band is False


def test_band_trace_id_is_a_ulid_lexora_accepts() -> None:
    from spirrow_mindwire.naysayer.n3_routing import (
        codex_limit_band_trace_id,
        is_codex_limit_band_trace_id,
    )
    from spirrow_mindwire.ulid_util import new_ulid

    # Copied from spirrow-lexora src/lexora/services/trace.py ULID_RE: a value it rejects is
    # stored as NULL, and the marked row could never be counted.
    lexora_ulid = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    trace = codex_limit_band_trace_id()
    assert lexora_ulid.fullmatch(trace)
    assert is_codex_limit_band_trace_id(trace)
    assert not is_codex_limit_band_trace_id(new_ulid())
    assert not is_codex_limit_band_trace_id(None)


def test_band_count_counts_requests_and_leaves_out_shadow_and_unmarked_rows() -> None:
    from spirrow_mindwire.naysayer.codex_band_count import count_band
    from spirrow_mindwire.naysayer.n3_routing import codex_limit_band_trace_id
    from spirrow_mindwire.ulid_util import new_ulid

    one, two = codex_limit_band_trace_id(), codex_limit_band_trace_id()
    rows: list[dict[str, Any]] = [
        {"trace_id": one, "endpoint": "/v1/chat/completions", "cost_usd": 1.0, "timestamp": "t3"},
        {"trace_id": one, "endpoint": "/v1/chat/completions", "cost_usd": 0.5, "timestamp": "t2"},
        {"trace_id": one, "endpoint": "shadow", "cost_usd": 9.0, "timestamp": "t2"},
        {"trace_id": two, "endpoint": "/v1/messages", "cost_usd": None, "timestamp": "t1"},
        {"trace_id": new_ulid(), "endpoint": "/v1/messages", "cost_usd": 7.0, "timestamp": "t0"},
        {"trace_id": None, "endpoint": "/v1/messages", "cost_usd": 7.0, "timestamp": "t0"},
    ]
    result = count_band(rows, limit=100)
    assert (result.reviews, result.rows, result.cost_usd) == (2, 3, 1.5)
    assert (result.oldest, result.truncated) == ("t0", False)
    assert count_band(rows, limit=len(rows)).truncated is True
    assert "truncated=true" in count_band(rows, limit=len(rows)).render()
    assert count_band([], limit=10).render().startswith("reviews=0 rows=0")


def test_band_count_cli_fails_loudly_when_the_ledger_read_stalls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from spirrow_mindwire.naysayer import codex_band_count

    async def stalled(limit: int) -> list[dict[str, Any]]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(codex_band_count, "_read", stalled)
    monkeypatch.setattr(codex_band_count, "_READ_TIMEOUT_SECONDS", 0.01)

    assert codex_band_count.run(10) == 1
    captured = capsys.readouterr()
    assert "reviews=" not in captured.out
    assert "exceeded 0.01s" in captured.err


# ---------- ADR-14 §10: route_tier always returns the codex tier ------------ #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("marker", "paths", "trusted", "prompt_chars", "cause"),
    [
        (True, ("src/app.py",), _toml(), 1_000, "marker"),
        (None, ("src/app.py",), _toml(), 1_000, "marker not read"),
        (False, ("deploy/app.service",), _toml(), 1_000, "matches"),
        (False, (N3_CONFIG_PATH,), _toml(), 1_000, "touches"),
        (False, ("../x.py",), _toml(), 1_000, "repository boundary"),
        (False, ("src/app.py",), "not = = toml", 1_000, "unresolved"),
        (False, ("src/app.py",), _toml(), CODEX_TIER_PROMPT_CHAR_LIMIT + 1, "codex tier limit"),
    ],
)
async def test_route_tier_sends_every_n3_case_to_codex(
    marker: bool | None, paths: tuple[str, ...], trusted: str, prompt_chars: int, cause: str
) -> None:
    request = N3Request(
        marker=marker, paths=paths, globs=await _globs(trusted), prompt_chars=prompt_chars
    )
    assert classify_n3(request).tier == _GEMINI  # what the old rule did
    decision = route_tier(request)
    assert decision.tier == _CODEX
    assert decision.allowed_backends == frozenset({"codex"})
    assert decision.in_codex_limit_band is False
    assert decision.reason.startswith(
        "codex tier (N-3 classified, routed to codex per ADR-14 §10: "
    )
    assert cause in decision.reason


@pytest.mark.anyio
async def test_route_tier_leaves_a_plain_codex_decision_as_it_was() -> None:
    request = N3Request(False, ("src/app.py",), await _globs(_toml("secrets/*")), 1_000)
    assert route_tier(request) == classify_n3(request)
