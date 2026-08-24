"""ADR-2026-05-29-11 partition-key normalisation + injectivity gate.

The ADR title (as available via ``spec/adr_index.yaml``): *author/identity
partition キー正規化 (lowercase + 区切り正規化 + 単射性 gate + strict-by-default)*.
The ADR body is in a docs repository this file cannot reach, so nothing here is
reconstructed from the body — only the surface rule the title names is
implemented, plus the shape existing callers in this repo already rely on.

The normalisation is intentionally weak:

- casefold
- collapse any run of whitespace / underscore / hyphen to a single ``-``
- strip

Two names that differ only in casing or separator style land on one key
(``Bohr`` / ``bohr`` / ``Bohr`` with a trailing space; ``pr-gate-relay`` /
``pr_gate_relay`` / ``PR Gate Relay``). This is the same rule that
:func:`~spirrow_mindwire.conductor.head_skip._normalize_token` has been running
since it was added, and that this module now re-exposes as the public entry
point so a caller that needs the SAME rule at a different call site does not
copy it (a second spelling of a rule is a second place for it to be wrong —
:mod:`~spirrow_mindwire.conductor.head_skip`'s own docstring makes exactly this
point).

**Injectivity gate**: :func:`find_collisions` reports groups where two or more
raw strings collapse to the same normalised key. In the head-skip caller the
rule is *"injective on the alphabet of names ADR-11 allows"*, so a collision
there means the input violated the alphabet. In the identity-findings caller,
where the input is real chatroom author strings from a live corpus, a collision
is exactly the "**登録前に衝突を測る**" measurement Bohr's msg-1487 §6 asked
for — a signal that registering both raw forms under one identity would
silently merge two different actors. The gate REPORTS collisions rather than
resolving them; msg-1487 §6 point 1 ("**衝突は登録せず報告**") is enforced by
callers refusing to write when a collision is present, not by this module
picking a winner.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

__all__ = [
    "IdentityCollisionError",
    "find_collisions",
    "normalize_identity_key",
]

_SEP_RE = re.compile(r"[\s_-]+")


def normalize_identity_key(name: str) -> str:
    """Return the ADR-11 canonical key for ``name`` (see module docstring for the rule).

    Empty / whitespace-only input returns ``""``. The rule is total — every string
    has a canonical form — so a caller who wants to reject unusable names should
    check the result against ``""`` (which is itself a possible collision target
    if multiple raw strings normalise to it: see :func:`find_collisions`).
    """
    if not name:
        return ""
    return _SEP_RE.sub("-", name.strip()).casefold()


def find_collisions(names: Iterable[str]) -> dict[str, list[str]]:
    """Group raw strings by their normalised key; return groups with 2+ raws.

    Duplicate raw strings are folded (a name appearing three times unchanged is
    NOT a collision — the same name is the same identity, that is the point of
    normalisation). Only genuinely different raw spellings mapping to one key
    are reported: ``{"pr-gate-relay": ["pr-gate-relay", "PR_Gate_Relay"]}``.

    The order of raw strings within each group is the order they were first
    seen, so a caller writing a finding can point at the offender predictably
    ("the older spelling is X, the new one that collided with it is Y").

    Callers must **not** treat this as "safe to write one canonical form": that
    is exactly the "silent merge" msg-1487 §6 forbids. On a non-empty result,
    the write must be withheld and the group surfaced as a finding.
    """
    seen: dict[str, list[str]] = defaultdict(list)
    for raw in names:
        key = normalize_identity_key(raw)
        if raw in seen[key]:
            continue
        seen[key].append(raw)
    return {key: raws for key, raws in seen.items() if len(raws) > 1}


class IdentityCollisionError(ValueError):
    """Raised by callers that promised strict-by-default injectivity.

    This module never raises; callers wrap :func:`find_collisions` in whatever
    strictness their context requires (a script writing a report tolerates
    collisions and prints them; a code path about to register an identity in
    Prismind treats a collision as a hard stop). Providing the exception here
    keeps the vocabulary in one place.

    ``groups`` is the result of :func:`find_collisions` — the normalised keys
    mapped to the raw spellings that collided, so the message can name them
    without the caller reformatting.
    """

    def __init__(self, groups: dict[str, list[str]]) -> None:
        joined = "; ".join(f"{key!r}: {raws!r}" for key, raws in sorted(groups.items()))
        super().__init__(
            f"ADR-11 injectivity violation — {len(groups)} normalised key(s) collide: {joined}"
        )
        self.groups = groups
