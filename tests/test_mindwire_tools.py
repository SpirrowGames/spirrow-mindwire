"""Tests for the in-process MindWire MCP tool surface.

Each tool is exercised directly through its SdkMcpTool ``handler`` —
wiring it through a real SDK session would require the claude CLI
binary, which is out of scope here. Phanthand is mocked with
``AsyncMock``; the filesystem uses ``tmp_path``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from claude_agent_sdk import SdkMcpTool

from spirrow_mindwire.claude_code import (
    build_mindwire_mcp_server,
    build_mindwire_tools,
)
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.phanthand import (
    FileExistsData,
    FileInfoData,
    FileListData,
    FileListEntry,
    FileReadData,
    FileSearchData,
    PhanthandAPIError,
    PhanthandClient,
)

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _layout(base: Path) -> ThreadDirLayout:
    return ThreadDirLayout(base_dir=base, thread_id=ULID_A)


def _make_tools(
    base: Path,
    next_seq: int = 2,
    *,
    phanthand_client: Any | None = None,
    sender: str = "claude-code",
    recipient: str = "claude.ai",
    now: datetime | None = NOW,
) -> list[SdkMcpTool[Any]]:
    return build_mindwire_tools(
        layout=_layout(base),
        next_seq=next_seq,
        sender=sender,  # type: ignore[arg-type]
        recipient=recipient,  # type: ignore[arg-type]
        phanthand_client=phanthand_client or AsyncMock(spec=PhanthandClient),
        now=now,
    )


def _tool_by_name(tools: list[SdkMcpTool[Any]], name: str) -> SdkMcpTool[Any]:
    return next(t for t in tools if t.name == name)


# ---------- write_reply -------------------------------------------------


@pytest.mark.anyio
async def test_write_reply_writes_atomic_message_file(tmp_path: Path) -> None:
    tools = _make_tools(tmp_path, next_seq=2)
    write_reply = _tool_by_name(tools, "write_reply")

    result = await write_reply.handler({"content": "hello world"})

    target = _layout(tmp_path).message_path(2, "claude-code")
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "hello world" in text
    # No leftover .tmp
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == []
    assert "Reply written" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_write_reply_emits_yaml_frontmatter_with_z_suffix(tmp_path: Path) -> None:
    tools = _make_tools(tmp_path, next_seq=2)
    write_reply = _tool_by_name(tools, "write_reply")

    await write_reply.handler({"content": "body"})

    target = _layout(tmp_path).message_path(2, "claude-code")
    text = target.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    parts = text.split("---\n", 2)
    assert len(parts) >= 3
    fm = yaml.safe_load(parts[1])
    assert fm["schema_version"] == 1
    assert fm["msg_id"] == f"{ULID_A}/002"
    assert fm["seq"] == 2
    assert fm["from"] == "claude-code"
    assert fm["to"] == "claude.ai"
    assert fm["created_at"] == "2026-05-07T08:43:07Z"


@pytest.mark.anyio
async def test_write_reply_uses_overflow_padding_for_seq_above_999(tmp_path: Path) -> None:
    tools = _make_tools(tmp_path, next_seq=1000)
    write_reply = _tool_by_name(tools, "write_reply")
    await write_reply.handler({"content": "x"})
    target = _layout(tmp_path).message_path(1000, "claude-code")
    assert target.name == "1000-from-cc.md"
    text = target.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["msg_id"] == f"{ULID_A}/1000"


# ---------- read_file ---------------------------------------------------


@pytest.mark.anyio
async def test_read_file_returns_phanthand_content(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.read_file.return_value = FileReadData(
        path="/D/x.py", content="print(1)", size=8, encoding="utf-8"
    )
    tools = _make_tools(tmp_path, phanthand_client=client)
    read_file = _tool_by_name(tools, "read_file")

    result = await read_file.handler({"path": "/D/x.py"})

    client.read_file.assert_awaited_once_with("/D/x.py")
    assert result["content"][0]["text"] == "print(1)"
    assert "isError" not in result


@pytest.mark.anyio
async def test_read_file_error_becomes_iserror_tool_result(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.read_file.side_effect = PhanthandAPIError(
        "Path not allowed: /etc/passwd", endpoint="/files/read"
    )
    tools = _make_tools(tmp_path, phanthand_client=client)
    read_file = _tool_by_name(tools, "read_file")

    result = await read_file.handler({"path": "/etc/passwd"})

    assert result["isError"] is True
    assert "Path not allowed" in result["content"][0]["text"]


# ---------- list_dir ----------------------------------------------------


@pytest.mark.anyio
async def test_list_dir_formats_entries(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.list_directory.return_value = FileListData(
        path="/D",
        entries=[
            FileListEntry(name="a.py", path="/D/a.py", is_dir=False, size=10),
            FileListEntry(name="sub", path="/D/sub", is_dir=True),
        ],
        count=2,
    )
    tools = _make_tools(tmp_path, phanthand_client=client)
    list_dir = _tool_by_name(tools, "list_dir")

    result = await list_dir.handler({"path": "/D"})

    text = result["content"][0]["text"]
    assert "F /D/a.py" in text
    assert "D /D/sub" in text


@pytest.mark.anyio
async def test_list_dir_empty_directory(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.list_directory.return_value = FileListData(path="/D", entries=[], count=0)
    tools = _make_tools(tmp_path, phanthand_client=client)
    list_dir = _tool_by_name(tools, "list_dir")
    result = await list_dir.handler({"path": "/D"})
    assert "(empty)" in result["content"][0]["text"]


# ---------- search ------------------------------------------------------


@pytest.mark.anyio
async def test_search_returns_match_list(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.file_search.return_value = FileSearchData(
        path="/D",
        pattern="*.py",
        matches=["/D/a.py", "/D/b.py"],
        count=2,
        truncated=False,
    )
    tools = _make_tools(tmp_path, phanthand_client=client)
    search = _tool_by_name(tools, "search")

    result = await search.handler({"path": "/D", "pattern": "*.py"})
    text = result["content"][0]["text"]
    assert "/D/a.py" in text
    assert "/D/b.py" in text
    assert "truncated" not in text


@pytest.mark.anyio
async def test_search_marks_truncated_results(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.file_search.return_value = FileSearchData(
        path="/D",
        pattern="*.py",
        matches=["/D/a.py"],
        count=1,
        truncated=True,
    )
    tools = _make_tools(tmp_path, phanthand_client=client)
    search = _tool_by_name(tools, "search")
    result = await search.handler({"path": "/D", "pattern": "*.py"})
    assert "truncated" in result["content"][0]["text"]


# ---------- file_info ---------------------------------------------------


@pytest.mark.anyio
async def test_file_info_renders_metadata(tmp_path: Path) -> None:
    client = AsyncMock(spec=PhanthandClient)
    client.file_info.return_value = FileInfoData(
        path="/D/x.py",
        name="x.py",
        size=1234,
        created=None,
        modified=NOW,
        is_file=True,
        is_dir=False,
        readonly=False,
    )
    tools = _make_tools(tmp_path, phanthand_client=client)
    file_info = _tool_by_name(tools, "file_info")

    result = await file_info.handler({"path": "/D/x.py"})
    text = result["content"][0]["text"]
    assert "kind: file" in text
    assert "size: 1234" in text
    assert "readonly: False" in text
    # FileExistsData unused in this test path; reference to silence linters.
    assert FileExistsData.__name__ == "FileExistsData"


# ---------- factory wiring ----------------------------------------------


@pytest.mark.anyio
async def test_factory_builds_all_five_tools(tmp_path: Path) -> None:
    tools = _make_tools(tmp_path)
    names = sorted(t.name for t in tools)
    assert names == ["file_info", "list_dir", "read_file", "search", "write_reply"]


def test_build_mindwire_mcp_server_returns_sdk_config(tmp_path: Path) -> None:
    server_cfg = build_mindwire_mcp_server(
        layout=_layout(tmp_path),
        next_seq=1,
        sender="claude-code",
        recipient="claude.ai",
        phanthand_client=AsyncMock(spec=PhanthandClient),
    )
    # McpSdkServerConfig is a TypedDict — minimum check is that it has
    # the keys the SDK requires for an in-process server.
    assert server_cfg["type"] == "sdk"
    assert server_cfg["name"] == "mindwire"
