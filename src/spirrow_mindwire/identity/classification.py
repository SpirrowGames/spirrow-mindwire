"""Loader + derivation for :file:`spec/identity/legitimate_roles.yaml`.

Two entry points:

- :func:`load_legitimate_roles` reads the YAML, validates the shape, applies
  the ADR-11 normalisation to every identity name, and rejects the file on
  either a schema violation or a collision (the "strict-by-default" side of
  ADR-11 — a classification file where two entries collide would silently
  drop one).

- :func:`derive_allowed_and_residual` implements msg-1493 §2 / §3 **as
  corrected by msg-1585 §3** (the Bohr post that follows msg-1536 in
  ``T-role-null-must-become-impossible``): given the observed role set (a
  live-corpus fact from ``scripts/identity_findings.py``) and the loaded
  classification, return ``(allowed_roles, residual, unused)``. msg-1493 §2's
  intersection ``observed ∩ legitimate`` is **withdrawn** — it could only ever
  narrow an entitlement, never grant one, so an identity that is not yet
  registered (whose claimed roles are therefore dropped to ``null``) could
  never bootstrap out of ``observed = ∅``. This is still the "by construction"
  derivation Einstein required in msg-1492: the caller never proposes values;
  it hands the observation to this function and receives the derived triple.

Neither entry point knows about the identity store. The write half calls this
same function to compute what to supply to ``upsert_identity``; the read half
calls it to build findings. One derivation, two callers — msg-1493 §2's "**規
則は 2 本にならない**".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .normalize import IdentityCollisionError, find_collisions, normalize_identity_key

__all__ = [
    "ClassificationEntry",
    "ClassificationError",
    "LegitimateRolesFile",
    "derive_allowed_and_residual",
    "load_legitimate_roles",
]


class ClassificationError(ValueError):
    """Raised by :func:`load_legitimate_roles` on schema or invariant violations."""


@dataclass(frozen=True)
class ClassificationEntry:
    """One row of :file:`spec/identity/legitimate_roles.yaml`.

    Attributes:
      name: the raw ``identity_name`` as written in code / the YAML entry
        (before ADR-11 normalisation). Preserved so a finding can quote the
        source spelling.
      key: :func:`~.normalize.normalize_identity_key` applied to :attr:`name`;
        this is what joins against the observed-corpus author strings (also
        normalised at query time).
      kind: ``"participant"`` or ``"machine"``.
      legitimate: the role set this identity may honestly claim. ``frozenset``
        so callers can compute intersections / differences without mutating
        the classification.
      primary_source: pointer into the repo (``"path::symbol"``).
      reason: prose justification.
    """

    name: str
    key: str
    kind: str
    legitimate: frozenset[str]
    primary_source: str
    reason: str


@dataclass(frozen=True)
class LegitimateRolesFile:
    """Parsed :file:`spec/identity/legitimate_roles.yaml`."""

    version: int
    entries: tuple[ClassificationEntry, ...]

    def by_key(self, key: str) -> ClassificationEntry | None:
        """Return the entry whose normalised key matches ``key``, or ``None``.

        The linear scan is fine — the file has O(10) entries and is loaded
        once per process. Building a dict is a premature optimisation that
        would just be a second cache to keep coherent with ``entries``.
        """
        for entry in self.entries:
            if entry.key == key:
                return entry
        return None


_VALID_KINDS = frozenset({"participant", "machine"})


def load_legitimate_roles(path: Path) -> LegitimateRolesFile:
    """Read, validate, and normalise the classification YAML.

    Raises :class:`ClassificationError` for a schema violation
    (missing/unknown fields, wrong types, kind-vs-legitimate mismatch) and
    :class:`IdentityCollisionError` for an ADR-11 injectivity violation
    across the file's identity names (two entries whose ``name`` normalises
    to one key). Both are hard stops rather than warnings — a classification
    file that has silently merged two identities is worse than no
    classification, because it looks authoritative.
    """
    raw_text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ClassificationError(f"{path}: YAML parse error: {exc}") from exc

    if not isinstance(data, dict):
        raise ClassificationError(f"{path}: top-level must be a mapping, got {type(data).__name__}")
    version = data.get("version")
    if version != 1:
        raise ClassificationError(f"{path}: version must be 1, got {version!r}")
    raw_entries = data.get("identities")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ClassificationError(f"{path}: `identities` must be a non-empty list")

    entries: list[ClassificationEntry] = []
    for i, raw in enumerate(raw_entries):
        entries.append(_parse_entry(raw, i, path))

    collisions = find_collisions(entry.name for entry in entries)
    if collisions:
        raise IdentityCollisionError(collisions)

    return LegitimateRolesFile(version=version, entries=tuple(entries))


def _parse_entry(raw: Any, index: int, path: Path) -> ClassificationEntry:
    if not isinstance(raw, dict):
        raise ClassificationError(
            f"{path}: identities[{index}] must be a mapping, got {type(raw).__name__}"
        )
    name = raw.get("name")
    kind = raw.get("kind")
    legitimate = raw.get("legitimate")
    primary_source = raw.get("primary_source")
    reason = raw.get("reason")
    if not isinstance(name, str) or not name.strip():
        raise ClassificationError(f"{path}: identities[{index}].name must be a non-empty string")
    if kind not in _VALID_KINDS:
        raise ClassificationError(
            f"{path}: identities[{index}].kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}"
        )
    if not isinstance(legitimate, list) or not all(isinstance(r, str) for r in legitimate):
        raise ClassificationError(
            f"{path}: identities[{index}].legitimate must be a list of strings"
        )
    if kind == "machine" and legitimate:
        raise ClassificationError(
            f"{path}: identities[{index}] kind=machine requires legitimate=[] "
            f"(machinery has no honest role claim; msg-1484 §2)"
        )
    if kind == "participant" and not legitimate:
        raise ClassificationError(
            f"{path}: identities[{index}] kind=participant requires len(legitimate) >= 1 "
            f"(a participant with no honest role would erase attestation; msg-1484 §5)"
        )
    if not isinstance(primary_source, str) or not primary_source.strip():
        raise ClassificationError(
            f"{path}: identities[{index}].primary_source must be a non-empty string"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ClassificationError(f"{path}: identities[{index}].reason must be a non-empty string")

    return ClassificationEntry(
        name=name,
        key=normalize_identity_key(name),
        kind=kind,
        legitimate=frozenset(legitimate),
        primary_source=primary_source,
        reason=reason,
    )


@dataclass(frozen=True)
class DerivationResult:
    """The output of msg-1493 §2 / §3 as corrected by msg-1585 §3.

    Attributes:
      allowed_roles: ``legitimate`` itself — the entitlement the classification
        grants. The observation is **not an input to this field**. This is what
        the write half MUST supply to ``upsert_identity``, and it is still
        never a value the caller proposed (msg-1492 Einstein: "by
        construction") — it comes from the reviewed YAML.
      residual: ``observed \\ legitimate``. Roles the identity has actually
        claimed but is not entitled to — fabrication evidence. Non-empty ⇒
        surfaced as a finding (msg-1493 §3), and registering that identity sits
        inside the write-set lock because its posts start being rejected.
      unused: ``legitimate \\ observed``. Rights the identity holds but has not
        exercised since the cutoff — what the withdrawn intersection was
        silently discarding. Blast radius is **zero** (allowing a role nobody
        uses rejects nothing) ⇒ ``unused ≠ ∅`` is **outside** the lock and is
        reported only (msg-1585 §3).
    """

    allowed_roles: frozenset[str]
    residual: frozenset[str]
    unused: frozenset[str]


def derive_allowed_and_residual(
    observed: Iterable[str], legitimate: Iterable[str]
) -> DerivationResult:
    """Return ``(legitimate, observed \\ legitimate, legitimate \\ observed)``.

    ``observed`` is the role set the identity has actually claimed on posts
    since the deploy cutoff (msg-1484 §4 scope); ``legitimate`` comes from the
    classification file. Both are already role strings (not enums) — the
    detector's join is on the ``message.role`` VALUE column, so the caller
    normalises to the string form once at query time and this function stays
    string-typed.

    An empty ``observed`` returns ``allowed_roles = legitimate``. The
    observation is not an input to the entitlement; it is the input to
    ``residual`` / ``unused``. An identity that is not yet registered has every
    role it claims **dropped** by ``chatroom_post_message`` (an unverified role
    is recorded as ``null``), so ``observed = ∅`` is not an accident but a
    certainty for exactly the identities the write half exists to register —
    under the withdrawn intersection, registration could never bootstrap
    (msg-1585 §1 / §3).

    ``legitimate`` may be empty (a machine identity per the YAML): then
    ``allowed_roles = ∅`` regardless of ``observed``, and every observed role
    becomes residual. This is by design — a machine that posted with any role
    stamped has fabricated attestation, and every one of those posts is
    evidence.
    """
    observed_set = frozenset(observed)
    legitimate_set = frozenset(legitimate)
    return DerivationResult(
        allowed_roles=legitimate_set,
        residual=observed_set - legitimate_set,
        unused=legitimate_set - observed_set,
    )


def default_classification_path() -> Path:
    """The in-tree path to :file:`spec/identity/legitimate_roles.yaml`.

    Kept as a function (not a module constant) so a test / caller with a
    different repo root can override the search without monkey-patching a
    module attribute. The path is relative to the repository root, not to
    this module: the YAML is the SoT humans edit, not a resource shipped
    inside the wheel.
    """
    # This file lives at src/spirrow_mindwire/identity/classification.py, so
    # the repo root is three parents up.
    return Path(__file__).resolve().parents[3] / "spec" / "identity" / "legitimate_roles.yaml"


def _also_export(_names: Mapping[str, Any]) -> None:
    """A no-op used to keep DerivationResult / default_classification_path visible.

    Both names are re-exported via ``__all__`` on the identity package, not
    from this module, so linters do not flag them as unused. This helper
    exists so a future refactor that removes the package-level re-export does
    not silently strip the names from the derivation surface.
    """


# Silence "imported but unused" if a future refactor drops the package re-export;
# DerivationResult and default_classification_path are part of the public API of
# this module even though __all__ only lists the frozen-classification-file names.
_also_export(
    {
        "DerivationResult": DerivationResult,
        "default_classification_path": default_classification_path,
    }
)
