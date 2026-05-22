"""AdapterRegistry implementation — ADR-2026-05-21-06 §3.2 (T13 skeleton).

Dict-backed trivial registry (Phase 1). ``qualified_for`` gates each role
to adapters carrying the required capabilities, enforcing **naysayer
independence at the architecture level** (ADR-05 §4/§5, inherited by
ADR-06): the naysayer slot requires ``NAYSAYER_QUALIFIED``, so a
same-model adapter — e.g. the T11 ``ClaudeCodeSdkAdapter``, which
deliberately omits that capability — is excluded from naysayer
candidates. This is the T11↔T13 cross-check anchor (capabilities ↔
qualified_for); the live cross-check against the real adapter lands once
both PR-B and PR-C are in main.

Phase 2 will likely replace the static role→capability map with a policy
object (ADR-06 §3.2 / §5).
"""

from __future__ import annotations

from spirrow_mindwire.ports import RoleAdapter
from spirrow_mindwire.value_objects import Capability, Role

# Minimum capabilities an adapter must declare to fill each role slot
# (ADR-05 §4 capability gating; naysayer independence via NAYSAYER_QUALIFIED).
_REQUIRED_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.PROPOSER: frozenset({Capability.READ_THREAD, Capability.POST_REPLY}),
    Role.NAYSAYER: frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}
    ),
    Role.IMPLEMENTER: frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}
    ),
}


class AdapterAlreadyRegisteredError(ValueError):
    """Raised when registering an ``adapter_id`` that is already present."""


class AdapterNotFoundError(KeyError):
    """Raised by :meth:`InMemoryAdapterRegistry.get` for an unknown ``adapter_id``."""


class InMemoryAdapterRegistry:
    """Dict-backed :class:`spirrow_mindwire.ports.AdapterRegistry` (Phase 1)."""

    def __init__(self) -> None:
        self._by_id: dict[str, RoleAdapter] = {}

    def register(self, adapter: RoleAdapter) -> None:
        if adapter.adapter_id in self._by_id:
            raise AdapterAlreadyRegisteredError(adapter.adapter_id)
        self._by_id[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> RoleAdapter:
        try:
            return self._by_id[adapter_id]
        except KeyError as exc:
            raise AdapterNotFoundError(adapter_id) from exc

    def qualified_for(self, role: Role) -> list[RoleAdapter]:
        """Return registered adapters whose capabilities satisfy ``role``.

        Naysayer independence (ADR-05 §5) is enforced here: only adapters
        carrying ``NAYSAYER_QUALIFIED`` qualify for the naysayer slot, so a
        same-model adapter (e.g. T11 ``ClaudeCodeSdkAdapter``) is excluded.
        """
        required = _REQUIRED_CAPABILITIES[role]
        return [adapter for adapter in self._by_id.values() if required <= adapter.capabilities]


__all__ = [
    "AdapterAlreadyRegisteredError",
    "AdapterNotFoundError",
    "InMemoryAdapterRegistry",
]
