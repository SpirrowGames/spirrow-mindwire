"""In-process MindWire MCP server: the 5 custom tools claude-code uses.

architecture.md §6.3 fixes the surface:

- ``mcp__mindwire__write_reply(content)`` — atomic write of the next
  message file. Hides the on-disk layout / seq numbering / filename
  scheme from claude-code.
- ``mcp__mindwire__read_file(path)`` — thin proxy to Phanthand
  ``/files/read``.
- ``mcp__mindwire__list_dir(path)`` — Phanthand ``/files/list``.
- ``mcp__mindwire__search(path, pattern)`` — Phanthand ``/files/search``.
- ``mcp__mindwire__file_info(path)`` — Phanthand ``/files/info``.

The four Phanthand-backed tools follow ``feedback_trust_llm_for_tool_errors``:
``PhanthandError`` becomes an ``isError=True`` tool result with the
server-side message preserved verbatim. The LLM gets to decide how to
react.

``write_reply`` is local-only — atomic-rename failures propagate up so
the watcher's retry / dead-letter layer can catch them; we don't dress
them as tool errors because there is no LLM-actionable recovery for a
filesystem fault.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml
from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)

from spirrow_mindwire.filesystem import ThreadDirLayout, atomic_write_text
from spirrow_mindwire.phanthand import PhanthandClient, PhanthandError
from spirrow_mindwire.schema import Participant


def _zero_padded_seq(seq: int) -> str:
    """Width-3 padding, expanding to len(str(seq)) on overflow (architecture.md §3.2)."""
    width = 3 if seq < 1000 else len(str(seq))
    return f"{seq:0{width}d}"


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def build_mindwire_tools(
    *,
    layout: ThreadDirLayout,
    next_seq: int,
    sender: Participant,
    recipient: Participant,
    phanthand_client: PhanthandClient,
    now: datetime | None = None,
) -> list[SdkMcpTool[Any]]:
    """Construct the 5 in-process custom tools for one watcher invocation.

    Each invocation builds a fresh tool list because the closure
    captures *this* invocation's seq / clock — the next invocation
    needs a different seq.

    *now* is injectable so tests can pin the timestamp; production code
    leaves it ``None`` and lets ``write_reply`` call ``datetime.now(UTC)``.
    """

    @tool(
        "write_reply",
        "Send your reply to the MindWire thread. Pass the markdown body "
        "as `content`. End your turn with exactly one call to this tool.",
        {"content": str},
    )
    async def write_reply(args: dict[str, Any]) -> dict[str, Any]:
        body: str = args["content"]
        ts = now if now is not None else datetime.now(UTC)
        msg_id = f"{layout.thread_id}/{_zero_padded_seq(next_seq)}"
        frontmatter: dict[str, Any] = {
            "schema_version": 1,
            "msg_id": msg_id,
            "seq": next_seq,
            "from": sender,
            "to": recipient,
            "created_at": _iso_z(ts),
        }
        yaml_block = yaml.safe_dump(
            frontmatter,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        content = f"---\n{yaml_block}---\n\n{body}\n"
        target = layout.message_path(next_seq, sender)
        atomic_write_text(target, content)
        return _ok(f"Reply written ({msg_id}).")

    @tool(
        "read_file",
        "Read the text content of a file via Phanthand. Read-only; cannot modify files.",
        {"path": str},
    )
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await phanthand_client.read_file(args["path"])
        except PhanthandError as e:
            return _error(f"read_file failed: {e}")
        return _ok(data.content)

    @tool(
        "list_dir",
        "List files and directories at the given path via Phanthand.",
        {"path": str},
    )
    async def list_dir(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await phanthand_client.list_directory(args["path"])
        except PhanthandError as e:
            return _error(f"list_dir failed: {e}")
        lines = [f"{'D' if entry.is_dir else 'F'} {entry.path}" for entry in data.entries]
        return _ok("\n".join(lines) if lines else "(empty)")

    @tool(
        "search",
        "Search for files matching a glob pattern under the given path "
        "via Phanthand. Example pattern: '**/*.py'.",
        {"path": str, "pattern": str},
    )
    async def search(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await phanthand_client.file_search(args["path"], args["pattern"])
        except PhanthandError as e:
            return _error(f"search failed: {e}")
        body = "\n".join(data.matches) if data.matches else "(no matches)"
        if data.truncated:
            body += f"\n... (truncated at {data.count} matches)"
        return _ok(body)

    @tool(
        "file_info",
        "Get metadata (size / mtime / kind) for a file via Phanthand.",
        {"path": str},
    )
    async def file_info(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await phanthand_client.file_info(args["path"])
        except PhanthandError as e:
            return _error(f"file_info failed: {e}")
        kind = "dir" if data.is_dir else "file"
        modified = data.modified.isoformat() if data.modified else "(unknown)"
        text = (
            f"path: {data.path}\n"
            f"kind: {kind}\n"
            f"size: {data.size}\n"
            f"modified: {modified}\n"
            f"readonly: {data.readonly}"
        )
        return _ok(text)

    return [write_reply, read_file, list_dir, search, file_info]


def build_mindwire_mcp_server(
    *,
    layout: ThreadDirLayout,
    next_seq: int,
    sender: Participant,
    recipient: Participant,
    phanthand_client: PhanthandClient,
    now: datetime | None = None,
) -> McpSdkServerConfig:
    """Build the in-process MCP server config for the SDK.

    The watcher passes the returned config under
    ``mcp_servers={"mindwire": ...}`` and lists the matching
    ``allowed_tools`` (``mcp__mindwire__write_reply``, ...) when calling
    :func:`invoke_claude_code`.
    """

    return create_sdk_mcp_server(
        name="mindwire",
        version="0.1.0",
        tools=build_mindwire_tools(
            layout=layout,
            next_seq=next_seq,
            sender=sender,
            recipient=recipient,
            phanthand_client=phanthand_client,
            now=now,
        ),
    )


__all__ = ["build_mindwire_mcp_server", "build_mindwire_tools"]
