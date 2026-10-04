"""S3b-3 (Bohr msg-6523 §2): mindwire's wake lookup reaches magickit's conclusion (ADR-11).

The gateway sends a parsed ``STOP: blocked-on … wake:<w>`` to magickit as ``disposition.wake``,
and magickit refuses a wake that is not a registered identity (``DispositionWakeUnknownError``).
magickit's rule, copied below from the source, is: look the wake up as given; if the registry
says "not registered", look it up again by its ADR-11 key. If mindwire resolved a different set
of spellings, one of two things would go wrong silently:

* mindwire accepts a spelling magickit refuses → a refused disposition (the gateway then re-posts
  without it, PR #455, so the stop is recorded with no disposition);
* magickit accepts a spelling mindwire refuses → the line reads MALFORMED and nothing is sent for
  a stop magickit would have recorded.

Sources, both read at spirrow-magickit ``eda77a8ba1dcb5be331970d62f4be3762b286e53``
(main, 2026-10-04):

* ``src/magickit/mcp/tools/chatroom.py`` ``_lookup_wake`` (L1637-1650) — modelled by
  :func:`_magickit_lookup_wake`;
* ``tests/unit/test_disposition_gate.py`` — the registry (``_REGISTRY``) and the wake vectors of
  ``test_unregistered_wake_is_refused`` / ``test_wake_is_matched_by_its_adr11_key`` /
  ``test_registered_wake_spelling_is_looked_up_once``, copied into :data:`_MAGICKIT_WAKE_VECTORS`.

The ADR-11 key itself is pinned by ``tests/fixtures/adr11_normalize_vectors.json`` (canonical in
this repository, copied by magickit); its vectors are reused here as wake spellings. The
ADR-2026-05-29-11 body is not readable from this repository, so nothing here is checked against
its text — only against magickit's code and tests named above.

What is compared is *existence* (is the wake a registered identity?). The two further rules
mindwire applies on top — no ``human`` wake (B-5) and no implementer wake (S3b-2) — are
mindwire's own and are pinned in ``test_conductor_stop_line.py`` /
``test_stop_disposition_slice3b.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spirrow_mindwire.conductor.disposition import disposition_payload
from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    StopStatus,
    _wake_lookup,
    resolve_handoff,
)
from spirrow_mindwire.identity import normalize_identity_key
from spirrow_mindwire.value_objects import Role

_VECTORS = Path(__file__).resolve().parent / "fixtures" / "adr11_normalize_vectors.json"

#: magickit ``_REGISTRY`` (test_disposition_gate.py), the agent identities only — a human
#: identity can never be a mindwire wake (B-5), so it has no mindwire roster role to map to.
_MAGICKIT_AGENT_REGISTRY = ("Heisenberg", "pr-gate-relay")

#: (wake as written, magickit verdict: registered?) — from the three magickit tests named above.
_MAGICKIT_WAKE_VECTORS: tuple[tuple[str, bool], ...] = (
    ("Nobody", False),
    ("none", False),
    ("Einstien", False),
    ("PR_Gate_Relay", True),
    ("Heisenberg", True),
)


def _magickit_lookup_wake(registry: frozenset[str], wake: str) -> str | None:
    """magickit ``_lookup_wake``: as given, then by the ADR-11 key. Returns the record found."""
    if wake in registry:
        return wake
    key = normalize_identity_key(wake)
    if not key or key == wake:
        return None
    return key if key in registry else None


def _roster(names: tuple[str, ...] | list[str]) -> dict[str, Role]:
    # The role is irrelevant to existence; proposer keeps every name wake-eligible.
    return dict.fromkeys(names, Role.PROPOSER)


@pytest.mark.parametrize(("wake", "registered"), _MAGICKIT_WAKE_VECTORS)
def test_wake_existence_matches_magickits_tests(wake: str, registered: bool) -> None:
    registry = frozenset(_MAGICKIT_AGENT_REGISTRY)
    assert (_magickit_lookup_wake(registry, wake) is not None) is registered  # the copied model
    assert (_wake_lookup(_roster(_MAGICKIT_AGENT_REGISTRY), wake) is not None) is registered


@pytest.mark.parametrize(("wake", "registered"), _MAGICKIT_WAKE_VECTORS)
def test_the_wake_mindwire_sends_is_one_magickit_finds(wake: str, registered: bool) -> None:
    """Whatever mindwire sends is the roster's canonical spelling, which magickit finds as is."""
    stop = resolve_handoff(
        f"x\n\nSTOP: blocked-on thread:T-a wake:{wake}\nNEXT: none",
        _roster(_MAGICKIT_AGENT_REGISTRY),
    ).stop_line
    assert stop is not None
    payload = disposition_payload(stop)
    if not registered:
        assert stop.status is StopStatus.MALFORMED and payload is None
        return
    assert payload is not None
    sent = payload["wake"]
    assert _magickit_lookup_wake(frozenset(_MAGICKIT_AGENT_REGISTRY), sent) == sent


def _spelling_vectors() -> list[tuple[str, str]]:
    """ADR-11 vectors usable as a wake: no whitespace (the STOP grammar's ``wake:\\S+``), a
    non-empty key, and not a reserved token (MALFORMED by B-5 before any lookup)."""
    data = json.loads(_VECTORS.read_text(encoding="utf-8"))
    return [
        (raw, key)
        for raw, key in data["normalize"]
        if raw
        and not any(ch.isspace() for ch in raw)
        and key
        and key not in (HUMAN_TOKEN, NONE_TOKEN)
    ]


def test_spelling_vectors_are_not_empty() -> None:
    assert _spelling_vectors(), "no usable vectors — the parametrised test would pass vacuously"


@pytest.mark.parametrize(("raw", "key"), _spelling_vectors())
def test_every_spelling_magickit_resolves_by_key_resolves_here(raw: str, key: str) -> None:
    """Registry holding the key spelling only (e.g. ``pr-gate-relay``): magickit finds ``raw``
    through its ADR-11 key, so mindwire must resolve it too, to that same record."""
    registry = frozenset({key})
    assert _magickit_lookup_wake(registry, raw) == key
    stop = resolve_handoff(
        f"x\n\nSTOP: blocked-on thread:T-a wake:{raw}\nNEXT: none", _roster([key])
    ).stop_line
    assert stop is not None and stop.status is StopStatus.BLOCKED_ON
    assert stop.wake == key
