"""Stage 2 — two-role proposer↔naysayer dispatch (ADR-06 §3 / ADR-05 §5).

Proves the *core Stage 2 milestone* end-to-end and in-process: with both
real adapters registered — :class:`ClaudeCodeSdkAdapter` (proposer, fake
SDK client) and :class:`NaysayerLexoraAdapter` (naysayer, fake Lexora
client) — the real :class:`Dispatcher` routes a human message to the
proposer, then routes the proposer's proposal to the **independent**
naysayer, and both replies land in the ChatRoom with the correct authors.

This is the automated proof of the two-role loop without real models /
network (those incur Claude API + Lexora cost + post to a live chatroom —
that is the ``-m manual`` smoke + the documented dogfood procedure in
``test_naysayer_lexora_smoke.py``).

Independence is *architecture-enforced* (ADR-05 §5): the naysayer slot
requires ``NAYSAYER_QUALIFIED``, which only the Lexora adapter carries, so
the same-model claude-code adapter can never be assigned the naysayer role
— asserted directly here via ``qualified_for``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.adapters.naysayer_lexora import NaysayerLexoraAdapter
from spirrow_mindwire.dispatcher.core import Dispatcher, NoQualifiedAdapterError
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.lexora.client import ChatCompletion, ChatMessage
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    Role,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeSdkClient:
    """Fake claude_agent_sdk.ClaudeSDKClient yielding one proposal turn."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    async def receive_response(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=self._text)], model="m")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="end_turn",
            result="ok",
        )

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


class _FakeLexora:
    """Fake LexoraClient yielding one naysayer critique."""

    def __init__(self, critique: str) -> None:
        self._critique = critique

    async def chat_completion(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion:
        return ChatCompletion(
            content=self._critique,
            reasoning_content="(deliberation — never posted)",
            finish_reason="stop",
            model="DeepSeek-V4-Flash.gguf",
            usage={"completion_tokens": 30},
        )

    async def health(self) -> dict[str, Any]:
        return {"status": "ok", "backends": {"naysayer": "healthy"}}

    async def aclose(self) -> None: ...


class _FakeGateway:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post_reply(
        self,
        thread_ref: ThreadRef,
        *,
        author: str,
        body: str,
        reply_to_msg_id: str | None,
        idempotency_key: str,
        role: Role | None = None,
    ) -> str:
        self.posts.append({"author": author, "body": body, "reply_to_msg_id": reply_to_msg_id})
        return f"posted-{len(self.posts)}"


def _sdk_factory(client: _FakeSdkClient) -> Callable[[Any], Any]:
    def make(_options: Any) -> _FakeSdkClient:
        return client

    return make


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _new_message(*, event_id: str, author: str, body: str, msg_id: str) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=event_id,
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author=author, body=body, parent_msg_id=None),
    )


def _build(
    tmp_path: Path, *, proposal: str, critique: str
) -> tuple[Dispatcher, InMemoryAdapterRegistry, _FakeGateway]:
    proposer = ClaudeCodeSdkAdapter(
        cwd=tmp_path, client_factory=_sdk_factory(_FakeSdkClient(proposal))
    )
    naysayer = NaysayerLexoraAdapter(client=_FakeLexora(critique))
    registry = InMemoryAdapterRegistry()
    registry.register(proposer)  # registered first → first qualified for PROPOSER
    registry.register(naysayer)
    gateway = _FakeGateway()

    async def sink(_event: Event) -> None:
        return None

    return Dispatcher(registry=registry, gateway=gateway, event_sink=sink), registry, gateway


# --------------------------------------------------------------------------- #
# architecture-enforced independence
# --------------------------------------------------------------------------- #


def test_qualified_for_routes_roles_to_distinct_adapters(tmp_path: Path) -> None:
    _disp, registry, _gw = _build(tmp_path, proposal="p", critique="c")
    naysayers = registry.qualified_for(Role.NAYSAYER)
    proposers = registry.qualified_for(Role.PROPOSER)
    # Only the Lexora adapter qualifies as naysayer (ADR-05 §5 independence).
    assert [a.adapter_id for a in naysayers] == ["naysayer-lexora"]
    # claude-code is registered first → the proposer slot resolves to it.
    assert proposers[0].adapter_id == "claude-code-sdk"


def test_naysayer_slot_unfilled_without_independent_adapter(tmp_path: Path) -> None:
    # Sanity: without the Lexora adapter, the naysayer slot has no candidate
    # (a same-model proposer adapter can never silently fill it).
    proposer = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_sdk_factory(_FakeSdkClient("p")))
    registry = InMemoryAdapterRegistry()
    registry.register(proposer)
    assert registry.qualified_for(Role.NAYSAYER) == []


@pytest.mark.anyio
async def test_naysayer_slot_unfilled_raises_on_spawn(tmp_path: Path) -> None:
    proposer = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_sdk_factory(_FakeSdkClient("p")))
    registry = InMemoryAdapterRegistry()
    registry.register(proposer)
    disp = Dispatcher(registry=registry, gateway=_FakeGateway())
    with pytest.raises(NoQualifiedAdapterError):
        await disp.spawn_instance(_thread_ref(), Role.NAYSAYER, "naysayer-1")


# --------------------------------------------------------------------------- #
# the round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_proposer_then_naysayer_round_trip(tmp_path: Path) -> None:
    disp, _registry, gateway = _build(
        tmp_path,
        proposal="We should cache everything forever.",
        critique="You wrote 'cache everything forever' — that ignores invalidation.",
    )
    proposer = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    naysayer = await disp.spawn_instance(_thread_ref(), Role.NAYSAYER, "naysayer-1")
    assert proposer.adapter_id == "claude-code-sdk"
    assert naysayer.adapter_id == "naysayer-lexora"

    # 1) human → proposer: proposer posts a proposal.
    await disp.dispatch(
        proposer,
        _new_message(event_id="e1", author="human", body="How should we speed it up?", msg_id="h1"),
    )
    assert len(gateway.posts) == 1
    proposal_post = gateway.posts[0]
    assert proposal_post["author"] == "proposer-1"  # I3 v2.2: author = instance_id
    # The dispatcher stamps the harness-derived source marker on the body
    # (msg-805 D3 / msg-834 §2). The agent's proposal is preserved verbatim;
    # the marker is the trailing HTML-comment line.
    assert proposal_post["body"].startswith("We should cache everything forever.")
    assert proposal_post["body"].rstrip().splitlines()[-1].startswith("<!-- source:")

    # 2) proposer's proposal → naysayer: the independent critic responds. The
    # relayed body carries the proposal + its harness-appended source marker;
    # the Lexora-backed naysayer here is a pre-SDK stateless HTTP adapter
    # (no ``ClaudeAgentOptions`` object), so its post carries no marker —
    # the dispatcher applies the marker only to adapters that expose
    # ``source_marker_options`` (see the source-marker wiring tests in
    # ``tests/test_dispatcher_core.py``).
    await disp.dispatch(
        naysayer,
        _new_message(
            event_id="e2",
            author="proposer",
            body=proposal_post["body"],
            msg_id="p1",
        ),
    )
    assert len(gateway.posts) == 2
    critique_post = gateway.posts[1]
    assert critique_post["author"] == "naysayer-1"  # I3 v2.2: author = instance_id
    assert "invalidation" in critique_post["body"]
    assert critique_post["reply_to_msg_id"] == "p1"


@pytest.mark.anyio
async def test_naysayer_does_not_critique_its_own_post(tmp_path: Path) -> None:
    # The naysayer self-filters its own post echoed back (no self-reply loop).
    # I3 v2.2: the echoed author is the instance_id ("naysayer-1"), not the role.
    disp, _registry, gateway = _build(tmp_path, proposal="p", critique="c")
    naysayer = await disp.spawn_instance(_thread_ref(), Role.NAYSAYER, "naysayer-1")
    await disp.dispatch(
        naysayer,
        _new_message(event_id="e1", author="naysayer-1", body="my own critique", msg_id="n1"),
    )
    assert gateway.posts == []
