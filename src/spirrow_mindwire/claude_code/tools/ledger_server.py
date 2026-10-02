"""In-process MCP server ``mindwire-ledger``: the implementer's only Magickit tools.

Design: thread ``T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down`` Bohr msg-5296 /
msg-5300, endorsed by Einstein msg-5301. The logic lives in
:mod:`spirrow_mindwire.magickit.ledger_notes`; this module only turns it into two SDK tools.

The session sees exactly :data:`LEDGER_TOOL_NAMES` and nothing else from Magickit. It is
in-process (``type: "sdk"``), so ``connect()`` returning still means every tool is available
(T43's ``spawn.ready`` premise, pinned in ``tests/test_role_tool_surface.py``), and it opens no
path to another agent: both tools act on the ledger of the thread's own project, never on a
session or a thread.

Errors are returned as ``isError`` tool results with the reason, after ``ledger.note_failed`` has
been emitted by :class:`~spirrow_mindwire.magickit.ledger_notes.LedgerNotes`.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from ...magickit.ledger_notes import LedgerError, LedgerNotes

LEDGER_SERVER_NAME = "mindwire-ledger"
LEDGER_TOOL_GET = "ledger_get_task"
LEDGER_TOOL_APPEND = "ledger_append_note"
LEDGER_TOOL_NAMES: frozenset[str] = frozenset({LEDGER_TOOL_GET, LEDGER_TOOL_APPEND})
"""Every tool the server exposes. ``tests/test_ledger_server.py`` pins equality, not exclusion."""

LEDGER_ALLOWED_TOOLS: tuple[str, ...] = tuple(
    f"mcp__{LEDGER_SERVER_NAME}__{name}" for name in sorted(LEDGER_TOOL_NAMES)
)
"""The SDK-qualified names, for ``allowed_tools``."""


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _error(exc: LedgerError) -> dict[str, Any]:
    text = f"ledger error ({exc.reason.value}): {exc}"
    return {"content": [{"type": "text", "text": text}], "isError": True}


def build_ledger_tools(notes: LedgerNotes) -> list[SdkMcpTool[Any]]:
    """The two tools, bound to one session's :class:`LedgerNotes`."""

    @tool(
        LEDGER_TOOL_GET,
        "Read one Magickit task (name, status, notes, phase) from this thread's project ledger. "
        "Read-only.",
        {"task_id": str},
    )
    async def ledger_get_task(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _ok(await notes.get_task(args.get("task_id", "")))
        except LedgerError as exc:
            return _error(exc)

    @tool(
        LEDGER_TOOL_APPEND,
        "Append text to the notes of a Magickit task in this thread's project ledger. The text is "
        "added at the end under a provenance header; existing notes are kept. Notes cannot be "
        "replaced or deleted — to correct a note, append the correction.",
        {"task_id": str, "text": str},
    )
    async def ledger_append_note(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _ok(await notes.append_note(args.get("task_id", ""), args.get("text", "")))
        except LedgerError as exc:
            return _error(exc)

    return [ledger_get_task, ledger_append_note]


def build_ledger_mcp_server(notes: LedgerNotes) -> McpSdkServerConfig:
    """The in-process server config, to be passed as ``mcp_servers={LEDGER_SERVER_NAME: ...}``."""
    return create_sdk_mcp_server(
        name=LEDGER_SERVER_NAME, version="0.1.0", tools=build_ledger_tools(notes)
    )


__all__ = [
    "LEDGER_ALLOWED_TOOLS",
    "LEDGER_SERVER_NAME",
    "LEDGER_TOOL_APPEND",
    "LEDGER_TOOL_GET",
    "LEDGER_TOOL_NAMES",
    "build_ledger_mcp_server",
    "build_ledger_tools",
]
