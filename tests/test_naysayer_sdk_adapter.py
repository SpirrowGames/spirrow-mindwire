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
from spirrow_mindwire.naysayer.preflight import PreflightError
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    AttestationRecord,
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionHandle,
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
        preflight=_preflight_ok(),
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
        preflight=_preflight_ok(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event())
    assert len(captured) == 1
    assert captured[0].body == "This over-scopes. VERDICT: object."
    assert captured[0].adapter_metadata["adapter_id"] == "naysayer-sdk"
    assert (await adapter.health(handle)).state is SessionState.IDLE


# --------------------------------------------------------------------------- #
# P-1c — stop discarding the ResultMessage (msg-953 §2 P-1c, Tier-C msg-954 §3)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_reply_metadata_retains_sdk_session_facts(tmp_path: Path) -> None:
    """``_drain_reply`` no longer throws the ``ResultMessage`` away.

    Before this change the adapter read ``is_error`` and dropped the rest, so
    ``session_id`` / ``duration_ms`` / ``num_turns`` never reached the event
    log and a turn left no operational trace at all (msg-950 §2 / msg-951 §5).
    """
    captured: list[ReplyDraft] = []
    client = _FakeClient([_assistant("critique"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
        preflight=_preflight_ok(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event())

    meta = captured[0].adapter_metadata
    assert meta["sdk_session_id"] == "test"
    assert meta["sdk_duration_ms"] == 10
    assert meta["sdk_num_turns"] == 1


@pytest.mark.anyio
async def test_reply_metadata_never_carries_the_model_echo(tmp_path: Path) -> None:
    """★ The ``model`` field is deliberately NOT retained.

    Tier-C msg-954 §3: "``.model`` は tier のエコーなので provenance に使わ
    ない". Lexora answers an Anthropic-compatible request by echoing the tier
    alias (``"naysayer"``), never the concrete backend model — measured in
    msg-950 §2. Recording it would put a value that LOOKS like provenance
    into the event log's ``model_id`` field, which is precisely the overclaim
    this whole thread exists to remove. The value of P-1c is operational
    observability, not proof of independence.

    An ``AssistantMessage`` in this fake carries ``model="gemini"``, so a
    naive "keep everything" implementation would leak exactly the misleading
    datum. Pinning the absence is the point of this test.
    """
    captured: list[ReplyDraft] = []
    client = _FakeClient([_assistant("critique"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
        preflight=_preflight_ok(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event())

    meta = captured[0].adapter_metadata
    assert "model" not in meta
    assert "model_id" not in meta
    assert "sdk_model" not in meta
    assert "gemini" not in repr(meta)


@pytest.mark.anyio
async def test_reply_body_is_unchanged_by_result_retention(tmp_path: Path) -> None:
    """Retaining the ``ResultMessage`` must not alter the posted critique text."""
    captured: list[ReplyDraft] = []
    client = _FakeClient([_assistant("a"), _assistant("b"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
        preflight=_preflight_ok(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event())
    assert captured[0].body == "ab"


@pytest.mark.anyio
async def test_self_post_is_filtered(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    client = _FakeClient([_assistant("x"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
        preflight=_preflight_ok(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="naysayer-1"))  # our own echoed post
    assert captured == []
    assert client.queries == []


# --------------------------------------------------------------------------- #
# P-2 — spawn-time preflight attestation (msg-953 §3 / Tier-C msg-954 §3)
# --------------------------------------------------------------------------- #


def _attestation(*, backend: str = "gemini") -> AttestationRecord:
    return AttestationRecord(
        tier="naysayer",
        backend=backend,
        expected="gemini",
        route="lexora.local:8110",
        probe="cost-row#6032",
        at=_TS,
    )


def _preflight_ok(calls: list[int] | None = None) -> Callable[[], Any]:
    async def run() -> AttestationRecord:
        if calls is not None:
            calls.append(1)
        return _attestation()

    return run


def _preflight_failing(exc: Exception) -> Callable[[], Any]:
    async def run() -> AttestationRecord:
        raise exc

    return run


@pytest.mark.anyio
async def test_spawn_runs_the_preflight_and_retains_the_record(tmp_path: Path) -> None:
    """The record reaches the dispatcher through the ``attestation_record`` getter.

    That getter is the seam P-1 opened (``dispatcher/core.py`` looks it up
    duck-typed, independently of ``source_marker_options``). Until now no
    adapter defined it, so the branch was inert; this is what makes it live.
    """
    calls: list[int] = []
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(_FakeClient([_assistant("x"), _result()]), []),
        preflight=_preflight_ok(calls),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert calls == [1]
    record = adapter.attestation_record(handle)
    assert record is not None
    assert record.backend == "gemini"


@pytest.mark.anyio
async def test_spawn_refuses_when_the_preflight_fails(tmp_path: Path) -> None:
    """★ Fail-closed: no session, therefore no turn, therefore no post.

    ``docs/deploy.md:73`` has always claimed the naysayer "refuses to spawn"
    without a working independent route; before P-2 the only thing checked was
    whether the env var was a non-empty string. This is the check that makes the
    refusal about the route actually resolving to the expected backend.
    """
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(_FakeClient([_assistant("x"), _result()]), []),
        preflight=_preflight_failing(PreflightError("backend mismatch: got vllm, expected gemini")),
    )
    with pytest.raises(NaysayerSdkSpawnError) as excinfo:
        await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert "backend mismatch" in str(excinfo.value)


@pytest.mark.anyio
async def test_failed_preflight_leaves_no_session_behind(tmp_path: Path) -> None:
    """A refused spawn must not leave a half-built session the loop could reuse."""
    client = _FakeClient([_assistant("x"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
        preflight=_preflight_failing(PreflightError("unreachable")),
    )
    with pytest.raises(NaysayerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert adapter._sessions == {}


@pytest.mark.anyio
async def test_preflight_runs_before_the_sdk_session_is_connected(tmp_path: Path) -> None:
    """Order matters: a failing preflight must not leave an SDK subprocess running.

    The SDK client is a real subprocess in production. Connecting first and then
    refusing would leak one per refused spawn, and ``halt`` is never called for a
    session that was never returned.
    """
    client = _FakeClient([_assistant("x"), _result()])
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(client, []),
        preflight=_preflight_failing(PreflightError("unreachable")),
    )
    with pytest.raises(NaysayerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert client.connected is False


@pytest.mark.anyio
async def test_attestation_record_is_none_for_an_unknown_handle(tmp_path: Path) -> None:
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url=_BASE_URL,
        client_factory=_factory(_FakeClient([]), []),
        preflight=_preflight_ok(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    other = SessionHandle(
        session_id="01JOTHER",
        instance_id="naysayer-1",
        adapter_id="naysayer-sdk",
        thread_ref=_thread_ref(),
        role=Role.NAYSAYER,
        started_at=_TS,
    )
    assert adapter.attestation_record(handle) is not None
    assert adapter.attestation_record(other) is None


@pytest.mark.anyio
async def test_missing_base_url_is_refused_without_burning_a_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap check stays first: no URL → refuse without paying for a probe."""
    monkeypatch.delenv("MINDWIRE_NAYSAYER_BASE_URL", raising=False)
    calls: list[int] = []
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="",
        preflight=_preflight_ok(calls),
    )
    with pytest.raises(NaysayerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert calls == []


def test_adapters_do_not_import_the_marker_builder() -> None:
    """msg-834 §2 (c) — including transitively, now that P-2 adds a module.

    ``naysayer/preflight.py`` needs the same credential-guarded route reducer
    the ``source:`` line uses. Importing it FROM ``source_marker`` would drag the
    marker builder into the adapter's import graph through the back door, so the
    reducer was extracted to its own module and both sides import that instead.

    Checked over the parsed import statements, not over the file text: the
    adapter legitimately *mentions* ``source_marker`` — its getter is called
    ``source_marker_options`` and its comments discuss the marker at length. A
    substring check would call that an import and be wrong.
    """
    import ast

    import spirrow_mindwire.adapters.naysayer_sdk as sdk_mod
    import spirrow_mindwire.naysayer.preflight as preflight_mod

    for module in (sdk_mod, preflight_mod):
        tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        offenders = [name for name in imported if name.split(".")[-1] == "source_marker"]
        assert offenders == [], f"{module.__name__} imports the marker builder: {offenders}"


@pytest.mark.anyio
async def test_end_to_end_a_naysayer_post_carries_both_marker_lines(tmp_path: Path) -> None:
    """★ The real adapter through the real dispatcher, onto a real posted body.

    Every other test here checks one hop. This one checks that the hops are
    joined: ``NaysayerSdkAdapter`` is the sole ``NAYSAYER_QUALIFIED`` adapter the
    registry hands the naysayer slot to, so this is the production path, and the
    thing being pinned is that a naysayer reply now leaves with BOTH lines —

        <!-- source: … route=… tier=… -->   what the session was configured to do
        <!-- attest: … backend=… -->        what the gateway's ledger said it did

    A unit test on the getter cannot see a dispatcher that fails to call it. The
    getter existed and returned records for a whole release while nothing read
    them; that is precisely the defect msg-960 caught, and only an end-to-end
    assertion would have caught it earlier.
    """
    from spirrow_mindwire.dispatcher.core import Dispatcher
    from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry

    class _Gateway:
        def __init__(self) -> None:
            self.posts: list[str] = []

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
            self.posts.append(body)
            return "posted-1"

    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://100.79.84.62:8110",
        client_factory=_factory(_FakeClient([_assistant("VERDICT: object."), _result()]), []),
        preflight=_preflight_ok(),
    )
    registry = InMemoryAdapterRegistry()
    registry.register(adapter)
    gateway = _Gateway()
    disp = Dispatcher(registry=registry, gateway=gateway)

    handle = await disp.spawn_instance(_thread_ref(), Role.NAYSAYER, "naysayer-1")
    await disp.dispatch(handle, _event(msg_id="m42"))

    lines = gateway.posts[0].rstrip().splitlines()
    assert lines[0] == "VERDICT: object."
    assert lines[-2] == (
        "<!-- source: tools=0 · mcp=0 · setting_sources=unset "
        "· route=100.79.84.62:8110 · tier=naysayer -->"
    )
    assert lines[-1] == (
        "<!-- attest: tier=naysayer · backend=gemini · expected=gemini "
        "· route=lexora.local:8110 · probe=cost-row#6032 · at=2026-06-04T00:00:00Z -->"
    )
