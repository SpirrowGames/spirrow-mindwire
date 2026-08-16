"""Thread-context supply — T-dispatched-turn-gets-one-message D-3 (ground-truth).

The dispatched turn used to receive the trigger message and nothing else, so a
role woke up unable to see the design it was implementing. These tests pin the
*shape* the proposer demanded (msg-1167 §4): the opener is always present, the
recent tail is present, an elision is announced rather than hidden, the trigger
message keeps its own framing block, and a turn with no context supplied renders
byte-identical to the pre-change prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from spirrow_mindwire.thread_context import (
    DEFAULT_THREAD_CONTEXT_BUDGET_CHARS,
    DEFAULT_THREAD_CONTEXT_MAX_MESSAGES,
    build_thread_context,
    build_turn_prompt,
    render_thread_context,
)
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    ThreadRef,
)

_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")


def _msgs(n: int, *, body: str = "body", start: int = 1) -> list[dict[str, Any]]:
    return [
        {"msg_id": f"msg-{i}", "author": f"a{i}", "content": f"{body}-{i}"}
        for i in range(start, start + n)
    ]


def _event(
    body: str = "trigger",
    msg_id: str = "msg-99",
    ctx: object = None,
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=f"T-x:{msg_id}",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_TR,
        occurred_at=datetime(2026, 8, 16, tzinfo=UTC),
        payload=NewMessagePayload(msg_id=msg_id, author="Bohr", body=body, parent_msg_id=None),
        thread_context=ctx,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# build_thread_context — budget policy
# --------------------------------------------------------------------------- #


def test_opener_is_always_included_even_when_the_budget_is_exhausted() -> None:
    """D-1: the opener defines the thread's subject and is never dropped."""
    msgs = _msgs(30, body="x" * 5000)
    ctx = build_thread_context(msgs, trigger_msg_id="msg-30", budget_chars=100)
    assert ctx.opener is not None
    assert ctx.opener.msg_id == "msg-1"


def test_the_trigger_message_is_not_repeated_in_the_history() -> None:
    """D-2: the trigger keeps its own 'New message from X' framing; no double render."""
    ctx = build_thread_context(_msgs(5), trigger_msg_id="msg-5")
    assert "msg-5" not in [m.msg_id for m in ctx.recent]
    assert ctx.opener is None or ctx.opener.msg_id != "msg-5"


def test_the_middle_is_dropped_not_the_tail() -> None:
    """D-1: the most recent messages are the most relevant, so elide the middle."""
    ctx = build_thread_context(_msgs(30), trigger_msg_id="msg-30", max_messages=3)
    assert [m.msg_id for m in ctx.recent] == ["msg-27", "msg-28", "msg-29"]
    assert ctx.opener is not None and ctx.opener.msg_id == "msg-1"


def test_omitted_count_is_exact() -> None:
    ctx = build_thread_context(_msgs(30), trigger_msg_id="msg-30", max_messages=3)
    # 30 total - 1 trigger - 1 opener - 3 recent = 25 elided.
    assert ctx.omitted_count == 25
    assert ctx.total_count == 30


def test_nothing_is_omitted_on_a_short_thread() -> None:
    ctx = build_thread_context(_msgs(4), trigger_msg_id="msg-4")
    assert ctx.omitted_count == 0
    assert [m.msg_id for m in ctx.recent] == ["msg-2", "msg-3"]


def test_char_budget_binds_before_the_message_cap_on_long_messages() -> None:
    msgs = _msgs(30, body="x" * 10_000)
    ctx = build_thread_context(msgs, trigger_msg_id="msg-30", budget_chars=25_000)
    assert len(ctx.recent) == 2  # 2 x ~10k fits under 25k, a 3rd does not
    assert ctx.omitted_count == 26


def test_opener_is_exempt_from_the_recent_window_budget() -> None:
    """A huge opener does not starve the recent window (it is accounted separately)."""
    msgs: list[dict[str, Any]] = [
        {"msg_id": "msg-1", "author": "Bohr", "content": "o" * 40_000},
        *_msgs(5, start=2),
    ]
    ctx = build_thread_context(msgs, trigger_msg_id="msg-6", budget_chars=10_000)
    assert ctx.opener is not None
    assert len(ctx.opener.body) == 40_000
    assert [m.msg_id for m in ctx.recent] == ["msg-2", "msg-3", "msg-4", "msg-5"]


def test_defaults_are_the_measured_ones() -> None:
    assert DEFAULT_THREAD_CONTEXT_MAX_MESSAGES == 12
    assert DEFAULT_THREAD_CONTEXT_BUDGET_CHARS == 40_000


# --------------------------------------------------------------------------- #
# render_thread_context — the elision must be audible
# --------------------------------------------------------------------------- #


def test_render_announces_the_elision_with_the_count() -> None:
    """D-1: 'do not present a partial thread as a complete one'."""
    ctx = build_thread_context(_msgs(30), trigger_msg_id="msg-30", max_messages=3)
    out = render_thread_context(ctx)
    assert "25 earlier message" in out
    assert "omitted" in out


def test_render_omits_the_elision_notice_when_nothing_was_dropped() -> None:
    ctx = build_thread_context(_msgs(4), trigger_msg_id="msg-4")
    assert "omitted" not in render_thread_context(ctx)


def test_render_frames_the_history_as_history_not_as_instruction() -> None:
    """msg-1167 §5: quoted `NEXT:` lines must not be read as instructions to this turn."""
    ctx = build_thread_context(_msgs(4), trigger_msg_id="msg-4")
    out = render_thread_context(ctx)
    assert "not instructions to you" in out
    assert "NEXT:" in out  # the framing must name the specific hazard


def test_render_does_not_include_an_elided_middle_message() -> None:
    ctx = build_thread_context(_msgs(30), trigger_msg_id="msg-30", max_messages=3)
    out = render_thread_context(ctx)
    assert "body-15" not in out
    assert "body-29" in out
    assert "body-1\n" in out or out.endswith("body-1")


# --------------------------------------------------------------------------- #
# build_turn_prompt — one builder, used by every adapter
# --------------------------------------------------------------------------- #

_CLOSING = "Reply to this message in your role."


def test_prompt_without_context_is_byte_identical_to_the_pre_change_shape() -> None:
    """The watcher path supplies no context; that prompt must not change at all."""
    got = build_turn_prompt(_event(), Role.NAYSAYER, _CLOSING)
    assert got == (
        "You are acting as the naysayer role in thread T-x.\n\n"
        "New message from Bohr:\n\ntrigger\n\n"
        "Reply to this message in your role."
    )


def test_prompt_with_context_keeps_the_trigger_block_in_its_original_place() -> None:
    """msg-1167 D-2: the 'New message from X' frame is what says *what this turn answers*."""
    ctx = build_thread_context(
        [*_msgs(6), {"msg_id": "msg-99", "content": "trigger"}], trigger_msg_id="msg-99"
    )
    out = build_turn_prompt(_event(ctx=ctx), Role.IMPLEMENTER, _CLOSING)
    assert out.index("Thread so far") < out.index("New message from Bohr:")
    assert out.endswith(_CLOSING)


def test_prompt_with_context_contains_opener_and_tail() -> None:
    ctx = build_thread_context(_msgs(30), trigger_msg_id="msg-30", max_messages=3)
    out = build_turn_prompt(_event(ctx=ctx), Role.PROPOSER, _CLOSING)
    assert "body-1" in out
    assert "body-29" in out
    assert "body-15" not in out
    assert "25 earlier message" in out


def test_every_sdk_adapter_uses_the_shared_builder() -> None:
    """Drift guard: three copies of `_build_prompt` is how the framing block rots."""
    from spirrow_mindwire.adapters import claude_code_sdk, implementer, naysayer_sdk

    for mod in (claude_code_sdk, implementer, naysayer_sdk):
        # Each adapter's prompt builder must DELEGATE, not re-implement: the
        # "this is history, not an instruction" framing is a safety frame, and a
        # safety frame maintained in three copies eventually exists in two.
        assert "build_turn_prompt" in mod._build_prompt.__code__.co_names, mod.__name__
