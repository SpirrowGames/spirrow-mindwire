"""Independent-naysayer support: the 5-principles SOT + the deterministic ADR index.

The 5 principles live in ``spec/NAYSAYER_PRINCIPLES.md`` (versioned SOT) and are injected
verbatim via :func:`~spirrow_mindwire.naysayer.principles.build_preamble`. The complete ADR
index injected into the agent's summon is built by ``adr_index.build_adr_index_block``
(ADR-2026-06-04-19 N-2). ADR-19 superseded ADR-17's relay/bundle, so the ``context_bundle``
gather and the ``scripts/design_review.py`` relay were removed (N-4); the design-time naysayer
now participates as the ``NaysayerSdkAdapter`` summoned in the ordinary loop.
"""

from __future__ import annotations

from .adr_index import build_adr_index_block, load_adr_index, parse_adr_index
from .principles import (
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
    ObjectionClass,
    build_preamble,
    load_principles,
    objection_classes,
    principles_version,
)

__all__ = [
    "NAYSAYER_MODEL_TIER",
    "NAYSAYER_UPSTREAM_MODEL",
    "ObjectionClass",
    "build_adr_index_block",
    "build_preamble",
    "load_adr_index",
    "load_principles",
    "objection_classes",
    "parse_adr_index",
    "principles_version",
]
