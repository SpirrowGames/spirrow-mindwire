"""Tests for ``NaysayerSdkAdapter`` — the naysayer as a Gemini-backed loop agent.

The Claude Agent SDK session is replaced by a fake client injected via
``client_factory``; the ``ClaudeAgentOptions`` passed to the factory are captured
so we can assert the independence wiring (``ANTHROPIC_BASE_URL`` + model) and the
verbatim 5-principles injection (ADR-17 D-1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.naysayer_sdk import (
    NaysayerSdkAdapter,
    NaysayerSdkSpawnError,
    build_naysayer_system_prompt,
)
from spirrow_mindwire.obligations import load_manifest
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

_TS = datetime(2026, 6, 4, tzinfo=UTC)
_BASE_URL = "http://lexora.local:8110"

# Loop-readable obligations manifest — required by the adapter and the prompt
# builder now that the verdict-constraint clause has been MOVED to it (§N.4).
# Loaded once at import time; the manifest is immutable.
_OBLIGATIONS = load_manifest()


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="gemini")


def _result(*, is_error: bool = False, result: str | None = "ok") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=is_error,
        num_turns=1,
        session_id="test",
        stop_reason="end_turn",
        result=result,
    )


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.connected = False
        self.queries: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _factory(client: _FakeClient, captured_options: list[Any]) -> Callable[[Any], Any]:
    def make(options: Any) -> _FakeClient:
        captured_options.append(options)
        return client

    return make


def _ctx(captured: list[ReplyDraft]) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.NAYSAYER,
        own_instance_id="naysayer-1",
    )


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="T-design", chatroom_uri="mc://t/1")


def _event(*, author: str = "Bohr", body: str = "proposing X", msg_id: str = "m1") -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author=author, body=body, parent_msg_id=None),
    )


def test_capabilities_naysayer_qualified_no_execute() -> None:
    caps = NaysayerSdkAdapter.capabilities
    assert Capability.NAYSAYER_QUALIFIED in caps
    assert Capability.EXECUTE_CODE not in caps
    assert Capability.READ_THREAD in caps
    assert Capability.POST_REPLY in caps


def test_satisfies_roleadapter_protocol(tmp_path: Path) -> None:
    adapter: RoleAdapter = NaysayerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=_BASE_URL
    )
    assert adapter.adapter_id == "naysayer-sdk"


def test_system_prompt_injects_principles_verbatim() -> None:
    prompt = build_naysayer_system_prompt(obligations=_OBLIGATIONS)
    assert "silence is negligence" in prompt  # the SOT, verbatim
    assert "principles_version=" in prompt
    assert "independent naysayer" in prompt  # role instructions follow


def test_system_prompt_injects_handoff_protocol() -> None:
    # PR-2b-1: the naysayer ends each critique with a NEXT: line so the conductor can chain.
    prompt = build_naysayer_system_prompt(obligations=_OBLIGATIONS)
    assert "Conductor handoff protocol" in prompt
    assert "NEXT:" in prompt
    # The verdict-constraint clause is now delivered via OBL-VERDICT-CONSTRAINT
    # (moved out of source, injected from the manifest). The assertion is on the
    # rendered prompt so the injection wiring itself is under test, not just the
    # manifest text — repointed at the assembled prompt per the Tier-C GO
    # ("existing tests: repoint at the rendered prompt, do not delete").
    assert "advisory, not a veto" in prompt
    assert "[OBL-VERDICT-CONSTRAINT]" in prompt


def test_system_prompt_injects_adr_index_from_a_fixture_manifest(tmp_path: Path) -> None:
    # N-2: the deterministic in-repo ADR index is injected so the agent's worldview is
    # not bounded by what the thread happens to cite. ``repo_root`` is a TEST affordance
    # for pointing at a fixture manifest — see the regression test below for why
    # production callers must never pass it.
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(
        'adrs:\n  - id: ADR-2026-05-31-15\n    title: "independence gradation"\n',
        encoding="utf-8",
    )
    prompt = build_naysayer_system_prompt(obligations=_OBLIGATIONS, repo_root=tmp_path)
    assert "ADR index (id + title)" in prompt
    assert "ADR-2026-05-31-15 — independence gradation" in prompt


def test_system_prompt_defaults_to_mindwires_own_manifest() -> None:
    # The default must resolve to THIS repo's committed manifest, not the caller's cwd.
    prompt = build_naysayer_system_prompt(obligations=_OBLIGATIONS)
    assert "ADR index (id + title)" in prompt
    assert "UNAVAILABLE" not in prompt


def test_adapter_injects_the_index_even_though_the_reviewed_repo_has_none(
    tmp_path: Path,
) -> None:
    """Regression: the reviewed repo never carries MindWire's ADR manifest.

    ``tmp_path`` stands in for the repo under review — like the real one, it has no
    ``spec/adr_index.yaml``. The adapter used to pass this cwd through as ``repo_root``,
    so ``load_adr_index`` fail-opened to ``()`` and EVERY design-time review ran with the
    "ADR index — UNAVAILABLE" block. It was invisible because the old version of this
    test planted a manifest inside ``tmp_path`` first, asserting a condition the
    conductor path can never satisfy.

    Asserting on the constructed prompt (not on a helper) is the point: the defect lived
    in the wiring between the two, which is precisely what a helper-level test misses.
    """
    assert not (tmp_path / "spec" / "adr_index.yaml").exists()  # as in production

    adapter = NaysayerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=_BASE_URL
    )

    prompt = adapter._system_prompt
    assert "ADR index (id + title)" in prompt
    assert "UNAVAILABLE" not in prompt
    # A real entry from the committed manifest — proves it is MindWire's, not a stub.
    assert "ADR-2026-05-31-15" in prompt


@pytest.mark.anyio
async def test_spawn_fails_closed_without_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDWIRE_NAYSAYER_BASE_URL", raising=False)
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=""
    )  # no URL, no env
    with pytest.raises(NaysayerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))


@pytest.mark.anyio
async def test_options_route_to_gemini_tier(tmp_path: Path) -> None:
    opts: list[Any] = []
    client = _FakeClient([_assistant("…"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, opts),
    )
    await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    # Independence: inference is pinned to the Lexora Gemini tier, never api.anthropic.com.
    assert opts[0].env["ANTHROPIC_BASE_URL"] == _BASE_URL
    assert opts[0].model == "naysayer"
    assert "silence is negligence" in opts[0].system_prompt  # principles injected (D-1)


@pytest.mark.anyio
async def test_deliver_event_posts_critique(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    client = _FakeClient(
        [_assistant("This over-scopes. "), _assistant("VERDICT: object."), _result()]
    )
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event())
    assert len(captured) == 1
    assert captured[0].body == "This over-scopes. VERDICT: object."
    assert captured[0].adapter_metadata["adapter_id"] == "naysayer-sdk"
    assert (await adapter.health(handle)).state is SessionState.IDLE


@pytest.mark.anyio
async def test_self_post_is_filtered(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    client = _FakeClient([_assistant("x"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="naysayer-1"))  # our own echoed post
    assert captured == []
    assert client.queries == []
