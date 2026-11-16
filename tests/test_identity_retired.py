"""A retired identity keeps its row but claims no role.

T-retire-operator-identity-and-scope-next-fermi (msg-8871 Takahito, msg-8898 executed, msg-8899
D1-4 "`operator` は消さず `retired: 2026-11-10` を付ける"). The store record of ``operator`` was set
to ``allowed_roles=[]`` on 2026-11-10. These tests pin the classification side of that:

- the loader accepts ``retired`` and requires ``legitimate=[]`` for a retired row of any kind;
- a retired participant is the one participant allowed to hold no role;
- the registration constructor therefore writes ``allowed_roles=[]`` for it, and a store record
  that still holds a role is reported as drift (so ``register_identities.py --apply`` can never
  re-grant ``integrator`` to ``operator``);
- the shipped YAML carries ``operator`` as retired with ``legitimate=[]``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.identity import (
    ClassificationEntry,
    ClassificationError,
    build_upsert_identity_args,
    check_identity_against_classification,
    default_classification_path,
    load_legitimate_roles,
)


def _yaml(tmp_path: Path, entry: str) -> Path:
    path = tmp_path / "legitimate_roles.yaml"
    path.write_text(
        "version: 1\nidentities:\n" + entry.strip("\n") + "\n",
        encoding="utf-8",
    )
    return path


_RETIRED_PARTICIPANT = """
  - name: gone
    kind: participant
    legitimate: []
    independence_class: main-chain
    retired: {retired}
    primary_source: "x"
    reason: "y"
"""


class TestLoader:
    @pytest.mark.parametrize("retired", ['"2026-11-10"', "2026-11-10"])
    def test_retired_participant_with_no_role_loads(self, tmp_path: Path, retired: str) -> None:
        # Quoted (a str) and unquoted (YAML reads a datetime.date) both load as the ISO date.
        loaded = load_legitimate_roles(
            _yaml(tmp_path, _RETIRED_PARTICIPANT.format(retired=retired))
        )
        entry = loaded.by_key("gone")
        assert entry is not None
        assert entry.retired == "2026-11-10"
        assert entry.legitimate == frozenset()
        assert entry.kind == "participant"

    @pytest.mark.parametrize("kind", ["participant", "machine"])
    def test_retired_row_with_a_role_is_rejected(self, tmp_path: Path, kind: str) -> None:
        ic = "    independence_class: main-chain\n" if kind == "participant" else ""
        body = (
            f'  - name: gone\n    kind: {kind}\n    legitimate: ["integrator"]\n{ic}'
            f'    retired: "2026-11-10"\n    primary_source: "x"\n    reason: "y"\n'
        )
        with pytest.raises(ClassificationError, match=r"is retired .* requires legitimate=\[\]"):
            load_legitimate_roles(_yaml(tmp_path, body))

    def test_retired_settled_closer_still_needs_no_role(self, tmp_path: Path) -> None:
        # The machine exception does not survive retirement: retired wins.
        body = (
            '  - name: settled-closer\n    kind: machine\n    legitimate: ["integrator"]\n'
            '    retired: "2026-11-10"\n    primary_source: "x"\n    reason: "y"\n'
        )
        with pytest.raises(ClassificationError, match="is retired"):
            load_legitimate_roles(_yaml(tmp_path, body))

    @pytest.mark.parametrize(
        "bad",
        ['"yesterday"', '""', "3", '"2026-13-01"', "[]", '"20261110"', '"2026-W46-2"'],
    )
    def test_malformed_retired_is_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ClassificationError, match="retired must be a date"):
            load_legitimate_roles(_yaml(tmp_path, _RETIRED_PARTICIPANT.format(retired=bad)))

    def test_unretired_participant_with_no_role_is_still_rejected(self, tmp_path: Path) -> None:
        body = (
            "  - name: alive\n    kind: participant\n    legitimate: []\n"
            '    primary_source: "x"\n    reason: "y"\n'
        )
        with pytest.raises(ClassificationError, match="requires len\\(legitimate\\) >= 1"):
            load_legitimate_roles(_yaml(tmp_path, body))

    def test_absent_retired_is_none(self, tmp_path: Path) -> None:
        body = (
            '  - name: alive\n    kind: participant\n    legitimate: ["proposer"]\n'
            '    primary_source: "x"\n    reason: "y"\n'
        )
        entry = load_legitimate_roles(_yaml(tmp_path, body)).by_key("alive")
        assert entry is not None and entry.retired is None


def _retired_entry() -> ClassificationEntry:
    return ClassificationEntry(
        name="gone",
        key="gone",
        kind="participant",
        legitimate=frozenset(),
        primary_source="p::s",
        reason="r",
        independence_class="main-chain",
        retired="2026-11-10",
    )


def _codes(entry: ClassificationEntry, roles: object) -> list[str]:
    return [
        v.code
        for v in check_identity_against_classification(
            entry, independence_class="main-chain", allowed_roles=roles
        )
    ]


class TestGuardAndConstructor:
    def test_constructor_writes_no_role_for_a_retired_participant(self) -> None:
        assert build_upsert_identity_args(_retired_entry()) == {
            "identity_name": "gone",
            "independence_class": "main-chain",
            "allowed_roles": [],
        }

    def test_store_record_with_no_role_is_clean(self) -> None:
        assert _codes(_retired_entry(), []) == []

    def test_store_record_that_kept_a_role_is_drift(self) -> None:
        # The case this change exists for: a store record that still says integrator.
        assert _codes(_retired_entry(), ["integrator"]) == ["allowed_roles_differ_from_legitimate"]

    def test_unretired_participant_with_no_role_is_still_a_violation(self) -> None:
        alive = ClassificationEntry(
            name="alive",
            key="alive",
            kind="participant",
            legitimate=frozenset({"proposer"}),
            primary_source="p::s",
            reason="r",
            independence_class="main-chain",
        )
        assert "participant_has_no_roles" in _codes(alive, [])


class TestShippedFile:
    def test_operator_is_retired_with_no_role(self) -> None:
        loaded = load_legitimate_roles(default_classification_path())
        operator = loaded.by_key("operator")
        assert operator is not None
        assert operator.retired == "2026-11-10"
        assert operator.legitimate == frozenset()
        assert operator.kind == "participant"
        assert build_upsert_identity_args(operator)["allowed_roles"] == []

    def test_only_operator_is_retired(self) -> None:
        loaded = load_legitimate_roles(default_classification_path())
        assert [e.name for e in loaded.entries if e.retired is not None] == ["operator"]
