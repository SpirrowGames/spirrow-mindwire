"""Manual smoke for the live Lexora naysayer path (ADR-06 Stage 2).

Marked ``manual`` → excluded from CI (``addopts -m "not manual"``); run with
``uv run pytest -m manual``. Requires a reachable Lexora gateway:

- **Production** (mindwire co-resident with Lexora on sg-ai-server-01): the
  default ``http://localhost:8110`` is correct — leave ``MINDWIRE_LEXORA_URL``
  unset. Loopback is mandatory there: Lexora binds ``0.0.0.0`` with no caller
  auth, so a LAN/Tailscale default would widen the unauthenticated surface.
- **Dev box** (this machine reaches the server over Tailscale): export
  ``MINDWIRE_LEXORA_URL=http://100.79.84.62:8110`` before running.

What this asserts against the **real** gateway (msg-215 verification items):

1. Response shape — the reply is ``choices[0].message.content`` and the
   deliberation is the *sibling* ``choices[0].message.reasoning_content``
   (a separate field, not a ``<think>`` tag inside content). This is the
   open point main asked Claude Code to confirm on real hardware.
2. Fail-loud (main order #1) — an unknown tier raises
   :class:`LexoraHTTPError` (HTTP 502 from the gateway: the naysayer tier
   has no silent fallback to another model).
3. A real ``model="naysayer"`` critique flows end-to-end through
   :class:`NaysayerLexoraAdapter` → ``on_reply`` → a non-empty reply that
   is the content, never the reasoning_content.

Out of scope here (documented dogfood procedure, not automated — incurs
**Claude API + Lexora cost** and **posts to a live chatroom**): the full
two-role round-trip on a real thread. Procedure:

    from pathlib import Path
    from spirrow_mindwire.adapters import ClaudeCodeSdkAdapter, NaysayerLexoraAdapter
    from spirrow_mindwire.dispatcher.core import Dispatcher
    from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
    from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
    from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
    from spirrow_mindwire.magickit.watcher import ChatroomWatcher  # WatchSpec wiring
    from spirrow_mindwire.value_objects import Role

    registry = InMemoryAdapterRegistry()
    registry.register(ClaudeCodeSdkAdapter(cwd=Path.cwd()))   # proposer
    registry.register(NaysayerLexoraAdapter())                # naysayer (Lexora)
    gateway = MagickitChatroomGateway(StreamableHttpChatroomMcp())
    dispatcher = Dispatcher(registry=registry, gateway=gateway)
    # spawn_role(PROPOSER) → claude-code, spawn_role(NAYSAYER) → naysayer-lexora;
    # post a human message, watch proposer propose, feed that to the naysayer,
    # and verify an author=naysayer critique appears that quotes the proposal.

The automated in-process equivalent (fake models) is
``tests/test_two_role_dispatch.py``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

from spirrow_mindwire.adapters.naysayer_lexora import NaysayerLexoraAdapter
from spirrow_mindwire.lexora.client import ChatMessage, LexoraClient, LexoraHTTPError
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    ThreadRef,
)

_PROPOSAL = "We should store user passwords in plaintext so login is faster."
_TERSE_CRITIC = "You are a terse critic. Reply in one sentence."

# Windows consoles default to legacy codepages (e.g. cp932) that cannot encode
# the model's reply (em-dashes / non-breaking hyphens / Japanese); emit UTF-8 so
# the critique print() below doesn't raise UnicodeEncodeError. getattr keeps mypy
# happy (reconfigure is TextIOWrapper-only). Mirrors scripts/dogfood_smoke.py.
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")


@pytest.mark.manual
@pytest.mark.anyio
async def test_live_response_shape_content_and_reasoning_are_siblings() -> None:
    async with LexoraClient() as client:  # MINDWIRE_LEXORA_URL or localhost default
        result = await client.chat_completion(
            model="naysayer",
            max_tokens=1500,
            messages=[
                ChatMessage(role="system", content=_TERSE_CRITIC),
                ChatMessage(role="user", content=f"A proposer claims: {_PROPOSAL} Critique it."),
            ],
        )
    # The reply lives in content; reasoning_content is a *separate* sibling field.
    assert result.content.strip(), "content must be non-empty for a normal completion"
    assert "<think>" not in result.content, "content must not embed a <think> tag"
    # finish_reason is a normal stop (not truncated) for a 1-sentence reply.
    assert result.finish_reason in {"stop", None}
    # Whatever the deliberation, it is NOT the posted reply.
    if result.reasoning_content is not None:
        assert result.reasoning_content != result.content


@pytest.mark.manual
@pytest.mark.anyio
async def test_live_unknown_tier_is_fail_loud() -> None:
    async with LexoraClient() as client:
        with pytest.raises(LexoraHTTPError) as ei:
            await client.chat_completion(
                model="this-tier-does-not-exist",
                max_tokens=50,
                messages=[ChatMessage(role="user", content="hi")],
            )
    # 502 from the gateway: no silent fallback to another model (main order #1).
    assert ei.value.status_code is not None and ei.value.status_code >= 400


@pytest.mark.manual
@pytest.mark.anyio
async def test_live_naysayer_adapter_round_trip() -> None:
    captured: list[ReplyDraft] = []

    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    ctx = SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.NAYSAYER,
        own_instance_id="naysayer-1",
    )
    thread_ref = ThreadRef(
        project_id="spirrow-mindwire", thread_id="smoke-thread", chatroom_uri="mc://smoke"
    )
    event = ChatroomEvent(
        event_id="smoke-event",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=thread_ref,
        occurred_at=datetime.now(UTC),
        payload=NewMessagePayload(
            msg_id="p1", author="proposer", body=_PROPOSAL, parent_msg_id=None
        ),
    )

    adapter = NaysayerLexoraAdapter(health_check_on_spawn=True)  # default model="naysayer"
    try:
        handle = await adapter.spawn(thread_ref, Role.NAYSAYER, ctx)
        await adapter.deliver_event(handle, event)
    finally:
        await adapter.aclose()

    assert len(captured) == 1
    body = captured[0].body
    assert body.strip(), "the naysayer must produce a non-empty critique"
    assert captured[0].adapter_metadata["model"]
    # Eyeball the critique when running manually — it should disagree with the
    # plaintext-password proposal and reference it (citation-type review).
    print("\n--- naysayer critique ---\n" + body + "\n--- end ---")
