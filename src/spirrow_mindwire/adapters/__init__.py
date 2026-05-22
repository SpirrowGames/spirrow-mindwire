"""Phase 1 RoleAdapter implementations (ADR-2026-05-21-06 §3.1).

Each adapter runs one role on one thread behind the
:class:`spirrow_mindwire.ports.RoleAdapter` Protocol. Phase 1 ships a
single adapter (:class:`ClaudeCodeSdkAdapter`); Phase 2 adds others
(Gemini, browser-driven, etc.) without touching the dispatcher.
"""

from __future__ import annotations

from .claude_code_sdk import (
    ClaudeCodeSdkAdapter,
    ClaudeCodeSdkDeliveryError,
    ClaudeCodeSdkHaltError,
    ClaudeCodeSdkHealthError,
    ClaudeCodeSdkSpawnError,
)

__all__ = [
    "ClaudeCodeSdkAdapter",
    "ClaudeCodeSdkDeliveryError",
    "ClaudeCodeSdkHaltError",
    "ClaudeCodeSdkHealthError",
    "ClaudeCodeSdkSpawnError",
]
