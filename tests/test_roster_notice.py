"""The roster notice: every turn is told which names a ``NEXT:`` line may address.

T-naysayer-invents-next-participant D-1 (Bohr msg-8588 → msg-8591 → msg-8594, then Einstein's
go with no objections). No prompt carried the roster's names, so the naysayer guessed the
implementer's persona and invented one (``Curie``, ``Gauss``, ``Feynman``, ``Lexora``) the
conductor could not resolve.
The tests carry the letters of the design:

* (a) both prompt builders (``build_turn_prompt`` and the Lexora ``_build_messages``) carry every
  roster name, right after the role line;
* (b) ``roster_notice=None`` leaves both prompts as they were;
* (c) the notice is built from the roster the conductor routes with, and names the reserved
  words ``HUMAN_TOKEN`` / ``OPERATOR_TOKEN`` but never ``NONE_TOKEN``;
* (d) a roster without an implementer lists what it holds and does not raise;
* (e) an empty roster gives ``None``: no block.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from test_conductor_retry_notice import _Dispatcher, _Mcp

from spirrow_mindwire.adapters.naysayer_lexora import _build_messages as lexora_messages
from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    OPERATOR_TOKEN,
    HandoffKind,
    build_roster_notice,
    resolve_handoff,
)
from spirrow_mindwire.thread_context import build_turn_prompt
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    ThreadRef,
)

_ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}
_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")
_HEAD = "You are acting as the naysayer role in thread T-x."


def _event(notice: str | None) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="T-x:msg-2",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_TR,
        occurred_at=datetime(2026, 11, 9, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="msg-2", author="Bohr", body="b", parent_msg_id=None),
        roster_notice=notice,
    )


def _lexora_user(event: ChatroomEvent) -> str:
    return lexora_messages(event, Role.NAYSAYER, "sys")[1].content


# ─── (c) / (d) / (e): the notice itself ───────────────────────────────────────────────────────


def test_the_notice_names_every_participant_with_its_role() -> None:
    notice = build_roster_notice(_ROSTER)
    assert notice is not None
    assert notice.splitlines()[0] == (
        "Participants of this thread: "
        "Bohr = proposer, Einstein = naysayer, Heisenberg = implementer."
    )


def test_every_name_the_notice_offers_resolves() -> None:
    """(c) The names the prompt teaches are names the router accepts, from the same roster."""
    notice = build_roster_notice(_ROSTER)
    assert notice is not None
    for identity, role in _ROSTER.items():
        assert identity in notice
        handoff = resolve_handoff(f"go\n\nNEXT: {identity}", _ROSTER)
        assert handoff.kind is HandoffKind.ROLE and handoff.role is role


def test_the_notice_offers_human_and_operator_but_never_none() -> None:
    """(c, msg-8594 Obj-2-1) Reserved words are named one by one; the retired ``none`` is not."""
    notice = build_roster_notice(_ROSTER)
    assert notice is not None
    words = set(re.findall(r"[a-z]+", notice))
    assert HUMAN_TOKEN in words
    assert OPERATOR_TOKEN in words
    assert NONE_TOKEN not in words
    assert f"({HUMAN_TOKEN} / {OPERATOR_TOKEN})" in notice


def test_a_roster_without_an_implementer_lists_what_it_holds() -> None:
    """(d) A role the roster does not hold simply does not appear; nothing raises."""
    notice = build_roster_notice({"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER})
    assert notice is not None
    assert "Bohr = proposer, Einstein = naysayer." in notice
    assert Role.IMPLEMENTER.value not in notice
    assert "None" not in notice


def test_an_empty_roster_gives_no_notice() -> None:
    """(e) No roster, no block."""
    assert build_roster_notice({}) is None


# ─── (a) / (b): both prompt builders ──────────────────────────────────────────────────────────


def test_the_sdk_prompt_carries_the_roster_right_after_the_role_line() -> None:
    notice = build_roster_notice(_ROSTER)
    assert notice is not None
    prompt = build_turn_prompt(_event(notice), Role.NAYSAYER, "closing")
    assert prompt.startswith(f"{_HEAD}\n\n{notice}\n\n")
    for identity in _ROSTER:
        assert identity in prompt


def test_the_lexora_prompt_carries_the_roster_right_after_the_role_line() -> None:
    """Two of the four incidents ran on gemini-fallback: the Lexora path must carry it too."""
    notice = build_roster_notice(_ROSTER)
    assert notice is not None
    user = _lexora_user(_event(notice))
    assert user.startswith(f"{_HEAD}\n\n{notice}\n\n")
    for identity in _ROSTER:
        assert identity in user


def test_without_the_notice_both_prompts_are_unchanged() -> None:
    """(b) ``None`` renders no block: the prompt is the one before the field existed."""
    notice = build_roster_notice(_ROSTER)
    assert notice is not None
    with_sdk = build_turn_prompt(_event(notice), Role.NAYSAYER, "closing")
    without_sdk = build_turn_prompt(_event(None), Role.NAYSAYER, "closing")
    assert with_sdk.replace(f"{notice}\n\n", "", 1) == without_sdk
    assert "Participants of this thread" not in without_sdk
    with_lex = _lexora_user(_event(notice))
    without_lex = _lexora_user(_event(None))
    assert with_lex.replace(f"{notice}\n\n", "", 1) == without_lex
    assert "Participants of this thread" not in without_lex


# ─── the conductor attaches it, from its own roster, to every role ────────────────────────────


def _thread(head_author: str, head_body: str) -> list[dict[str, Any]]:
    return [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {"msg_id": "msg-2", "author": head_author, "content": head_body, "timestamp": ""},
    ]


async def _first_dispatch(
    messages: list[dict[str, Any]], roster: dict[str, Role]
) -> tuple[Role, ChatroomEvent]:
    dispatcher = _Dispatcher()
    conductor = Conductor(
        mcp=_Mcp(messages),
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=roster,
        naysayer_identity="Einstein",
        max_rounds=1,
    )
    await conductor.run()
    assert dispatcher.dispatched, "the conductor did not dispatch"
    return dispatcher.dispatched[0]


def _dispatch(messages: list[dict[str, Any]]) -> tuple[Role, ChatroomEvent]:
    return asyncio.run(_first_dispatch(messages, _ROSTER))


def test_the_conductor_attaches_the_notice_to_every_role() -> None:
    expected = build_roster_notice(_ROSTER)
    for head_author, head_body, role in (
        ("Bohr", "design\n\nNEXT: Einstein", Role.NAYSAYER),
        ("Einstein", "critique\n\nNEXT: Bohr", Role.PROPOSER),
    ):
        got_role, event = _dispatch(_thread(head_author, head_body))
        assert got_role is role
        assert event.roster_notice == expected
        assert expected is not None and expected in build_turn_prompt(event, role, "closing")
