"""Cross-checks the SYSTEM_PROMPT tool list against the live tool factory.

The two sources of truth for "which mcp__mindwire__* tools exist" —
``SYSTEM_PROMPT`` (where Claude reads them from) and
``build_mindwire_tools`` (where the SDK actually serves them from) —
must agree exactly. A drift in either direction is a bug:

- name only in SYSTEM_PROMPT → Claude tries to call a tool that doesn't
  exist; the SDK silently fails and the thread stalls.
- name only in build_mindwire_tools → Claude never learns the tool
  exists and won't use it.

These tests run on every CI build so a future PR adding (or renaming)
a tool can't ship without updating both ends in lock-step.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from spirrow_mindwire.claude_code import (
    SYSTEM_PROMPT,
    build_mindwire_tools,
)
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.phanthand import PhanthandClient

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
MCP_SERVER_NAME = "mindwire"
"""Must match the ``name=`` argument to ``create_sdk_mcp_server`` in
``mindwire_server.py`` — the SDK exposes tools as
``mcp__<server_name>__<tool_name>``.
"""

_FQN_RE = re.compile(rf"mcp__{MCP_SERVER_NAME}__(\w+)")


def _live_tool_names(tmp_path: Path) -> set[str]:
    """Return the set of bare tool names exposed by ``build_mindwire_tools``.

    The tool factory needs a layout / phanthand client / seq, but the
    name set is invariant in those — any plausible inputs work.
    """

    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    tools: list[Any] = build_mindwire_tools(
        layout=layout,
        next_seq=2,
        sender="claude-code",
        recipient="claude.ai",
        phanthand_client=AsyncMock(spec=PhanthandClient),
    )
    return {t.name for t in tools}


def _system_prompt_tool_names() -> set[str]:
    """Return the set of bare tool names referenced by SYSTEM_PROMPT."""
    return set(_FQN_RE.findall(SYSTEM_PROMPT))


def test_system_prompt_lists_every_live_tool(tmp_path: Path) -> None:
    """Every tool ``build_mindwire_tools`` serves must be named in SYSTEM_PROMPT.

    A live tool that the prompt doesn't mention is dead surface — Claude
    won't discover it and the SDK ends up serving an undocumented tool.
    """
    live = _live_tool_names(tmp_path)
    in_prompt = _system_prompt_tool_names()
    missing_from_prompt = live - in_prompt
    assert not missing_from_prompt, (
        f"SYSTEM_PROMPT does not advertise live MCP tools: {sorted(missing_from_prompt)}. "
        f"Add ``mcp__{MCP_SERVER_NAME}__<name>`` references to system_prompt.py."
    )


def test_system_prompt_does_not_advertise_missing_tools(tmp_path: Path) -> None:
    """Every tool name SYSTEM_PROMPT mentions must actually exist.

    A prompt name with no corresponding tool means Claude is told to
    call something the SDK can't route — which silently no-ops at the
    SDK layer and stalls the thread.
    """
    live = _live_tool_names(tmp_path)
    in_prompt = _system_prompt_tool_names()
    extra_in_prompt = in_prompt - live
    assert not extra_in_prompt, (
        f"SYSTEM_PROMPT references tools that build_mindwire_tools does not serve: "
        f"{sorted(extra_in_prompt)}. Either add the tool to mindwire_server.py or "
        "remove the dangling reference."
    )
