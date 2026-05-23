"""Tests for T20 ``NaysayerPrReviewAdapter`` (WIRING_ALLOWLIST_SPEC §A.3).

Fake Lexora + fake GitHub clients exercise the PR-review flow (diff fetch →
critique → chatroom post + GitHub review submit), verdict parsing, the no-PR-ref
no-op, and the fail-closed paths (Lexora/GitHub unreachable, empty review).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.adapters.naysayer_pr_review import (
    NaysayerPrReviewAdapter,
    NaysayerPrReviewDeliveryError,
)
from spirrow_mindwire.github.client import PrRef, ReviewEvent
from spirrow_mindwire.lexora.client import ChatCompletion, ChatMessage
from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 23, tzinfo=UTC)
_PR_TEXT = "spirrowgames/spirrow-mindwire#42"


class _FakeLexora:
    def __init__(
        self,
        *,
        content: str | None = "looks risky\n\nVERDICT: REQUEST_CHANGES",
        finish_reason: str = "stop",
        raise_exc: Exception | None = None,
    ) -> None:
        self._content = content
        self._fr = finish_reason
        self._raise = raise_exc
        self.calls: list[tuple[str, list[ChatMessage], int]] = []

    async def chat_completion(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion:
        self.calls.append((model, messages, max_tokens))
        if self._raise is not None:
            raise self._raise
        return ChatCompletion(
            content=self._content,
            reasoning_content="...",
            finish_reason=self._fr,
            model="DeepSeek-V4-Flash",
            usage={"total_tokens": 10},
        )

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def aclose(self) -> None:
        return None


class _FakeGitHub:
    def __init__(
        self,
        *,
        diff: str = "diff --git a/x b/x\n+added",
        fetch_exc: Exception | None = None,
        submit_exc: Exception | None = None,
    ) -> None:
        self._diff = diff
        self._fetch_exc = fetch_exc
        self._submit_exc = submit_exc
        self.fetched: list[PrRef] = []
        self.submitted: list[tuple[PrRef, ReviewEvent, str]] = []

    async def fetch_pr_diff(self, pr: PrRef) -> str:
        self.fetched.append(pr)
        if self._fetch_exc is not None:
            raise self._fetch_exc
        return self._diff

    async def submit_review(self, pr: PrRef, *, event: ReviewEvent, body: str) -> dict[str, Any]:
        self.submitted.append((pr, event, body))
        if self._submit_exc is not None:
            raise self._submit_exc
        return {"id": 1, "state": event.value}

    async def aclose(self) -> None:
        return None


def _thread_ref() -> ThreadRef:
    return ThreadRef(
        project_id="spirrow-mindwire", thread_id="T-pr-review-1", chatroom_uri="mc://t/1"
    )


def _ctx(captured: list[ReplyDraft]) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(on_reply=on_reply, on_event_log=on_event_log, own_role=Role.NAYSAYER)


def _event(*, author: str = "orchestrator", body: str = f"review {_PR_TEXT}") -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author=author, body=body, parent_msg_id=None),
    )


async def _spawn(adapter: NaysayerPrReviewAdapter, captured: list[ReplyDraft]):
    return await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))


# ---------- capabilities -------------------------------------------------- #


def test_capabilities_naysayer_qualified_no_execute() -> None:
    caps = NaysayerPrReviewAdapter.capabilities
    assert Capability.NAYSAYER_QUALIFIED in caps
    assert Capability.EXECUTE_CODE not in caps


def test_satisfies_roleadapter_protocol() -> None:
    adapter: RoleAdapter = NaysayerPrReviewAdapter(lexora=_FakeLexora(), github=_FakeGitHub())
    assert adapter.adapter_id == "naysayer-pr-review"


# ---------- happy path ---------------------------------------------------- #


@pytest.mark.anyio
async def test_request_changes_flow() -> None:
    lexora = _FakeLexora(content="line 3 is wrong\n\nVERDICT: REQUEST_CHANGES")
    github = _FakeGitHub()
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=github)
    handle = await _spawn(adapter, captured)
    await adapter.deliver_event(handle, _event())

    assert github.fetched == [PrRef("spirrowgames", "spirrow-mindwire", 42)]
    assert len(captured) == 1
    assert "VERDICT: REQUEST_CHANGES" in captured[0].body
    assert captured[0].adapter_metadata["verdict"] == "REQUEST_CHANGES"
    assert len(github.submitted) == 1
    _pr, event, body = github.submitted[0]
    assert event is ReviewEvent.REQUEST_CHANGES
    assert "VERDICT" in body
    assert (await adapter.health(handle)).state is SessionState.IDLE


@pytest.mark.anyio
async def test_approve_flow() -> None:
    lexora = _FakeLexora(content="all good\n\nVERDICT: APPROVE")
    github = _FakeGitHub()
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=github)
    handle = await _spawn(adapter, captured)
    await adapter.deliver_event(handle, _event())
    assert github.submitted[0][1] is ReviewEvent.APPROVE
    assert captured[0].adapter_metadata["verdict"] == "APPROVE"


@pytest.mark.anyio
async def test_ambiguous_verdict_defaults_to_request_changes() -> None:
    # No VERDICT line → never silently approve (ADR-05 §5).
    lexora = _FakeLexora(content="some comments without a verdict line")
    github = _FakeGitHub()
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=github)
    handle = await _spawn(adapter, captured)
    await adapter.deliver_event(handle, _event())
    assert github.submitted[0][1] is ReviewEvent.REQUEST_CHANGES


@pytest.mark.anyio
async def test_lexora_called_with_naysayer_tier_and_budget() -> None:
    lexora = _FakeLexora()
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=_FakeGitHub())
    handle = await _spawn(adapter, [])
    await adapter.deliver_event(handle, _event())
    model, _messages, max_tokens = lexora.calls[0]
    assert model == "naysayer"
    assert max_tokens >= 1500  # §A.3 reasoning-model floor


# ---------- no-op / filters ---------------------------------------------- #


@pytest.mark.anyio
async def test_no_pr_ref_is_noop() -> None:
    lexora = _FakeLexora()
    github = _FakeGitHub()
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=github)
    handle = await _spawn(adapter, captured)
    await adapter.deliver_event(handle, _event(body="just chatting, no PR here"))
    assert captured == []
    assert github.fetched == []
    assert lexora.calls == []
    assert (await adapter.health(handle)).state is SessionState.IDLE


@pytest.mark.anyio
async def test_own_role_self_filter() -> None:
    github = _FakeGitHub()
    adapter = NaysayerPrReviewAdapter(lexora=_FakeLexora(), github=github)
    handle = await _spawn(adapter, [])
    await adapter.deliver_event(handle, _event(author="naysayer"))
    assert github.fetched == []


@pytest.mark.anyio
async def test_non_new_message_is_noop() -> None:
    github = _FakeGitHub()
    adapter = NaysayerPrReviewAdapter(lexora=_FakeLexora(), github=github)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    event = ChatroomEvent(
        event_id="e",
        event_type=EventType.THREAD_CLOSED,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m", author="x", body=_PR_TEXT, parent_msg_id=None),
    )
    await adapter.deliver_event(handle, event)
    assert github.fetched == []


# ---------- fail-closed --------------------------------------------------- #


@pytest.mark.anyio
async def test_github_fetch_unreachable_fails_closed() -> None:
    github = _FakeGitHub(fetch_exc=RuntimeError("github down"))
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=_FakeLexora(), github=github)
    handle = await _spawn(adapter, captured)
    with pytest.raises(NaysayerPrReviewDeliveryError):
        await adapter.deliver_event(handle, _event())
    assert captured == []  # nothing posted
    assert github.submitted == []
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None


@pytest.mark.anyio
async def test_lexora_empty_review_fails_loud() -> None:
    lexora = _FakeLexora(content="", finish_reason="length")
    github = _FakeGitHub()
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=github)
    handle = await _spawn(adapter, captured)
    with pytest.raises(NaysayerPrReviewDeliveryError):
        await adapter.deliver_event(handle, _event())
    assert captured == []
    assert github.submitted == []  # no review submitted on empty critique
    assert (await adapter.health(handle)).state is SessionState.FAILED


@pytest.mark.anyio
async def test_github_submit_failure_after_post_fails_loud() -> None:
    # The critique is posted to the thread first, then the GH submit fails.
    lexora = _FakeLexora(content="bug here\n\nVERDICT: REQUEST_CHANGES")
    github = _FakeGitHub(submit_exc=RuntimeError("submit 500"))
    captured: list[ReplyDraft] = []
    adapter = NaysayerPrReviewAdapter(lexora=lexora, github=github)
    handle = await _spawn(adapter, captured)
    with pytest.raises(NaysayerPrReviewDeliveryError):
        await adapter.deliver_event(handle, _event())
    assert len(captured) == 1  # critique already posted to the thread
    assert (await adapter.health(handle)).state is SessionState.FAILED


# ---------- halt ---------------------------------------------------------- #


@pytest.mark.anyio
async def test_halt_idempotent() -> None:
    adapter = NaysayerPrReviewAdapter(lexora=_FakeLexora(), github=_FakeGitHub())
    handle = await _spawn(adapter, [])
    await adapter.halt(handle)
    assert (await adapter.health(handle)).state is SessionState.HALTED
    await adapter.halt(handle)  # no raise
    with pytest.raises(NaysayerPrReviewDeliveryError):
        await adapter.deliver_event(handle, _event())
