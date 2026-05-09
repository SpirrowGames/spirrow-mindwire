"""Tests for the static system prompt the watcher injects."""

from __future__ import annotations

from spirrow_mindwire.claude_code import SYSTEM_PROMPT


def test_system_prompt_is_nonempty_string() -> None:
    assert isinstance(SYSTEM_PROMPT, str)
    assert len(SYSTEM_PROMPT.strip()) > 0


def test_system_prompt_names_write_reply_tool() -> None:
    """Critical: the prompt must direct Claude to use write_reply."""
    assert "mcp__mindwire__write_reply" in SYSTEM_PROMPT


def test_system_prompt_names_phanthand_read_tools() -> None:
    """Phanthand-backed read tools must be discoverable from the prompt."""
    assert "mcp__mindwire__read_file" in SYSTEM_PROMPT


def test_system_prompt_names_all_phanthand_read_tools_fully_qualified() -> None:
    """Every Phanthand read tool must appear with the mcp__mindwire__ prefix.

    Without the prefix Claude tries to call e.g. ``list_dir`` directly
    instead of ``mcp__mindwire__list_dir``, which silently fails since
    the SDK only routes calls through fully-qualified MCP tool names.
    """
    for tool in (
        "mcp__mindwire__read_file",
        "mcp__mindwire__list_dir",
        "mcp__mindwire__search",
        "mcp__mindwire__file_info",
    ):
        assert tool in SYSTEM_PROMPT, f"missing fully-qualified tool name: {tool}"


def test_system_prompt_explains_is_latest_marker() -> None:
    """Claude must know which message it's replying to."""
    assert "is_latest" in SYSTEM_PROMPT


def test_system_prompt_disables_builtin_tools_in_one_clause() -> None:
    """Built-in tools must be disabled and the prompt must back it up.

    Asserts the negation appears in the *same* sentence as the built-in
    reference, so a future edit splitting them across paragraphs (where
    the builtin reference loses its negation) trips this test.
    """
    sentences = [s.strip().lower() for s in SYSTEM_PROMPT.replace("\n", " ").split(".")]
    assert any(
        ("built-in" in s or "builtin" in s) and ("disabled" in s or "do not" in s)
        for s in sentences
    ), "no single sentence both names built-in tools AND negates them"


def test_system_prompt_mentions_mw_thread_framing() -> None:
    """The XML framing the watcher injects must be referenced."""
    assert "<mw_thread>" in SYSTEM_PROMPT or "mw_thread" in SYSTEM_PROMPT
