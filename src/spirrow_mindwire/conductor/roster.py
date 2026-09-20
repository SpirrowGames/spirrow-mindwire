"""Roster → role → identity resolver, shared by the daemon and hand-run drivers.

This is the single-SOT resolver that both the :class:`~.core.Conductor` and
:mod:`scripts.naysayer_review` must call to look up "which persona fills a given
role in this project's roster". Before this module existed, the daemon rolled its
own resolver inside ``Conductor`` and the hand-run PR-gate driver had no resolver
at all — so a manual fire silently passed ``implementer=None`` and every
``REQUEST_CHANGES`` verdict routed to ``NEXT: human`` instead of to the implementer
fix loop (T-hand-fired-gate-cannot-name-the-implementer msg-3842). Extracting the
resolver here eliminates that disjoint state: both lanes read the roster from the
same code path against the same ``[conductor].roster`` SOT.

The resolver is deliberately *role-parameterised* rather than hardcoded to
:attr:`~..value_objects.Role.IMPLEMENTER`. The conductor's ctor exposes an
``implementer_role`` seam that tests use to construct conductors with substitute
roles; carrying that seam through as a first-class argument keeps the "search
for role X" and "say we searched for role X" facts in one place. Hardcoding the
literal would recreate the same disjoint-state problem in the error message
(msg-3884 / msg-3885) — the exception would confidently claim a role was missing
that the caller never asked for.

Failure is loud: zero or several roster entries for the requested role raise
:class:`RoleResolutionError` carrying the ``role`` and the ``matches`` it found.
Callers that need a fail-safe fallback (the conductor's PR-gate dispatch path,
which fails to ``NEXT: human`` rather than crashing) explicitly catch the
exception and translate to ``None``; callers that need fail-loud (the hand-run
driver, where an operator is at the terminal and can fix the config) let it
propagate. The resolver itself never returns a sentinel — an empty string sentinel
was rejected in msg-3849 as dual-management of absence.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..value_objects import Role


class RoleResolutionError(ValueError):
    """Roster does not have exactly one persona for the requested role.

    Attributes:
        role: The role that was searched for (interpolated into the message so
            an operator debugging a non-default ``implementer_role`` sees the
            role actually queried, not a hardcoded ``Role.IMPLEMENTER`` literal
            that would misdirect them to the wrong config key — msg-3884 Obj-1).
        matches: The persona names found for ``role``. Empty on zero-match;
            length ≥ 2 on the multi-match case. In roster iteration order so a
            reader can see which entries collided.
    """

    def __init__(self, role: Role, matches: list[str]) -> None:
        self.role = role
        self.matches = matches
        if not matches:
            super().__init__(f"no {role.name} persona in [conductor].roster")
        else:
            super().__init__(f"multiple {role.name} personas in [conductor].roster: {matches!r}")


def derive_identity_by_role(roster: Mapping[str, Role], role: Role) -> str:
    """Return the single persona filling ``role`` in ``roster``.

    Raises :class:`RoleResolutionError` when the roster has zero or several
    entries for ``role``. Callers decide the fail direction: the conductor
    catches and falls back to a ``NEXT: human`` route (fail-safe, because the
    daemon has no terminal to complain at); the hand-run driver lets the
    exception propagate so an operator sees the exact miswiring on stderr.
    """
    matches = [name for name, r in roster.items() if r is role]
    if len(matches) == 1:
        return matches[0]
    raise RoleResolutionError(role, matches)


__all__ = ["RoleResolutionError", "derive_identity_by_role"]
