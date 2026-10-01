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


class AdapterSpawnTimeoutError(AdapterSpawnError):
    """``spawn`` did not finish inside the adapter's own init time budget.

    The one spawn failure the conductor retries (T43, Bohr msg-5053 D-2): a connect that ran
    out of time says nothing about whether the next one will, whereas every other spawn failure
    (no base URL, ``adapter.job_assign_missed``, a refused preflight) fails the same way twice.
    It lives at the Port level so the conductor can tell the two apart without importing an
    adapter's concrete class.

    ``adapter_id`` and ``timeout_s`` are required, not optional: they are two of the fields of
    the ``spawn.timeout`` event, and only the adapter that raised knows them.
    """

    def __init__(self, message: str, *, adapter_id: str, timeout_s: float) -> None:
        super().__init__(message)
        self.adapter_id = adapter_id
        self.timeout_s = timeout_s


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

    ``code`` is the declared contract for the ``adapter.*`` catalog code a delivery failure
    carries on the exception itself (human msg-4910, Einstein msg-5001). A subclass that stands
    for exactly one failure overrides it at class level (``adapter.turn_timeout``,
    ``adapter.shutdown_failed``). The base value is ``None``, deliberately not a generic string:
    the conductor's ``error_code=`` falls back to the concrete class name when there is no code,
    and a default such as ``adapter.delivery_failed`` would erase which adapter failed.
    """

    code: str | None = None


__all__ = [
    "AdapterDeliveryError",
    "AdapterError",
    "AdapterHaltError",
    "AdapterHealthError",
    "AdapterSpawnError",
    "AdapterSpawnTimeoutError",
]
