"""A retired ROLE (``human``) may be held by no identity.

T-human-role-registry (Bohr msg-10141 Obj-1-2 / msg-10143 §2-2 / §2-3, Einstein msg-10140
Obj-1-2). The thread retires the role ``human`` from the role registry: past posts keep
the value, but no identity may hold it. These tests pin the repo side of that:

- the loader refuses a ``legitimate`` set that names ``human`` on ANY row, whatever its kind or
  name (not only a row named ``human``), so ``register_identities.py`` can never grant it;
- the registration guard reports ``role_retired`` for a payload or a store record whose
  ``allowed_roles`` names ``human``, so a record written by hand is a finding;
- the shipped YAML grants ``human`` to nobody.

The identity ``human`` and the independence_class ``human`` are not retired, and nothing here
touches them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.identity import (
    RETIRED_ROLES,
    ClassificationEntry,
    ClassificationError,
    check_identity_against_classification,
    check_store_record,
    default_classification_path,
    load_legitimate_roles,
)


def _yaml(tmp_path: Path, entry: str) -> Path:
    path = tmp_path / "legitimate_roles.yaml"
    path.write_text("version: 1\nidentities:\n" + entry.strip("\n") + "\n", encoding="utf-8")
    return path


def _entry(name: str = "Fermi", legitimate: frozenset[str] | None = None) -> ClassificationEntry:
    return ClassificationEntry(
        name=name,
        key=name.lower(),
        kind="participant",
        legitimate=legitimate if legitimate is not None else frozenset({"proposer", "reviewer"}),
        primary_source="x",
        reason="y",
        independence_class="human",
    )


def test_human_is_the_retired_role() -> None:
    assert frozenset({"human"}) == RETIRED_ROLES


class TestLoader:
    @pytest.mark.parametrize("name", ["human", "Fermi", "someone-else"])
    def test_participant_row_holding_human_is_refused(self, tmp_path: Path, name: str) -> None:
        body = (
            f'  - name: {name}\n    kind: participant\n    legitimate: ["proposer", "human"]\n'
            f'    independence_class: human\n    primary_source: "x"\n    reason: "y"\n'
        )
        with pytest.raises(ClassificationError, match=r"retired role\(s\) \['human'\]"):
            load_legitimate_roles(_yaml(tmp_path, body))

    def test_machine_row_holding_human_is_refused(self, tmp_path: Path) -> None:
        body = (
            '  - name: some-relay\n    kind: machine\n    legitimate: ["human"]\n'
            '    primary_source: "x"\n    reason: "y"\n'
        )
        with pytest.raises(ClassificationError, match="retired role"):
            load_legitimate_roles(_yaml(tmp_path, body))

    def test_shipped_yaml_grants_human_to_nobody(self) -> None:
        loaded = load_legitimate_roles(default_classification_path())
        assert all(not (e.legitimate & RETIRED_ROLES) for e in loaded.entries)


class TestGuard:
    def test_payload_with_human_is_reported(self) -> None:
        violations = check_identity_against_classification(
            _entry(), independence_class="human", allowed_roles=["proposer", "reviewer", "human"]
        )
        assert "role_retired" in [v.code for v in violations]

    def test_payload_without_human_is_clean(self) -> None:
        violations = check_identity_against_classification(
            _entry(), independence_class="human", allowed_roles=["proposer", "reviewer"]
        )
        assert violations == []

    def test_store_record_that_kept_human_is_drift(self) -> None:
        # The Fermi record as read 2026-11-28 (msg-10142): allowed_roles still holds `human`.
        row = check_store_record(
            _entry(),
            {
                "status": "found",
                "identity": {
                    "identity_name": "Fermi",
                    "independence_class": "human",
                    "allowed_roles": ["proposer", "reviewer", "human"],
                },
            },
        )
        assert "role_retired" in [v["code"] for v in row["violations"]]

    def test_identity_named_human_is_not_itself_refused(self) -> None:
        # Only the role is retired. The identity `human` with no role is consistent once
        # its row says so; it is the role value, not the name, the guard refuses.
        violations = check_identity_against_classification(
            _entry(name="human", legitimate=frozenset({"product_owner"})),
            independence_class="human",
            allowed_roles=["product_owner"],
        )
        assert violations == []
