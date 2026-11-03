"""The implementer prompt's TURN START — BASE SYNC section (T-implementer-merges-main-after-gate).

Measured on PR #370 (msg-5854): the implementer ran its gate, pushed, found the PR CONFLICTING,
merged ``origin/main`` and ran the gate a second time (425.8 s). The design (v0.3, msg-6119,
approved msg-7287) moves the base merge to the start of the turn so the first gate verifies the
merged result. These tests pin the three structural guarantees the design names: the section
order, the ALLOWED wording, and that ``rebase`` stays FORBIDDEN.
"""

from __future__ import annotations

import re

from spirrow_mindwire.adapters.implementer import (
    _BASE_IMPLEMENTER_SYSTEM_PROMPT,
    _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT,
)

_PROMPT = _BASE_IMPLEMENTER_SYSTEM_PROMPT


def _header_pos(header: str) -> int:
    """Index where a line starting with ``header`` begins, including position 0.

    Anchoring on ``(?:^|\\n)`` instead of a literal newline prefix keeps the helper correct if a
    future restructuring moves one of these headers to the very start of the prompt.
    """
    match = re.search(rf"(?:^|\n)({re.escape(header)})", _PROMPT)
    assert match is not None, f"header not found in implementer prompt: {header!r}"
    return match.start(1)


def _section(header: str) -> str:
    """Return the paragraph that starts with ``header`` (up to the next blank line)."""
    start = _header_pos(header)
    end = _PROMPT.find("\n\n", start + 1)
    return _PROMPT[start : end if end != -1 else len(_PROMPT)]


def test_header_pos_finds_a_header_at_index_zero() -> None:
    assert _header_pos(_PROMPT.split("\n", 1)[0]) == 0


def test_turn_start_section_sits_between_forbidden_and_gate() -> None:
    forbidden = _header_pos("FORBIDDEN (Tier C")
    turn_start = _header_pos("TURN START — BASE SYNC (do this FIRST")
    gate = _header_pos("GATE (run before every commit")
    assert forbidden < turn_start < gate


def test_turn_start_section_merges_never_rebases_and_has_no_prepush_check() -> None:
    section = _section("TURN START — BASE SYNC")
    assert "git fetch origin" in section
    assert "git merge origin/<base>" in section
    assert "never rebase" in section
    assert "REQUEST_CHANGES" in section
    assert "Do NOT add a conflict check right before push" in section


def test_allowed_includes_fetch_and_base_merge() -> None:
    allowed = _section("ALLOWED (Tier A)")
    assert "`git fetch`" in allowed
    assert "merging the PR's base branch" in allowed
    assert "into your own feature branch" in allowed


def test_rebase_stays_forbidden() -> None:
    forbidden = _section("FORBIDDEN (Tier C")
    assert "rebase" in forbidden
    assert "rebase" not in _section("ALLOWED (Tier A)")


def test_workflow_paragraph_refers_back_to_base_sync() -> None:
    assert "After the TURN START base sync, work on a feature/* branch" in _PROMPT


def test_rendered_prompt_carries_the_section() -> None:
    assert "TURN START — BASE SYNC" in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
