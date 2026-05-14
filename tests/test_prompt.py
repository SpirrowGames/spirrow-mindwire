"""Tests for :func:`spirrow_mindwire.claude_code.build_thread_prompt`.

Covers ``docs/architecture.md`` §6.4 contract: ``<mw_thread>`` framing,
``<mw_message>`` per-turn elements, ``is_latest="true"`` on the highest
seq, XML escaping of user bodies, deterministic attribute order, UTC
ISO 8601 timestamps with the canonical ``Z`` suffix, and ``ValueError``
on the empty-message edge case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.claude_code import build_thread_prompt
from spirrow_mindwire.schema import Message, ThreadMeta

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)
LATER = datetime(2026, 5, 7, 8, 45, 22, tzinfo=UTC)


def _meta(**overrides: Any) -> ThreadMeta:
    base: dict[str, Any] = {
        "schema_version": 2,
        "thread_id": ULID_A,
        "status": "active",
        "awaiting_from": "claude-code",
        "participants": ("claude.ai", "claude-code"),
        "created_at": NOW,
        "updated_at": LATER,
    }
    base.update(overrides)
    return ThreadMeta(**base)


def _msg(**overrides: Any) -> Message:
    base: dict[str, Any] = {
        "schema_version": 1,
        "msg_id": f"{ULID_A}/001",
        "seq": 1,
        "from": "claude.ai",
        "to": "claude-code",
        "created_at": NOW,
        "body": "hello",
    }
    base.update(overrides)
    return Message.model_validate(base)


def test_single_message_marks_is_latest() -> None:
    out = build_thread_prompt(_meta(), [_msg()])
    assert '<mw_thread thread_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"' in out
    assert ' status="active"' in out
    assert ' participants="claude.ai,claude-code">' in out
    assert ' seq="1"' in out
    assert ' from="claude.ai"' in out
    assert ' to="claude-code"' in out
    assert ' created_at="2026-05-07T08:43:07Z"' in out
    assert ' is_latest="true"' in out
    assert "hello" in out
    assert out.rstrip().endswith("</mw_thread>")


def test_only_highest_seq_carries_is_latest() -> None:
    msgs = [
        _msg(),
        _msg(
            seq=2,
            msg_id=f"{ULID_A}/002",
            **{"from": "claude-code"},
            to="claude.ai",
            created_at=LATER,
            body="reply",
        ),
    ]
    out = build_thread_prompt(_meta(), msgs)
    assert out.count(' is_latest="true"') == 1
    # is_latest must be on the seq=2 message, not seq=1
    seq1_block = out.split('seq="1"')[1].split("</mw_message>")[0]
    seq2_block = out.split('seq="2"')[1].split("</mw_message>")[0]
    assert "is_latest" not in seq1_block
    assert 'is_latest="true"' in seq2_block


def test_messages_are_emitted_in_seq_order_regardless_of_input_order() -> None:
    later_msg = _msg(
        seq=2,
        msg_id=f"{ULID_A}/002",
        **{"from": "claude-code"},
        to="claude.ai",
        created_at=LATER,
        body="reply",
    )
    out_normal = build_thread_prompt(_meta(), [_msg(), later_msg])
    out_reversed = build_thread_prompt(_meta(), [later_msg, _msg()])
    assert out_normal == out_reversed
    # Anchor the assertion on the full ``<mw_message seq="N"`` opening so
    # we're checking element position, not bare attribute substrings
    # (which would tie-break by lex order even on broken sorts).
    seq1_pos = out_normal.find('<mw_message seq="1"')
    seq2_pos = out_normal.find('<mw_message seq="2"')
    assert seq1_pos != -1 and seq2_pos != -1
    assert seq1_pos < seq2_pos


def test_xml_special_characters_in_body_are_escaped() -> None:
    body = "use <script>alert('x')</script> & co"
    out = build_thread_prompt(_meta(), [_msg(body=body)])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp; co" in out


def test_created_at_uses_canonical_z_suffix() -> None:
    out = build_thread_prompt(_meta(), [_msg()])
    # Pydantic stores UTC offset as +00:00 internally; the prompt must
    # emit the architecture.md §3 canonical "Z" form instead.
    assert "+00:00" not in out
    assert ' created_at="2026-05-07T08:43:07Z"' in out


def test_empty_messages_raises() -> None:
    with pytest.raises(ValueError, match="messages"):
        build_thread_prompt(_meta(), [])


def test_duplicate_seq_raises() -> None:
    duplicate = _msg()
    other = _msg(
        msg_id=f"{ULID_A}/001",
        seq=1,
        **{"from": "claude-code"},
        to="claude.ai",
        body="dup",
    )
    with pytest.raises(ValueError, match="duplicate seq"):
        build_thread_prompt(_meta(), [duplicate, other])


def test_message_from_different_thread_raises() -> None:
    other_thread = "01ARZ3NDEKTSV4RRFFQ69G5FBW"
    foreign = _msg(msg_id=f"{other_thread}/001")
    with pytest.raises(ValueError, match="does not belong to thread"):
        build_thread_prompt(_meta(), [foreign])


def test_thread_status_attribute_reflects_meta() -> None:
    out = build_thread_prompt(_meta(status="active"), [_msg()])
    assert ' status="active"' in out


def test_attribute_order_is_stable() -> None:
    out = build_thread_prompt(_meta(), [_msg()])
    thread_open = out.split(">", 1)[0]
    # attribute order: thread_id, status, participants
    assert thread_open.index("thread_id") < thread_open.index("status")
    assert thread_open.index("status") < thread_open.index("participants")

    msg_open = out.split("<mw_message", 1)[1].split(">", 1)[0]
    # attribute order: seq, from, to, created_at, (is_latest if present)
    for prev, nxt in [("seq", "from"), ("from=", "to="), ("to=", "created_at")]:
        assert msg_open.index(prev) < msg_open.index(nxt)
