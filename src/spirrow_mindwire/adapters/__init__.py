"""RoleAdapter implementations (ADR-2026-05-21-06 §3.1).

Each adapter runs one role on one thread behind the
:class:`spirrow_mindwire.ports.RoleAdapter` Protocol. Phase 1 shipped the
proposer/implementer adapter (:class:`ClaudeCodeSdkAdapter`); Stage 2 of
the Phase 2 dogfood roadmap adds the independent naysayer
(:class:`NaysayerLexoraAdapter`, a different model family via Lexora) so a
two-role proposer↔naysayer thread can run — all without touching the
dispatcher.
"""

from __future__ import annotations

from .claude_code_sdk import (
    ClaudeCodeSdkAdapter,
    ClaudeCodeSdkDeliveryError,
    ClaudeCodeSdkHaltError,
    ClaudeCodeSdkHealthError,
    ClaudeCodeSdkSpawnError,
)
from .implementer import (
    ImplementerAllowlistError,
    ImplementerSdkAdapter,
    ImplementerSdkDeliveryError,
    ImplementerSdkHaltError,
    ImplementerSdkHealthError,
    ImplementerSdkSpawnError,
    classify_tool_call,
)
from .naysayer_lexora import (
    NaysayerLexoraAdapter,
    NaysayerLexoraDeliveryError,
    NaysayerLexoraHaltError,
    NaysayerLexoraHealthError,
    NaysayerLexoraSpawnError,
)
from .naysayer_pr_review import (
    NaysayerPrReviewAdapter,
    NaysayerPrReviewDeliveryError,
    NaysayerPrReviewHaltError,
    NaysayerPrReviewHealthError,
    NaysayerPrReviewSpawnError,
)

__all__ = [
    "ClaudeCodeSdkAdapter",
    "ClaudeCodeSdkDeliveryError",
    "ClaudeCodeSdkHaltError",
    "ClaudeCodeSdkHealthError",
    "ClaudeCodeSdkSpawnError",
    "ImplementerAllowlistError",
    "ImplementerSdkAdapter",
    "ImplementerSdkDeliveryError",
    "ImplementerSdkHaltError",
    "ImplementerSdkHealthError",
    "ImplementerSdkSpawnError",
    "NaysayerLexoraAdapter",
    "NaysayerLexoraDeliveryError",
    "NaysayerLexoraHaltError",
    "NaysayerLexoraHealthError",
    "NaysayerLexoraSpawnError",
    "NaysayerPrReviewAdapter",
    "NaysayerPrReviewDeliveryError",
    "NaysayerPrReviewHaltError",
    "NaysayerPrReviewHealthError",
    "NaysayerPrReviewSpawnError",
    "classify_tool_call",
]
