"""Write half of T-role-null-must-become-impossible: payload constructor + guard.

Two entry points, both pure (no I/O):

- :func:`build_upsert_identity_args` is the **only** function in this repo that
  builds the arguments for magickit's ``upsert_identity`` from a classification
  entry. ``docs/identity-classification.md`` names this function instead of
  restating a payload (msg-1706 §2), because the payload it used to restate
  (``independence_class = null``) is one the live API rejects (msg-1703 §2).
  The constructor runs the guard on its own output and raises if the guard
  finds anything, so the write path cannot emit a payload the guard would reject.

- :func:`check_identity_against_classification` is the two-way guard from
  msg-1706 §1. It covers both directions of the biconditional, and all of it
  lives here in mindwire, next to the classification YAML that is its source of
  truth. None of it goes into Prismind, which does not own the role vocabulary:

  ============  ==========================================================
  ``kind``      required of the record
  ============  ==========================================================
  machine       ``allowed_roles == []`` **and** ``independence_class ==
                MACHINE_INDEPENDENCE_CLASS``
  participant   ``allowed_roles != []`` **and** ``independence_class !=
                MACHINE_INDEPENDENCE_CLASS``
  ============  ==========================================================

  The participant side is deliberately NOT written as ``∈ {main-chain,
  independent}``. Nobody has measured the set of values a participant can take,
  so it stays open (msg-1706 §1).

  The guard also reports two things that are not biconditional violations but
  would be tampering with a classified record: ``allowed_roles`` differing from
  ``legitimate`` (the entitlement is ``legitimate``, msg-1585 §3), and a
  participant whose ``independence_class`` differs from what the YAML declares.
  The same function checks a payload before it is sent
  (:func:`build_upsert_identity_args`) and a record read back from the live
  store (``scripts/register_identities.py`` read-back and
  ``scripts/identity_findings.py`` store check, msg-1706 §4). That gives one rule
  with several callers.

This module does not know the live enum. It holds no local copy of Prismind's
``INDEPENDENCE_CLASS_VALUES``, whether as a tuple or as an import from a sibling
checkout. The one verification that the deployed service accepts
:data:`MACHINE_INDEPENDENCE_CLASS` is the live ``upsert_identity`` result
(msg-1706 §2 / DoD 2 / DoD 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .classification import MACHINE_INDEPENDENCE_CLASS, ClassificationEntry

__all__ = [
    "GuardViolation",
    "RegistrationRefusedError",
    "build_upsert_identity_args",
    "check_identity_against_classification",
    "check_store_record",
]


@dataclass(frozen=True)
class GuardViolation:
    """One way a record (or payload) disagrees with its classification entry.

    ``code`` is a stable machine token (so a findings reader can count by kind);
    ``detail`` is the human explanation.
    """

    code: str
    detail: str


class RegistrationRefusedError(ValueError):
    """The constructor refused to build a payload for this entry."""


def check_identity_against_classification(
    entry: ClassificationEntry,
    *,
    independence_class: object,
    allowed_roles: object,
) -> list[GuardViolation]:
    """Return every violation of the msg-1706 §1 guard (empty list = consistent).

    ``independence_class`` / ``allowed_roles`` are typed ``object`` because this
    function also checks records read back from the live store. Such a record is
    external input and may hold the wrong type, and a wrong type is reported as a
    violation, not raised.
    """
    violations: list[GuardViolation] = []
    roles: frozenset[str] | None
    if isinstance(allowed_roles, list | tuple | set | frozenset) and all(
        isinstance(r, str) for r in allowed_roles
    ):
        roles = frozenset(allowed_roles)
    else:
        roles = None
        violations.append(
            GuardViolation(
                "allowed_roles_malformed",
                f"allowed_roles must be a list of strings, got {allowed_roles!r}",
            )
        )

    if entry.kind == "machine":
        if roles is not None and roles:
            violations.append(
                GuardViolation(
                    "machine_has_roles",
                    f"kind=machine requires allowed_roles=[]; got {sorted(roles)}",
                )
            )
        if independence_class != MACHINE_INDEPENDENCE_CLASS:
            violations.append(
                GuardViolation(
                    "machine_wrong_independence_class",
                    f"kind=machine requires independence_class="
                    f"{MACHINE_INDEPENDENCE_CLASS!r}; got {independence_class!r}",
                )
            )
    else:  # participant — the loader admits no third kind
        if roles is not None and not roles:
            violations.append(
                GuardViolation(
                    "participant_has_no_roles",
                    "kind=participant requires allowed_roles != [] "
                    "(an empty list would erase attestation; msg-1484 §5)",
                )
            )
        if not isinstance(independence_class, str) or not independence_class:
            violations.append(
                GuardViolation(
                    "participant_missing_independence_class",
                    f"kind=participant requires a non-empty independence_class; "
                    f"got {independence_class!r}",
                )
            )
        elif independence_class == MACHINE_INDEPENDENCE_CLASS:
            violations.append(
                GuardViolation(
                    "participant_marked_machine",
                    f"kind=participant must not carry independence_class="
                    f"{MACHINE_INDEPENDENCE_CLASS!r}",
                )
            )
        elif entry.independence_class is not None and independence_class != (
            entry.independence_class
        ):
            violations.append(
                GuardViolation(
                    "independence_class_differs_from_classification",
                    f"classification declares {entry.independence_class!r}; "
                    f"got {independence_class!r}",
                )
            )

    if roles is not None and roles != entry.legitimate:
        violations.append(
            GuardViolation(
                "allowed_roles_differ_from_legitimate",
                f"allowed_roles {sorted(roles)} != legitimate {sorted(entry.legitimate)} "
                f"(allowed_roles := legitimate, msg-1585 §3)",
            )
        )
    return violations


def build_upsert_identity_args(entry: ClassificationEntry) -> dict[str, Any]:
    """Build the ``upsert_identity`` arguments for ``entry``, the one constructor.

    - machine → ``independence_class = MACHINE_INDEPENDENCE_CLASS``,
      ``allowed_roles = []``.
    - participant → ``independence_class = entry.independence_class`` (from the
      YAML), ``allowed_roles = sorted(entry.legitimate)``.

    ``embodiment`` and ``persona_description`` are omitted. ``embodiment`` is
    deprecated on the identity record (ADR-2026-05-29-12) and out of scope
    (msg-1179 §9). ``persona_description``: an omitted value preserves an
    existing one.

    Raises :class:`RegistrationRefusedError` if a participant has no declared
    ``independence_class``, since guessing it is what msg-1179 §5 forbids. It
    also raises if the guard rejects the constructed payload, which can only
    happen if this function and the guard have drifted apart.
    """
    if entry.kind == "machine":
        independence_class = MACHINE_INDEPENDENCE_CLASS
    else:
        if entry.independence_class is None:
            raise RegistrationRefusedError(
                f"{entry.name!r}: kind=participant has no independence_class in the "
                f"classification; refusing to guess one (msg-1179 §5)"
            )
        independence_class = entry.independence_class
    args: dict[str, Any] = {
        "identity_name": entry.name,
        "independence_class": independence_class,
        "allowed_roles": sorted(entry.legitimate),
    }
    violations = check_identity_against_classification(
        entry,
        independence_class=args["independence_class"],
        allowed_roles=args["allowed_roles"],
    )
    if violations:
        raise RegistrationRefusedError(
            f"{entry.name!r}: constructed payload fails the guard: "
            + "; ".join(v.detail for v in violations)
        )
    return args


def check_store_record(
    entry: ClassificationEntry, get_identity_result: Mapping[str, Any]
) -> dict[str, Any]:
    """Fold one ``get_identity`` response into a findings row for ``entry``.

    ``status`` is taken from the response (``found`` / ``not_found`` /
    ``lookup_failed`` / ``contract_violation``). The guard runs only on
    ``found``. A classified identity with no record is reported as such, not
    treated as consistent. Not being registered is a state the write half has
    to close, and before this PR every classified identity was in that state.
    """
    status = str(get_identity_result.get("status") or "unknown")
    row: dict[str, Any] = {"identity_name": entry.name, "kind": entry.kind, "status": status}
    if status != "found":
        row["violations"] = []
        return row
    record = get_identity_result.get("identity")
    if not isinstance(record, Mapping):
        row["violations"] = [
            {"code": "record_malformed", "detail": f"identity is not a mapping: {record!r}"}
        ]
        return row
    row["independence_class"] = record.get("independence_class")
    row["allowed_roles"] = record.get("allowed_roles")
    row["violations"] = [
        {"code": v.code, "detail": v.detail}
        for v in check_identity_against_classification(
            entry,
            independence_class=record.get("independence_class"),
            allowed_roles=record.get("allowed_roles"),
        )
    ]
    return row
