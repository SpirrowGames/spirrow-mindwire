"""Identity classification / normalisation tools (T-role-null-must-become-impossible read half).

Two responsibilities, kept small:

- :mod:`.embodiment` — the ADR-2026-09-14-21 spawnability rule: which
  identities the conductor may start a session for (embodiment =
  ``terminal_coding_agent``) and which it must hand to a human instead.

- :mod:`.normalize` — the ADR-2026-05-29-11 partition-key normalisation
  (lowercase + separator collapse to ``-``), applied at every join between a raw
  identity string observed in a message and the classification's canonical name;
  and a collision detector so a set of raw strings never silently merges two
  identities into one (the "injectivity gate" from the ADR summary).

- :mod:`.classification` — loads :file:`spec/identity/legitimate_roles.yaml` (the
  machine-readable form of :doc:`docs/identity-classification.md`) and derives
  ``allowed_roles = legitimate``, ``residual = observed \\ legitimate`` and
  ``unused = legitimate \\ observed`` per identity, exactly as msg-1493 §2 / §3
  specifies once corrected by msg-1585 §3 (the observation feeds the two report
  sets, never the entitlement).

- :mod:`.registration` — the write half (msg-1704 / msg-1706): the one
  constructor of ``upsert_identity`` arguments and the two-way
  ``kind`` ⟺ (``allowed_roles``, ``independence_class``) guard that checks both
  outgoing payloads and records read back from the store.

None of these modules talks to the identity store — that is a magickit MCP surface this
repo does not own. Both modules are pure so ``scripts/identity_findings.py``
(live-magickit read) and any future write-half script (also live) can call them
identically without changing the derivation.
"""

from .classification import (
    MACHINE_INDEPENDENCE_CLASS,
    ClassificationEntry,
    ClassificationError,
    DerivationResult,
    LegitimateRolesFile,
    default_classification_path,
    derive_allowed_and_residual,
    load_legitimate_roles,
)
from .embodiment import (
    DEFAULT_IDENTITY_EMBODIMENT,
    EMBODIMENT_VALUES,
    SPAWNABLE_EMBODIMENT,
    blocked_embodiment,
    normalize_embodiment_table,
)
from .normalize import (
    IdentityCollisionError,
    find_collisions,
    normalize_identity_key,
)
from .registration import (
    GuardViolation,
    RegistrationRefusedError,
    build_upsert_identity_args,
    check_identity_against_classification,
    check_store_record,
)

__all__ = [
    "DEFAULT_IDENTITY_EMBODIMENT",
    "EMBODIMENT_VALUES",
    "MACHINE_INDEPENDENCE_CLASS",
    "SPAWNABLE_EMBODIMENT",
    "ClassificationEntry",
    "ClassificationError",
    "DerivationResult",
    "GuardViolation",
    "IdentityCollisionError",
    "LegitimateRolesFile",
    "RegistrationRefusedError",
    "blocked_embodiment",
    "build_upsert_identity_args",
    "check_identity_against_classification",
    "check_store_record",
    "default_classification_path",
    "derive_allowed_and_residual",
    "find_collisions",
    "load_legitimate_roles",
    "normalize_embodiment_table",
    "normalize_identity_key",
]
