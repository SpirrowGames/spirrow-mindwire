"""One time budget for an SDK ``connect()`` (T43 follow-up, Bohr msg-5053 D-4).

``ClaudeSDKClient.connect()`` starts the Claude Code CLI subprocess and waits for its
handshake. The SDK puts no upper bound on that wait, so an adapter that awaits it bare can sit
there for as long as the process lives: no exception, no stop reason, nothing posted. The
implementer has bounded its connect since v12 (``T-auto-backgrounded-command-hangs-conductor-4h``);
this module is the same budget for the adapters that had none.

- :data:`DEFAULT_CONNECT_TIMEOUT_SECONDS` is the one number. The implementer's spawn budget is
  defined from it, so the adapters cannot drift apart.
- :func:`connect_bounded` raises :class:`SdkConnectTimeoutError` only when **this** budget ran
  out. A ``TimeoutError`` raised inside ``connect()`` for a reason of its own (a socket, a
  subprocess wait) propagates as itself: it is a connect failure, not a connect that was still
  going when we stopped waiting, and the two get different treatment upstream (only the second
  is retried by the conductor).

What this does not do: reap the subprocess. A caller that gets :class:`SdkConnectTimeoutError`
owns the client and must shut it down.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

#: Seconds an SDK ``connect()`` may take. The implementer's value since v12, reused unchanged.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 60.0


class _Connectable(Protocol):
    async def connect(self) -> None: ...


class SdkConnectTimeoutError(RuntimeError):
    """The SDK ``connect()`` was still running when the caller's budget ran out."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"SDK connect did not finish inside {timeout_seconds}s")
        self.timeout_seconds = timeout_seconds


async def connect_bounded(client: _Connectable, timeout_seconds: float) -> None:
    """Await ``client.connect()`` for at most ``timeout_seconds``.

    ``asyncio.timeout`` rather than ``asyncio.wait_for`` so that ``expired()`` can tell our
    deadline from a ``TimeoutError`` the SDK raised itself (same reasoning as the turn budget in
    ``claude_code_sdk._drain_reply``).
    """
    budget = asyncio.timeout(timeout_seconds)
    try:
        async with budget:
            await client.connect()
    except TimeoutError as exc:
        if budget.expired():
            raise SdkConnectTimeoutError(timeout_seconds) from exc
        raise


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "SdkConnectTimeoutError",
    "connect_bounded",
]
