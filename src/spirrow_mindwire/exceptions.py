"""Port-level exception catalog — ADR-2026-05-21-06 (Accepted v2.1) §3.4.

One base class per :class:`~ports.RoleAdapter` method failure mode. The
adapter/dispatcher exception responsibility split is fixed at the ADR
level so that new exception types appearing during Phase 1 dogfooding are
absorbed by this hierarchy.

| Port method      | Failure raise class      | typical failure                              |
|------------------|--------------------------|----------------------------------------------|
| ``spawn()``      | ``AdapterSpawnError``    | subprocess launch / gateway connect / no model |
| ``halt()``       | ``AdapterHaltError``     | graceful timeout / force-kill failure        |
| ``health()``     | ``AdapterHealthError``   | adapter unresponsive / inconsistent state    |
| ``deliver_event()`` | ``AdapterDeliveryError`` | session closed / payload validation failure |

Adapters specialise these via subclasses (e.g. ``ClaudeCodeSdkSpawnError``,
T11). The dispatcher catches the **base** class; subclass detail flows to
observability via :attr:`~value_objects.HealthStatus.details` (I2). The
exception's catalog *code* follows the inherited error-code catalog
convention (ADR-06 §1 / §6); adapter failure codes use the ``adapter.*``
namespace (e.g. ``adapter.timeout``, see :class:`~value_objects.ErrorInfo`).
That code is cross-referenced through **``HealthStatus.error.code``**
(``ErrorInfo.code``) as the single SOT — ADR-06 §3.4 Option (i) — and is
**not** duplicated in ``HealthStatus.details`` (§3-axis dual-management
avoidance).

The common :class:`AdapterError` root lets the dispatcher catch all Port
failures with one ``except`` while still allowing per-method discrimination
("base class hierarchy で吸収", ADR-06 §3.4).
"""

from __future__ import annotations


class AdapterError(Exception):
    """Root of the §3.4 Port exception hierarchy.

    Common ancestor of the four per-method base classes so the dispatcher
    can ``except AdapterError`` broadly. Not raised directly — adapters
    raise (a subclass of) one of the four method-specific classes below.
    """


class AdapterSpawnError(AdapterError):
    """Raised by ``RoleAdapter.spawn`` on failure (ADR-06 §3.4).

    Typical: subprocess launch failure / gateway connection failure /
    model unavailable.
    """


class AdapterHaltError(AdapterError):
    """Raised by ``RoleAdapter.halt`` on a genuine halt failure (§3.4).

    Typical: graceful-shutdown timeout / force-kill failure. Note: calling
    ``halt`` on a terminal or already-halting session is an idempotent
    no-op (I8) and does **not** raise this.
    """


class AdapterHealthError(AdapterError):
    """Raised by ``RoleAdapter.health`` when health is undeterminable (§3.4).

    Typical: adapter unresponsive / internal state inconsistent.
    """


class AdapterDeliveryError(AdapterError):
    """Raised by ``RoleAdapter.deliver_event`` on failure (ADR-06 §3.4).

    Typical: session closed / payload validation failure.
    """


__all__ = [
    "AdapterDeliveryError",
    "AdapterError",
    "AdapterHaltError",
    "AdapterHealthError",
    "AdapterSpawnError",
]
