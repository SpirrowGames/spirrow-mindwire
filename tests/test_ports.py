"""Tests for ADR-2026-05-21-06 §3 Ports (PR-A / Step 0).

Structural conformance only — full adapter behaviour is T11 (PR-B) and the
registry's ``qualified_for`` policy is T13 (PR-C).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    HealthStatus,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


class _FakeAdapter:
    """Minimal concrete adapter; must satisfy the RoleAdapter Protocol."""

    adapter_id = "fake"
    capabilities = frozenset({Capability.POST_REPLY})

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        return SessionHandle(
            session_id="01JS",
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        return None

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(state=SessionState.IDLE, last_active_at=_TS, error=None, details={})


def test_fake_adapter_satisfies_roleadapter_protocol() -> None:
    # Static structural conformance is the real assertion (mypy); the
    # runtime checks just exercise the attributes.
    adapter: RoleAdapter = _FakeAdapter()
    assert adapter.adapter_id == "fake"
    assert Capability.POST_REPLY in adapter.capabilities


def test_spawn_context_holds_own_role() -> None:
    async def _on_reply(_: ReplyDraft) -> None: ...

    async def _on_event_log(_: Event) -> None: ...

    ctx = SpawnContext(on_reply=_on_reply, on_event_log=_on_event_log, own_role=Role.NAYSAYER)
    assert ctx.own_role is Role.NAYSAYER
