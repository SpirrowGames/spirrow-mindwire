"""Lexora model-gateway client integration.

Lexora (``SpirrowGames`` internal) is an OpenAI-compatible gateway that
fronts several model backends behind tier names (``light`` / ``medium`` /
``heavy`` / ``naysayer``). The Stage 2 independent-naysayer RoleAdapter
calls the ``naysayer`` tier (DeepSeek V4-Flash) through
:class:`LexoraClient`.
"""

from __future__ import annotations

from .client import (
    ChatCompletion,
    ChatMessage,
    LexoraAPIError,
    LexoraChatClient,
    LexoraClient,
    LexoraError,
    LexoraHTTPError,
    lexora_url,
)

__all__ = [
    "ChatCompletion",
    "ChatMessage",
    "LexoraAPIError",
    "LexoraChatClient",
    "LexoraClient",
    "LexoraError",
    "LexoraHTTPError",
    "lexora_url",
]
