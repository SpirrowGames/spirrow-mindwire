"""The isolation policy is defined once, and every SDK adapter uses that one.

The behavioural pins live next to each adapter (``test_claude_code_sdk_adapter``
/ ``test_naysayer_sdk_adapter`` assert the spawned options carry the isolation).
What those cannot catch is the drift this module exists to prevent: a *fourth*
adapter, written later, that builds ``ClaudeAgentOptions`` and simply never
mentions isolation. Nothing would fail — it would just quietly inherit the host
the way the proposer did until 2026-09-15, when an inherited claude.ai connector
parked a live thread. So the check here is over the source: whoever builds the
options must go through :func:`session_isolation_kwargs`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from spirrow_mindwire.adapters._session_isolation import session_isolation_kwargs

_ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "src" / "spirrow_mindwire" / "adapters"


def test_kwargs_are_the_isolation_pair() -> None:
    assert session_isolation_kwargs() == {"setting_sources": [], "strict_mcp_config": True}


def test_each_call_gets_its_own_setting_sources_list() -> None:
    """A shared module constant would hand every adapter the same mutable list."""
    first = session_isolation_kwargs()
    second = session_isolation_kwargs()
    assert first["setting_sources"] is not second["setting_sources"]
    first["setting_sources"].append("user")
    assert second["setting_sources"] == []


def _modules_naming(symbol: str) -> set[str]:
    hits = set()
    for path in _ADAPTERS_DIR.glob("*.py"):
        if symbol in path.read_text(encoding="utf-8"):
            hits.add(path.name)
    return hits


def test_every_adapter_building_options_goes_through_the_helper() -> None:
    builders = _modules_naming("ClaudeAgentOptions(") - {"_session_isolation.py"}
    # Guard the guard: if the constructor is ever renamed, this set empties and
    # the assertion below passes vacuously.
    assert builders, "no adapter appears to build ClaudeAgentOptions — has it been renamed?"
    users = _modules_naming("session_isolation_kwargs()")
    assert builders <= users, (
        f"these adapters build ClaudeAgentOptions without session_isolation_kwargs(): "
        f"{sorted(builders - users)}"
    )


def test_no_adapter_spells_the_options_out_by_hand() -> None:
    """One definition means one definition — not one plus a re-spelling of it."""
    for path in _ADAPTERS_DIR.glob("*.py"):
        if path.name == "_session_isolation.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "strict_mcp_config":
                raise AssertionError(
                    f"{path.name} passes strict_mcp_config directly; "
                    f"use session_isolation_kwargs() so the policy stays in one place"
                )
            if isinstance(node, ast.Constant) and node.value == "strict_mcp_config":
                raise AssertionError(
                    f"{path.name} names strict_mcp_config in a dict literal; "
                    f"use session_isolation_kwargs() so the policy stays in one place"
                )
