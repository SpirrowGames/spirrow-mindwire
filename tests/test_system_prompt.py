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


def test_system_prompt_explains_is_latest_marker() -> None:
    """Claude must know which message it's replying to."""
    assert "is_latest" in SYSTEM_PROMPT


def test_system_prompt_disables_builtin_tools_explicitly() -> None:
    """Built-in tools are turned off at the SDK layer; the prompt must back this up."""
    lower = SYSTEM_PROMPT.lower()
    assert "built-in" in lower or "builtin" in lower
    assert "disabled" in lower or "do not" in lower


def test_system_prompt_mentions_mw_thread_framing() -> None:
    """The XML framing the watcher injects must be referenced."""
    assert "<mw_thread>" in SYSTEM_PROMPT or "mw_thread" in SYSTEM_PROMPT
