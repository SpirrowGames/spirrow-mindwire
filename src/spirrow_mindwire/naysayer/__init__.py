"""Independent-naysayer support: principles SOT + design-time context bundle.

ADR-2026-06-03-17 (design-time naysayer participation). The 5 principles live in
``spec/NAYSAYER_PRINCIPLES.md`` (versioned SOT) and are injected verbatim via
:func:`~spirrow_mindwire.naysayer.principles.build_preamble`; the deterministic
design-thread context gather is
:func:`~spirrow_mindwire.naysayer.context_bundle.build_context_bundle`.
"""

from __future__ import annotations

from .adr_index import build_adr_index_block, load_adr_index, parse_adr_index
from .context_bundle import BundleManifest, ContextBundle, build_context_bundle
from .principles import (
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
    build_preamble,
    load_principles,
    principles_version,
)

__all__ = [
    "NAYSAYER_MODEL_TIER",
    "NAYSAYER_UPSTREAM_MODEL",
    "BundleManifest",
    "ContextBundle",
    "build_adr_index_block",
    "build_context_bundle",
    "build_preamble",
    "load_adr_index",
    "load_principles",
    "parse_adr_index",
    "principles_version",
]
