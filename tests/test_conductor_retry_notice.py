"""T-retry-once-before-quarantine D-4: the retry notice reaches only the roles it is meant for.

The sweep re-launches a thread once after its first failure and passes ``--retry-of``. The
conductor then attaches a fixed "check before you act" paragraph to round 0's prompt, but only
when the dispatched role is in ``RETRY_NOTICE_ROLES`` (msg-5441 / msg-5449). Every other role's
prompt must be byte-for-byte unchanged, because telling a role with no side effects to inspect a
working tree it never touched invites hallucinated checks (Einstein msg-5439, blocking).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.conductor import retry_notice as retry_notice_mod
from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.conductor.retry_notice import (
    RETRY_NOTICE_ROLES,
    UNKNOWN_FIRST_FAILURE_AT,
    RetryOf,
    parse_retry_of,
    render_retry_notice,
    retry_notice_for,
)
from spirrow_mindwire.thread_context import build_turn_prompt
from spirrow_mindwire.value_objects import ChatroomEvent, Role, SessionHandle, ThreadRef

_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")
_ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}
_RETRY = RetryOf(error_code="adapter.turn_timeout", first_failure_at="2026-10-01T10:00:00Z")


class _Mcp:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_get_thread":
            return {"messages": self.messages}
        return {"msg": {"msg_id": "msg-relay"}}


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[Role, ChatroomEvent]] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        return SessionHandle(
            session_id="s",
            instance_id=instance_id,
            adapter_id="a",
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.dispatched.append((handle.role, event))


def _thread(head_author: str, head_next: str) -> list[dict[str, Any]]:
    return [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {
            "msg_id": "msg-2",
            "author": head_author,
            "content": f"go\n\nNEXT: {head_next}",
            "timestamp": "",
        },
    ]


async def _first_dispatch(
    head_author: str, head_next: str, retry_of: RetryOf | None
) -> tuple[Role, ChatroomEvent]:
    dispatcher = _Dispatcher()
    c = Conductor(
        mcp=_Mcp(_thread(head_author, head_next)),
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
        retry_of=retry_of,
    )
    await c.run()
    assert dispatcher.dispatched, "the conductor did not dispatch"
    return dispatcher.dispatched[0]


# --- parsing -----------------------------------------------------------------------------------


def test_parse_retry_of_absent_or_blank_is_not_a_retry() -> None:
    assert parse_retry_of(None) is None
    assert parse_retry_of("") is None
    assert parse_retry_of("   ") is None


def test_parse_retry_of_splits_at_the_last_at_sign() -> None:
    parsed = parse_retry_of("adapter.shutdown_failed@2026-10-01T10:00:00.0000000Z")
    assert parsed == RetryOf("adapter.shutdown_failed", "2026-10-01T10:00:00.0000000Z")


def test_parse_retry_of_is_lenient_because_the_flag_itself_means_retry() -> None:
    # No '@': the whole text is the code; a retry is still a retry.
    assert parse_retry_of("exit-1") == RetryOf("exit-1", UNKNOWN_FIRST_FAILURE_AT)
    # Empty halves get placeholders rather than vanishing.
    assert parse_retry_of("@2026-10-01T10:00:00Z") == RetryOf("unknown", "2026-10-01T10:00:00Z")
    assert parse_retry_of("x@") == RetryOf("x", UNKNOWN_FIRST_FAILURE_AT)


def test_notice_text_names_the_failure_and_the_operations_not_to_repeat() -> None:
    text = render_retry_notice(_RETRY)
    assert "adapter.turn_timeout" in text
    assert "2026-10-01T10:00:00Z" in text
    words = ("作業ツリー", "ブランチ", "既存の PR", "push", "PR 作成", "コメント", "繰り返すな")
    for word in words:
        assert word in text


# --- the gate ----------------------------------------------------------------------------------


def test_registry_holds_exactly_the_implementer_today() -> None:
    assert frozenset({"implementer"}) == RETRY_NOTICE_ROLES


def test_notice_only_on_round_zero() -> None:
    assert retry_notice_for(_RETRY, "implementer", rounds=0) is not None
    assert retry_notice_for(_RETRY, "implementer", rounds=1) is None


def test_no_notice_without_retry_of_for_any_role() -> None:
    for role in Role:
        assert retry_notice_for(None, role.value, rounds=0) is None


@pytest.mark.anyio
async def test_implementer_dispatch_carries_the_notice_on_a_retry() -> None:
    role, event = await _first_dispatch("human", "Heisenberg", _RETRY)
    assert role is Role.IMPLEMENTER
    assert event.retry_notice == render_retry_notice(_RETRY)
    prompt = build_turn_prompt(event, role, "closing")
    assert render_retry_notice(_RETRY) in prompt
    # Its own block, after the trigger and before the closing — never inside the author's body.
    notice_at = prompt.index("[再試行の注記]")
    assert prompt.index("New message from") < notice_at < prompt.index("closing")
    assert "[再試行の注記]" not in event.payload.body


@pytest.mark.anyio
async def test_implementer_dispatch_without_retry_of_is_unchanged() -> None:
    _, event = await _first_dispatch("human", "Heisenberg", None)
    assert event.retry_notice is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("head_author", "head_next", "expected_role"),
    [
        ("Bohr", "Einstein", Role.NAYSAYER),
        ("Einstein", "Bohr", Role.PROPOSER),
    ],
)
async def test_other_roles_prompt_is_byte_identical_on_a_retry(
    head_author: str, head_next: str, expected_role: Role
) -> None:
    role_r, retried = await _first_dispatch(head_author, head_next, _RETRY)
    role_p, plain = await _first_dispatch(head_author, head_next, None)
    assert role_r is role_p is expected_role
    assert retried.retry_notice is None
    assert build_turn_prompt(retried, role_r, "c") == build_turn_prompt(plain, role_p, "c")


@pytest.mark.anyio
async def test_gate_reads_the_registry_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binds the use site to RETRY_NOTICE_ROLES: changing the constant changes who gets it."""
    monkeypatch.setattr(retry_notice_mod, "RETRY_NOTICE_ROLES", frozenset({"naysayer"}))
    _, naysayer_event = await _first_dispatch("Bohr", "Einstein", _RETRY)
    assert naysayer_event.retry_notice == render_retry_notice(_RETRY)
    _, implementer_event = await _first_dispatch("human", "Heisenberg", _RETRY)
    assert implementer_event.retry_notice is None


# --- CLI plumbing ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv_tail", "expected"),
    [
        ([], None),
        (["--retry-of", "adapter.turn_timeout@2026-10-01T10:00:00Z"], _RETRY),
    ],
)
def test_main_forwards_the_parsed_retry_of(
    monkeypatch: pytest.MonkeyPatch, argv_tail: list[str], expected: RetryOf | None
) -> None:
    """The sweep's ``--retry-of`` reaches ``run_conductor`` parsed; absent means not a retry."""
    from spirrow_mindwire import loop_runner
    from spirrow_mindwire.config import MindwireSettings

    seen: dict[str, Any] = {}

    async def _fake_run_conductor(_settings: MindwireSettings, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor", *argv_tail])
    try:
        loop_runner.main()
    except SystemExit as exc:
        assert not exc.code, f"main exited non-zero: {exc.code!r}"
    assert seen.get("retry_of") == expected
