"""Phase 1 Ports — ADR-2026-05-21-06 (Accepted v2.1) §3.

The hexagonal Port contracts the dispatcher depends on. Implementations
(adapters, the dispatcher-internal registry) live elsewhere; this module
is the §3 boundary only (value objects are :mod:`value_objects`, §2; the
§3.4 Port exception catalog is :mod:`exceptions`).

This module *supersedes* the ADR-05 ``RoleAdapter`` Protocol for Phase 1
(ADR-06 Metadata "Supersedes (partial)"): the signature SOT is here, not
in ADR-05.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from .value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    HealthStatus,
    ReplyDraft,
    Role,
    SessionHandle,
    ThreadRef,
)

# --------------------------------------------------------------------------- #
# §3.1 RoleAdapter + SpawnContext
# --------------------------------------------------------------------------- #


@dataclass
class SpawnContext:
    """The dispatcher's "exit" handed to an adapter at spawn (ADR-06 §3.1).

    The adapter does not know about the ChatRoom; it interacts only through
    these callbacks (I1 knowledge boundary).

    - ``on_reply``: emit a :class:`~value_objects.ReplyDraft` back to the
      dispatcher (which completes + posts it). Adapters MUST call this
      **synchronously within ``deliver_event``** (not from a detached
      background task), so the dispatcher's per-session serialization
      sequences reply ordering and the I5 ``reply_seq`` counter.
    - ``on_event_log``: append an observational :class:`~value_objects.Event`
      to the flat JSONL log. **Observational only** (I7): no control-flow
      semantics; the dispatcher isolates a raising callback and never lets
      it break the main flow.
    - ``own_role``: the role this session was spawned for. Added in v2.1
      (Gap-2 (b)): lets the adapter self-filter its own posts echoed back
      to it (defensive against a dispatcher routing bug causing a
      self-reply loop).
    - ``own_instance_id``: the stable per-instance label the dispatcher
      assigned this session (ADR-2026-05-24-08 §2.1). The adapter stamps it
      onto the :class:`~value_objects.SessionHandle` it returns from
      ``spawn`` — the handle is keyed by identity (I2) so the dispatcher
      cannot re-stamp it afterwards, hence it is delivered here at spawn
      (mirrors ``own_role``; keeps the ``RoleAdapter.spawn`` signature
      unchanged per ADR-08 §2.3).

    Not ``frozen`` (holds live callables); not identity-constrained.
    """

    on_reply: Callable[[ReplyDraft], Awaitable[None]]
    on_event_log: Callable[[Event], Awaitable[None]]
    own_role: Role
    own_instance_id: str


class RoleAdapter(Protocol):
    """A model+transport adapter that runs one role on one thread (§3.1).

    Lifecycle: ``spawn`` → ``deliver_event``\\* → ``halt``, with ``health``
    callable at any time. Failure raises from the §3.4 Port exception
    catalog (:mod:`exceptions`): :class:`~exceptions.AdapterSpawnError`,
    :class:`~exceptions.AdapterDeliveryError`,
    :class:`~exceptions.AdapterHaltError`,
    :class:`~exceptions.AdapterHealthError` respectively.

    SEAM (ADR-2026-05-24-08 §2.3): the "model+transport" bundle this Protocol
    still carries (one adapter = one transport x one role x one model) is the
    Phase 2 decomposition point into ``TransportAdapter`` (the terminal /
    browser pipe) x ``InstanceSpec`` (role + model_binding + capabilities).
    Phase 1 keeps this signature unchanged — the bundle is split only once a
    second transport (browser) appears; splitting now is YAGNI. Instance
    individuation is achieved earlier via ``SessionHandle.instance_id``
    (delivered through :attr:`SpawnContext.own_instance_id`).
    """

    adapter_id: str
    capabilities: frozenset[Capability]

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
        ctx: SpawnContext,
    ) -> SessionHandle:
        """Start a session for ``role`` on ``thread_ref``.

        Raises :class:`~exceptions.AdapterSpawnError` on failure.
        """
        ...

    async def deliver_event(
        self,
        handle: SessionHandle,
        event: ChatroomEvent,
    ) -> None:
        """Deliver one event to the session.

        Per-session call order is FIFO by ``event.occurred_at`` (I9). The
        adapter may treat receipt order as occurrence order. Raises
        :class:`~exceptions.AdapterDeliveryError` on failure.
        """
        ...

    async def halt(
        self,
        handle: SessionHandle,
        *,
        grace: timedelta = timedelta(seconds=5),
    ) -> None:
        """Stop the session, allowing ``grace`` for graceful shutdown.

        Idempotent no-op on a terminal (halted / failed) or already-halting
        session (I8): returns immediately, no state transition, no raise.
        Raises :class:`~exceptions.AdapterHaltError` only on a genuine
        halt failure (graceful timeout / force-kill failure).
        """
        ...

    async def health(
        self,
        handle: SessionHandle,
    ) -> HealthStatus:
        """Report current session health.

        Raises :class:`~exceptions.AdapterHealthError` if health cannot be
        determined (adapter unresponsive / internal state inconsistent).
        """
        ...


# --------------------------------------------------------------------------- #
# §3.2 AdapterRegistry
# --------------------------------------------------------------------------- #


class AdapterRegistry(Protocol):
    """Registry of available adapters, queried by role (ADR-06 §3.2).

    Phase 1 implementation is a trivial dict-backed registry;
    ``qualified_for`` policy-isation is expected in Phase 2. The Phase 1
    ``qualified_for`` gates the naysayer slot to adapters carrying
    :attr:`~value_objects.Capability.NAYSAYER_QUALIFIED` (architecture-level
    independence enforcement inherited from ADR-05).
    """

    def register(self, adapter: RoleAdapter) -> None: ...

    def get(self, adapter_id: str) -> RoleAdapter: ...

    def qualified_for(self, role: Role) -> list[RoleAdapter]: ...


# §3.3 ChatroomGateway is intentionally NOT a Port in Phase 1 (ADR-06 §3.3):
# it is a dispatcher-internal implementation (T12), revisited for Port-isation
# in Phase 2 if a second ChatRoom implementation appears.


__all__ = [
    "AdapterRegistry",
    "RoleAdapter",
    "SpawnContext",
]
