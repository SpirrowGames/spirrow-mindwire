"""Identity classification / normalisation tools (T-role-null-must-become-impossible read half).

Two responsibilities, kept small:

- :mod:`.normalize` — the ADR-2026-05-29-11 partition-key normalisation
  (lowercase + separator collapse to ``-``), applied at every join between a raw
  identity string observed in a message and the classification's canonical name;
  and a collision detector so a set of raw strings never silently merges two
  identities into one (the "injectivity gate" from the ADR summary).

- :mod:`.classification` — loads :file:`spec/identity/legitimate_roles.yaml` (the
  machine-readable form of :doc:`docs/identity-classification.md`) and derives
  ``allowed_roles = observed ∩ legitimate`` and ``residual = observed \\ legitimate``
  per identity, exactly as msg-1493 §2 / §3 specifies.

Neither module talks to the identity store — that is a magickit MCP surface this
repo does not own. Both modules are pure so ``scripts/identity_findings.py``
(live-magickit read) and any future write-half script (also live) can call them
identically without changing the derivation.
"""

from .classification import (
    ClassificationEntry,
    ClassificationError,
    DerivationResult,
    LegitimateRolesFile,
    default_classification_path,
    derive_allowed_and_residual,
    load_legitimate_roles,
)
from .normalize import (
    IdentityCollisionError,
    find_collisions,
    normalize_identity_key,
)

__all__ = [
    "ClassificationEntry",
    "ClassificationError",
    "DerivationResult",
    "IdentityCollisionError",
    "LegitimateRolesFile",
    "default_classification_path",
    "derive_allowed_and_residual",
    "find_collisions",
    "load_legitimate_roles",
    "normalize_identity_key",
]
