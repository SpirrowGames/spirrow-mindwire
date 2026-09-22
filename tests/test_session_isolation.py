"""The isolation policy is defined once, and every SDK adapter uses that one.

The behavioural pins live next to each adapter (``test_claude_code_sdk_adapter``
/ ``test_naysayer_sdk_adapter`` assert the spawned options carry the isolation).
What those cannot catch is the drift this module exists to prevent: a *fourth*
adapter, written later, that simply never mentions isolation. Nothing would
fail — it would just quietly inherit the host the way the proposer did until
2026-09-15, when an inherited claude.ai connector parked a live thread.

So the check here reads the source, and it is written so that **silence fails**:
every module under ``adapters/`` must call :func:`session_isolation_kwargs`
unless it is named in :data:`_NOT_SDK_BACKED` with a reason. A new file is
therefore isolated by default, and opting out is an edit a reviewer sees.

An earlier version of this test asked instead "which modules contain the text
``ClaudeAgentOptions(``?" and required *those* to use the helper. That inverted
the burden: the adapter this test most needs to catch — one that omits
isolation entirely — is also the one least likely to match, so it would never
have entered the set being checked. The gate on #276 rejected it for exactly
that (``invariant``, ``tests/test_session_isolation.py:45``).

Both failure directions were run as negative controls before this was committed:
a probe module that builds ``ClaudeAgentOptions`` without the helper reds
``test_every_adapter_module_isolates_or_declares_why_not``; the same probe
listed in ``_NOT_SDK_BACKED`` reds ``test_exemptions_build_no_sdk_session``;
and a probe that only names the helper in a comment reds the first test too,
which a substring check would have passed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from spirrow_mindwire.adapters._session_isolation import session_isolation_kwargs

_ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "src" / "spirrow_mindwire" / "adapters"

#: Modules that spawn no SDK session, and why. Membership here is not taken on
#: trust — ``test_exemptions_build_no_sdk_session`` re-derives it from the AST,
#: so a real adapter cannot be waved through by adding its name.
_NOT_SDK_BACKED: dict[str, str] = {
    "__init__.py": "package marker; defines no adapter",
    "_sdk_result.py": "parses an SDK result object; spawns nothing",
    "_session_isolation.py": "this module IS the policy",
    "_cli_selection.py": (
        "builds the model / cli_path kwargs for an adapter to splat; constructs "
        "no ClaudeAgentOptions and spawns nothing — isolation is applied by the "
        "adapter that USES it, next to its own session_isolation_kwargs() call"
    ),
    "_sdk_job_hook.py": (
        "SDK-adjacent process-lifecycle hook (Windows Job Objects); wraps the "
        "SDK's transport but builds no ClaudeAgentOptions of its own — "
        "isolation is applied by the adapter that USES the hook "
        "(implementer.py) via session_isolation_kwargs()"
    ),
    "naysayer_lexora.py": "reaches Lexora over HTTP; builds no SDK session",
}


def _adapter_modules() -> list[Path]:
    return sorted(_ADAPTERS_DIR.glob("*.py"))


def _uses_sdk_options(tree: ast.AST) -> bool:
    """True if the module imports or calls ``ClaudeAgentOptions``.

    AST, not a text search: a docstring that merely names the class is not a
    use, and this must not be satisfiable by mentioning the word.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "ClaudeAgentOptions" for alias in node.names
        ):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "ClaudeAgentOptions":
                return True
    return False


def _calls_isolation_helper(tree: ast.AST) -> bool:
    """True if the module actually calls ``session_isolation_kwargs``.

    AST for the same reason as :func:`_uses_sdk_options`: a substring check is
    satisfied by a module that only *mentions* the name — in a comment, a
    docstring, or a dead string constant — while omitting the call. The gate on
    #276 named that hole (``invariant``, round 4), and it is the same shape as
    the one round 2 named: a guard whose evidence is text rather than code.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "session_isolation_kwargs":
                return True
    return False


def test_kwargs_are_the_isolation_pair() -> None:
    assert session_isolation_kwargs() == {"setting_sources": [], "strict_mcp_config": True}


def test_each_call_gets_its_own_setting_sources_list() -> None:
    """A shared module constant would hand every adapter the same mutable list."""
    first = session_isolation_kwargs()
    second = session_isolation_kwargs()
    assert first["setting_sources"] is not second["setting_sources"]
    first["setting_sources"].append("user")
    assert second["setting_sources"] == []


@pytest.mark.parametrize("path", _adapter_modules(), ids=lambda p: p.name)
def test_every_adapter_module_isolates_or_declares_why_not(path: Path) -> None:
    if path.name in _NOT_SDK_BACKED:
        pytest.skip(f"declared not SDK-backed: {_NOT_SDK_BACKED[path.name]}")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _calls_isolation_helper(tree), (
        f"{path.name} builds a role session without calling session_isolation_kwargs(). "
        f"Call it, or — if this module spawns no SDK session — add it to "
        f"_NOT_SDK_BACKED with the reason. (Naming it in a comment is not calling it.)"
    )


@pytest.mark.parametrize("name", sorted(_NOT_SDK_BACKED), ids=str)
def test_exemptions_build_no_sdk_session(name: str) -> None:
    """The opt-out list cannot be used to park a real adapter in."""
    tree = ast.parse((_ADAPTERS_DIR / name).read_text(encoding="utf-8"))
    assert not _uses_sdk_options(tree), (
        f"{name} is listed in _NOT_SDK_BACKED but imports or constructs "
        f"ClaudeAgentOptions — it spawns a session and must isolate it"
    )


def test_no_adapter_spells_the_options_out_by_hand() -> None:
    """One definition means one definition — not one plus a re-spelling of it."""
    for path in _adapter_modules():
        if path.name == "_session_isolation.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            spelled = (isinstance(node, ast.keyword) and node.arg == "strict_mcp_config") or (
                isinstance(node, ast.Constant) and node.value == "strict_mcp_config"
            )
            if spelled:
                raise AssertionError(
                    f"{path.name} names strict_mcp_config directly; "
                    f"use session_isolation_kwargs() so the policy stays in one place"
                )
