"""Decision-request composer subpackage — T-decision-request-composer.

A parked ``NEXT: human`` thread deserves a self-contained question on the
human's phone — one line plus the concrete options and their trade-offs —
not the current bare "you have a decision to make, look at the chatroom"
ping. The composer produces that structured question; a wrapper renders
it for Discord and the ops dashboard.

The subpackage is deliberately structured to mirror the shape of the rest
of the codebase (a Protocol port + value objects + a Stub implementation +
a CLI): the port names neither the model nor the transport (msg-1370 §2,
"port はモデルもトランスポートも名指ししないこと"), and Claude Code sits
behind the port as one adapter — swappable — instead of being wired in.

Public surface:
    :class:`DecisionRequestComposer`  the Protocol every backend satisfies
    :class:`DecisionRequestInput`     what the composer is given
    :class:`DecisionRequestOutput`    what the composer produces
    :class:`DecisionOption`           one option row (id / label / gain / loss)
    :class:`ComposerStatus`           outcome enum for the on-disk envelope
    :class:`DecisionRequestEnvelope`  the JSON row stored in the pending cache
    :class:`StubComposer`             a deterministic no-model backend
                                      (used by tests and by the CLI's
                                      ``--backend stub`` mode)

The CLI entry point (``mindwire-compose-decision``) lives in
:mod:`.cli`. It is what the PowerShell sweep wrapper invokes on each
``NEXT: human`` stop, exactly once per ``reason:last_msg`` signature
(I-3 — see :mod:`.cli` for the dedup mechanism).
"""

from __future__ import annotations

from .claude_code import DEFAULT_COMPOSER_IDENTITY, PROMPT_VERSION, ClaudeCodeComposer
from .exceptions import (
    DecisionComposerEmptyError,
    DecisionComposerError,
    DecisionComposerTimeoutError,
)
from .ports import DecisionRequestComposer
from .stub import StubComposer
from .value_objects import (
    ComposerStatus,
    DecisionOption,
    DecisionRequestEnvelope,
    DecisionRequestInput,
    DecisionRequestOutput,
    ThreadTailMessage,
)

__all__ = [
    "DEFAULT_COMPOSER_IDENTITY",
    "PROMPT_VERSION",
    "ClaudeCodeComposer",
    "ComposerStatus",
    "DecisionComposerEmptyError",
    "DecisionComposerError",
    "DecisionComposerTimeoutError",
    "DecisionOption",
    "DecisionRequestComposer",
    "DecisionRequestEnvelope",
    "DecisionRequestInput",
    "DecisionRequestOutput",
    "StubComposer",
    "ThreadTailMessage",
]
