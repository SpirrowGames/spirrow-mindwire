"""The two readers that used the retired role ``human`` to find the human.

T-human-role-registry (Bohr msg-10148 ② / msg-10150, Einstein go on msg-10150). The two
readers move in opposite directions on purpose:

- ``settled_close`` "do not auto-close a thread the human owns" is a fail-closed PROTECTION,
  so its key is wide: the name ``human``, ``independence_class == "human"``, or
  ``allowed_roles`` holding ``product_owner`` or (until the store changes) ``human``. Fermi
  stays protected after ``human`` is taken off its ``allowed_roles``.
- ``park_wake`` ``human_close`` is a COUNTING class (it closes, moves and wakes nothing), so its
  key is narrow: the identity ``human`` by name, or ``product_owner`` in ``allowed_roles``.
  Fermi moves to ``unclassified``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spirrow_mindwire.park_wake.runner import RoleLookup
from spirrow_mindwire.settled_close.classify import OwnerKind
from spirrow_mindwire.settled_close.runner import OwnerLookup


class _Store:
    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "get_identity"
        who = arguments["identity_name"]
        self.calls.append(who)
        record = self.records.get(who)
        if record is None:
            return {"status": "not_found"}
        return {"status": "found", "identity": record}


_FERMI_NOW = {"independence_class": "human", "allowed_roles": ["proposer", "reviewer", "human"]}
_FERMI_AFTER = {"independence_class": "human", "allowed_roles": ["proposer", "reviewer"]}
_HUMAN_NOW = {"independence_class": "human", "allowed_roles": ["human"]}
_HUMAN_TH = {"independence_class": "human", "allowed_roles": ["product_owner"]}
_PO = {"independence_class": "main-chain", "allowed_roles": ["product_owner"]}
_AGENT = {"independence_class": "main-chain", "allowed_roles": ["proposer", "implementer"]}


class TestSettledCloseProtection:
    @pytest.mark.parametrize(
        ("owner", "record", "expected"),
        [
            ("Fermi", _FERMI_NOW, OwnerKind.HUMAN),
            # The case the stage order protects: Fermi after `human` is removed.
            ("Fermi", _FERMI_AFTER, OwnerKind.HUMAN),
            ("someone", _PO, OwnerKind.HUMAN),
            ("Bohr", _AGENT, OwnerKind.AGENT),
        ],
    )
    def test_owner_kind(self, owner: str, record: dict[str, Any], expected: OwnerKind) -> None:
        lookup = OwnerLookup(_Store({owner: record}))
        assert asyncio.run(lookup.kind(owner)) is expected

    @pytest.mark.parametrize("record", [_HUMAN_NOW, _HUMAN_TH, {"allowed_roles": []}])
    def test_identity_human_is_protected_by_name_whatever_its_record(
        self, record: dict[str, Any]
    ) -> None:
        store = _Store({"human": record})
        assert asyncio.run(OwnerLookup(store).kind("human")) is OwnerKind.HUMAN
        assert store.calls == []


class TestParkWakeHumanClose:
    @pytest.mark.parametrize("record", [_HUMAN_NOW, _HUMAN_TH, {"allowed_roles": []}])
    def test_identity_human_stays_human_close(self, record: dict[str, Any]) -> None:
        store = _Store({"human": record})
        assert asyncio.run(RoleLookup(store).is_human("human")) is True
        assert store.calls == []

    @pytest.mark.parametrize("record", [_FERMI_NOW, _FERMI_AFTER])
    def test_fermi_moves_to_unclassified(self, record: dict[str, Any]) -> None:
        # independence_class=human and the retired role `human` no longer make a human_close.
        assert asyncio.run(RoleLookup(_Store({"Fermi": record})).is_human("Fermi")) is False

    def test_product_owner_author_is_human_close(self) -> None:
        assert asyncio.run(RoleLookup(_Store({"someone": _PO})).is_human("someone")) is True

    def test_unregistered_author_is_not_human(self) -> None:
        assert asyncio.run(RoleLookup(_Store({})).is_human("nobody")) is False


def test_park_wake_and_settled_close_share_one_identity_name_set() -> None:
    # PR #615 gate advisory: the counting class and the protection must not drift apart on
    # identity names; park_wake re-exports settled_close's set rather than redefining it.
    from spirrow_mindwire.park_wake.runner import HUMAN_IDENTITY_NAMES as PARK_WAKE_NAMES
    from spirrow_mindwire.settled_close.classify import HUMAN_IDENTITY_NAMES as SETTLED_NAMES

    assert PARK_WAKE_NAMES is SETTLED_NAMES
