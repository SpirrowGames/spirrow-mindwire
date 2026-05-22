"""Tests for InMemoryAdapterRegistry (ADR-06 §3.2, T13 PR-C skeleton)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from spirrow_mindwire.dispatcher.registry import (
    AdapterAlreadyRegisteredError,
    AdapterNotFoundError,
    InMemoryAdapterRegistry,
)
from spirrow_mindwire.ports import AdapterRegistry, RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    HealthStatus,
    Role,
    SessionHandle,
    ThreadRef,
)


class _StubAdapter:
    """Minimal RoleAdapter stub declaring an id + capabilities (no behaviour)."""

    def __init__(self, adapter_id: str, capabilities: frozenset[Capability]) -> None:
        self.adapter_id = adapter_id
        self.capabilities = capabilities

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        raise NotImplementedError

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        raise NotImplementedError

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        raise NotImplementedError

    async def health(self, handle: SessionHandle) -> HealthStatus:
        raise NotImplementedError


def _claude_code_like() -> _StubAdapter:
    # Mirrors the T11 ClaudeCodeSdkAdapter capabilities (NO NAYSAYER_QUALIFIED).
    return _StubAdapter(
        "claude-code-sdk",
        frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}),
    )


def _naysayer_capable() -> _StubAdapter:
    return _StubAdapter(
        "gemini",
        frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}),
    )


def test_register_and_get() -> None:
    reg = InMemoryAdapterRegistry()
    adapter = _claude_code_like()
    reg.register(adapter)
    assert reg.get("claude-code-sdk") is adapter


def test_get_unknown_raises() -> None:
    reg = InMemoryAdapterRegistry()
    with pytest.raises(AdapterNotFoundError):
        reg.get("nope")


def test_duplicate_register_raises() -> None:
    reg = InMemoryAdapterRegistry()
    reg.register(_claude_code_like())
    with pytest.raises(AdapterAlreadyRegisteredError):
        reg.register(_claude_code_like())


def test_qualified_for_naysayer_excludes_non_qualified() -> None:
    reg = InMemoryAdapterRegistry()
    cc = _claude_code_like()
    gem = _naysayer_capable()
    reg.register(cc)
    reg.register(gem)
    naysayers = reg.qualified_for(Role.NAYSAYER)
    # Architecture-level independence (ADR-05 §5): same-model adapter excluded.
    assert cc not in naysayers
    assert gem in naysayers


def test_qualified_for_implementer_requires_execute_code() -> None:
    reg = InMemoryAdapterRegistry()
    cc = _claude_code_like()
    gem = _naysayer_capable()
    reg.register(cc)
    reg.register(gem)
    implementers = reg.qualified_for(Role.IMPLEMENTER)
    assert cc in implementers  # has EXECUTE_CODE
    assert gem not in implementers  # lacks EXECUTE_CODE


def test_qualified_for_proposer_accepts_basic() -> None:
    reg = InMemoryAdapterRegistry()
    cc = _claude_code_like()
    reg.register(cc)
    assert cc in reg.qualified_for(Role.PROPOSER)


def test_registry_and_stub_satisfy_protocols() -> None:
    # Static structural conformance (mypy) is the real assertion.
    reg: AdapterRegistry = InMemoryAdapterRegistry()
    adapter: RoleAdapter = _claude_code_like()
    reg.register(adapter)
    assert reg.qualified_for(Role.PROPOSER) == [adapter]
