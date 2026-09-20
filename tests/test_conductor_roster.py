"""Tests for :mod:`spirrow_mindwire.conductor.roster` — the shared role → identity resolver.

The resolver is the SOT both the conductor daemon and the hand-run PR-gate driver
(``scripts/naysayer_review.py``) call to look up "which persona fills a role in this
project's roster". Before it existed the two lanes rolled their own answer and drifted,
and every REQUEST_CHANGES verdict fired by hand routed to ``NEXT: human`` instead of the
implementer — T-hand-fired-gate-cannot-name-the-implementer msg-3842. These tests pin:

- the three outcomes exhaustively (exactly-one / zero / several) with the default
  :attr:`~spirrow_mindwire.value_objects.Role.IMPLEMENTER` role,
- the *parameterised* behaviour — the resolver honours the ``role`` argument passed by
  the caller rather than a hardcoded literal, so the ``implementer_role`` ctor seam on
  :class:`~spirrow_mindwire.conductor.core.Conductor` survives extraction (msg-3884 Obj-1
  BLOCKING catch: hardcoding the literal in the exception message would misdirect an
  operator to debug the wrong config key when the role differs from the default).
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.conductor.roster import RoleResolutionError, derive_identity_by_role
from spirrow_mindwire.value_objects import Role


def test_returns_the_sole_persona_when_exactly_one_fills_the_role() -> None:
    roster = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}
    assert derive_identity_by_role(roster, Role.IMPLEMENTER) == "Heisenberg"


def test_raises_with_empty_matches_when_no_persona_fills_the_role() -> None:
    # The fail-loud path the hand-run driver takes: an operator at the terminal sees the
    # exact miswiring, so the message must name the role searched for (not a literal) and
    # ``matches`` must be exposed so a caller can inspect it without re-scanning the roster
    # (which would recreate the disjoint state D-1 is here to prevent — msg-3849).
    roster = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER}
    with pytest.raises(RoleResolutionError) as excinfo:
        derive_identity_by_role(roster, Role.IMPLEMENTER)
    err = excinfo.value
    assert err.role is Role.IMPLEMENTER
    assert err.matches == []
    assert "no IMPLEMENTER persona" in str(err)


def test_raises_with_all_matches_when_multiple_personas_fill_the_role() -> None:
    # Order-preserving: the caller sees which entries collided in roster iteration order,
    # so an operator reading stderr can point at exactly the config lines to reconcile.
    roster = {
        "Bohr": Role.PROPOSER,
        "Heisenberg": Role.IMPLEMENTER,
        "Feynman": Role.IMPLEMENTER,
        "Einstein": Role.NAYSAYER,
    }
    with pytest.raises(RoleResolutionError) as excinfo:
        derive_identity_by_role(roster, Role.IMPLEMENTER)
    err = excinfo.value
    assert err.role is Role.IMPLEMENTER
    assert err.matches == ["Heisenberg", "Feynman"]
    assert "multiple IMPLEMENTER personas" in str(err)


def test_resolver_honours_the_role_argument_and_interpolates_it_into_the_message() -> None:
    # msg-3884 Obj-1 (BLOCKING): the ``implementer_role`` seam is preserved as a first-class
    # argument. Both the search and the error message must use THAT role, not a literal —
    # otherwise a non-default role stderr would send the operator to the wrong config key.
    roster = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER}
    # PROPOSER search on this roster returns "Bohr" — pins that the search actually uses the
    # passed role rather than falling back to Role.IMPLEMENTER.
    assert derive_identity_by_role(roster, Role.PROPOSER) == "Bohr"
    # No-match on a non-default role: the exception's role attribute and rendered message
    # must reflect THAT role — this is what msg-3884 caught as a BLOCKING defect on the
    # hardcoded-literal design (Obj-1).
    roster_no_proposer = {"Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}
    with pytest.raises(RoleResolutionError) as excinfo:
        derive_identity_by_role(roster_no_proposer, Role.PROPOSER)
    err = excinfo.value
    assert err.role is Role.PROPOSER
    assert "no PROPOSER persona" in str(err)
    assert "IMPLEMENTER" not in str(err)  # the exact defect Obj-1 pinned
