"""Attestation supply — T-dispatched-turn-gets-one-message D-2.

The independence probe was taken once per *spawn* and re-stamped on every post
of that session, so the ``attest:`` line measured independence per process, not
per verdict. Measured on the live corpus 2026-08-16: 39 attested posts carried
24 distinct probes; one probe was stamped on 5 posts spanning 8m59s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.obligations import ObligationsManifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import (
    AttestationRecord,
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionState,
    ThreadRef,
)

_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")


class _FakeSdkClient:
    """Minimal SDK session: one assistant text block then a successful result."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text="VERDICT: APPROVE")], model="gemini")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="t",
            stop_reason="end_turn",
            result="ok",
        )

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


def _new_message(msg_id: str) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=f"T-x:{msg_id}",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_TR,
        occurred_at=datetime.now(UTC),
        payload=NewMessagePayload(
            msg_id=msg_id, author="Bohr", body="a proposal", parent_msg_id=None
        ),
    )


def _record(probe: str) -> AttestationRecord:
    return AttestationRecord(
        tier="naysayer",
        backend="gemini",
        expected="gemini",
        route="host:8110",
        probe=probe,
        at=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_consecutive_naysayer_turns_carry_different_probes(
    tmp_path: Path, obligations: ObligationsManifest
) -> None:
    """D-2's own acceptance criterion, stated verbatim by the proposer."""
    from spirrow_mindwire.adapters.naysayer_sdk import NaysayerSdkAdapter

    probes = iter([_record("cost-row#1"), _record("cost-row#2"), _record("cost-row#3")])
    seen: list[str | None] = []

    async def _preflight() -> AttestationRecord:
        return next(probes)

    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=obligations,
        inference_base_url="http://lexora.local:8110",
        client_factory=lambda options: _FakeSdkClient(),
        preflight=_preflight,
    )

    async def _on_reply(draft: ReplyDraft) -> None:
        record = adapter.attestation_record(handle)
        seen.append(None if record is None else record.probe)

    async def _on_log(event: Any) -> None: ...

    ctx = SpawnContext(
        on_reply=_on_reply,
        on_event_log=_on_log,
        own_role=Role.NAYSAYER,
        own_instance_id="Einstein",
    )
    handle = await adapter.spawn(_TR, Role.NAYSAYER, ctx)
    for i in (1, 2):
        await adapter.deliver_event(handle, _new_message(f"msg-{i}"))

    assert seen == ["cost-row#2", "cost-row#3"], (
        "each verdict must carry the probe taken for it, not the spawn's"
    )
    assert seen[0] != seen[1]


@pytest.mark.anyio
async def test_a_failed_per_turn_preflight_refuses_to_post_the_verdict(
    tmp_path: Path, obligations: ObligationsManifest
) -> None:
    """Fail-closed is the whole point: an unattested verdict must never reach the thread."""
    from spirrow_mindwire.adapters.naysayer_sdk import (
        NaysayerSdkAdapter,
        NaysayerSdkDeliveryError,
    )

    calls = {"n": 0}

    async def _preflight() -> AttestationRecord:
        calls["n"] += 1
        if calls["n"] == 1:
            return _record("cost-row#1")
        raise RuntimeError("gateway attributed the tier to anthropic")

    replies: list[ReplyDraft] = []

    async def _on_reply(draft: ReplyDraft) -> None:
        replies.append(draft)

    async def _on_log(event: Any) -> None: ...

    client = _FakeSdkClient()
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=obligations,
        inference_base_url="http://lexora.local:8110",
        client_factory=lambda options: client,
        preflight=_preflight,
    )
    ctx = SpawnContext(
        on_reply=_on_reply,
        on_event_log=_on_log,
        own_role=Role.NAYSAYER,
        own_instance_id="Einstein",
    )
    handle = await adapter.spawn(_TR, Role.NAYSAYER, ctx)
    with pytest.raises(NaysayerSdkDeliveryError):
        await adapter.deliver_event(handle, _new_message("msg-1"))
    assert replies == [], "an unattested verdict reached the thread"
    assert client.queries == [], "the model was asked before the route was re-checked"
    health = await adapter.health(handle)
    assert health.state is SessionState.FAILED
