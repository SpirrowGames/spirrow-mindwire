"""Allowlist wrapper that makes a chatroom MCP client read-only.

The single home of :class:`ReadOnlyMcp` (T-stalled-pr-has-no-detector msg-4685 §1).
It used to exist as two byte-for-byte copies, one in ``scripts/unregistered_threads.py``
and one in ``scripts/pr_review_sweep_phase0.py``. Both now import it from here, and so
does the stall ledger's chatroom adapter.

The wrapper enforces a caller's write-zero claim mechanically: any tool name outside the
caller's allowlist raises :class:`ReadOnlyViolationError` before the call reaches the
network. Each caller keeps its own allowlist and its own label, so the error text each
script produced is unchanged ("<label> is read-only; refusing to call ...").
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol


class ReadOnlyViolationError(RuntimeError):
    """A tool outside the wrapper's allowlist was requested."""


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class ReadOnlyMcp:
    """Pass allowlisted tool calls through to ``inner``; refuse everything else."""

    def __init__(self, inner: ToolCaller, allowed: Iterable[str], *, label: str) -> None:
        self._inner = inner
        self._allowed = frozenset(allowed)
        self._label = label

    @property
    def allowed(self) -> frozenset[str]:
        return self._allowed

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._allowed:
            raise ReadOnlyViolationError(
                f"{self._label} is read-only; refusing to call {name!r}. "
                f"Allowed: {sorted(self._allowed)}"
            )
        return await self._inner.call_tool(name, arguments)
