"""Tests for :mod:`spirrow_mindwire.identity.classification`.

Covers three surfaces:

  1. YAML load + validation (the shape rules + invariants from the YAML header).
  2. The msg-1493 §2 / §3 derivation as corrected by msg-1585 §3
     (``allowed_roles = legitimate``, ``residual = observed \\ legitimate``,
     ``unused = legitimate \\ observed``).
  3. The shipped :file:`spec/identity/legitimate_roles.yaml` — a smoke test that
     the actual file loads cleanly + contains the four identities the doc names.

The invariants tests use in-memory fixtures written with ``tmp_path`` so a rule
violation is a self-contained test, independently of the shipped file. The
integration case at the bottom pins the shipped file so a hand-edit that breaks
the derivation is caught by the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.identity import (
    ClassificationError,
    IdentityCollisionError,
    default_classification_path,
    derive_allowed_and_residual,
    load_legitimate_roles,
)


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "legitimate_roles.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoad:
    def test_valid_file_loads(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: naysayer-pr-review
    kind: participant
    legitimate: ["naysayer"]
    primary_source: "src/spirrow_mindwire/orchestrator.py::x"
    reason: "the naysayer"
  - name: pr-gate-relay
    kind: machine
    legitimate: []
    primary_source: "src/spirrow_mindwire/conductor/core.py::y"
    reason: "the relay"
""".strip(),
        )
        loaded = load_legitimate_roles(path)
        assert loaded.version == 1
        assert len(loaded.entries) == 2
        naysayer = loaded.by_key("naysayer-pr-review")
        assert naysayer is not None
        assert naysayer.kind == "participant"
        assert naysayer.legitimate == frozenset({"naysayer"})
        relay = loaded.by_key("pr-gate-relay")
        assert relay is not None
        assert relay.kind == "machine"
        assert relay.legitimate == frozenset()

    def test_by_key_matches_on_the_normalised_entry_name(self, tmp_path: Path) -> None:
        """``by_key`` normalises the ENTRY name, not the query.

        The normalisation is one-sided, and this pins which side. The YAML entry is
        written in a non-canonical spelling (``PR Gate Relay``); loading canonicalises
        it to ``pr-gate-relay``, so a lookup by the canonical key finds it. The query
        string is used verbatim — a caller that hands over a raw, un-normalised
        spelling gets ``None``, which is why every real caller
        (``scripts/identity_findings.py``) normalises before it asks.
        """
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: PR Gate Relay
    kind: machine
    legitimate: []
    primary_source: "x"
    reason: "y"
""".strip(),
        )
        loaded = load_legitimate_roles(path)

        # The raw spelling is preserved for quoting; the key is the canonical form.
        (entry,) = loaded.entries
        assert entry.name == "PR Gate Relay"
        assert entry.key == "pr-gate-relay"

        # Querying by the canonical key finds the non-canonically-spelled entry.
        assert loaded.by_key("pr-gate-relay") is entry

        # ...and the query side is NOT normalised. These all normalise to
        # "pr-gate-relay", but by_key compares verbatim, so they miss. This is the
        # contract the caller has to honour — asserting it here means a future change
        # that starts normalising the query cannot land silently.
        assert loaded.by_key("PR Gate Relay") is None
        assert loaded.by_key("pr_gate_relay") is None

    def test_missing_version_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "identities: []")
        with pytest.raises(ClassificationError, match="version must be 1"):
            load_legitimate_roles(path)

    def test_wrong_version_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "version: 2\nidentities: []")
        with pytest.raises(ClassificationError, match="version must be 1"):
            load_legitimate_roles(path)

    def test_empty_identities_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "version: 1\nidentities: []")
        with pytest.raises(ClassificationError, match="non-empty list"):
            load_legitimate_roles(path)

    def test_machine_with_nonempty_legitimate_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: bogus
    kind: machine
    legitimate: ["naysayer"]
    primary_source: "x"
    reason: "y"
""".strip(),
        )
        with pytest.raises(ClassificationError, match="kind=machine requires legitimate=\\[\\]"):
            load_legitimate_roles(path)

    def test_participant_with_empty_legitimate_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: bogus
    kind: participant
    legitimate: []
    primary_source: "x"
    reason: "y"
""".strip(),
        )
        with pytest.raises(
            ClassificationError, match="kind=participant requires len\\(legitimate\\) >= 1"
        ):
            load_legitimate_roles(path)

    def test_unknown_kind_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: bogus
    kind: helper
    legitimate: []
    primary_source: "x"
    reason: "y"
""".strip(),
        )
        with pytest.raises(ClassificationError, match="kind must be one of"):
            load_legitimate_roles(path)

    def test_missing_primary_source_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: bogus
    kind: machine
    legitimate: []
    reason: "y"
""".strip(),
        )
        with pytest.raises(ClassificationError, match="primary_source"):
            load_legitimate_roles(path)

    def test_collision_across_entries_rejected(self, tmp_path: Path) -> None:
        # Two entries whose names normalise to the same key must not both load —
        # msg-1487 §6 point 1: silent merge is exactly what ADR-11's injectivity
        # gate forbids.
        path = _write_yaml(
            tmp_path,
            """
version: 1
identities:
  - name: pr-gate-relay
    kind: machine
    legitimate: []
    primary_source: "x"
    reason: "y"
  - name: PR_Gate_Relay
    kind: machine
    legitimate: []
    primary_source: "x"
    reason: "y"
""".strip(),
        )
        with pytest.raises(IdentityCollisionError):
            load_legitimate_roles(path)


class TestDerivation:
    def test_all_observed_legitimate_returns_full_allowed_and_empty_residual(self) -> None:
        result = derive_allowed_and_residual({"naysayer"}, {"naysayer"})
        assert result.allowed_roles == frozenset({"naysayer"})
        assert result.residual == frozenset()
        assert result.unused == frozenset()

    def test_machine_with_no_observed_roles_returns_all_empty(self) -> None:
        # An identity classified as machine with `legitimate=[]` and no observation
        # yields both sides empty — the honest state.
        result = derive_allowed_and_residual([], [])
        assert result.allowed_roles == frozenset()
        assert result.residual == frozenset()
        assert result.unused == frozenset()

    def test_machine_that_has_claimed_a_role_puts_that_role_in_residual(self) -> None:
        # msg-1493 §3: a machine that has posted with a role stamped is exactly the
        # evidence of fabrication the epic exists to surface. The classified role
        # goes into residual, not allowed_roles.
        result = derive_allowed_and_residual({"naysayer", "reviewer"}, [])
        assert result.allowed_roles == frozenset()
        assert result.residual == frozenset({"naysayer", "reviewer"})
        assert result.unused == frozenset()

    def test_legitimate_role_never_observed_is_still_allowed_and_reported_unused(
        self,
    ) -> None:
        # An identity classified as legitimate for roles X and Y that has only ever
        # posted as X keeps BOTH: the classification grants the entitlement, the
        # observation does not ration it. Y is reported as `unused` — a right held
        # but not exercised, blast radius zero (msg-1585 §3).
        result = derive_allowed_and_residual({"naysayer"}, {"naysayer", "reviewer"})
        assert result.allowed_roles == frozenset({"naysayer", "reviewer"})
        assert result.residual == frozenset()
        assert result.unused == frozenset({"reviewer"})

    def test_participant_with_no_observed_roles_still_gets_full_entitlement(self) -> None:
        """An unregistered participant must bootstrap out of ``observed = ∅``.

        ``chatroom_post_message`` DROPS the role of an author with no registered
        identity and records ``null`` ⇒ for exactly the identities the write half
        exists to register, ``observed = ∅`` is a certainty, not an accident.
        Under the withdrawn intersection every such participant derived
        ``allowed_roles = ∅``, which the msg-1489 §4 bidirectional invariant then
        turns into ``independence_class = null`` — registering a participant as
        machinery. Regression pin for msg-1585 §1.
        """
        result = derive_allowed_and_residual([], {"naysayer"})
        assert result.allowed_roles == frozenset({"naysayer"})
        assert result.residual == frozenset()
        assert result.unused == frozenset({"naysayer"})

    def test_partial_overlap_yields_both_sides_nonempty(self) -> None:
        # Legitimate for {X}, observed {X, Y} ⇒ allowed = {X}, residual = {Y}. The
        # write half registers with allowed_roles = {X} and the finding names Y as
        # evidence.
        result = derive_allowed_and_residual({"naysayer", "reviewer"}, {"naysayer"})
        assert result.allowed_roles == frozenset({"naysayer"})
        assert result.residual == frozenset({"reviewer"})
        assert result.unused == frozenset()


class TestShippedFile:
    def test_default_path_loads_and_contains_the_documented_identities(self) -> None:
        # Smoke test on the actual file: a hand-edit that breaks the invariants is
        # caught here rather than at live-script run time.
        loaded = load_legitimate_roles(default_classification_path())
        # The four names from docs/identity-classification.md must appear.
        for name in ("naysayer-pr-review", "orchestrator", "pr-gate-relay", "conductor-probe"):
            entry = loaded.by_key(name)
            assert entry is not None, f"missing classification for {name!r}"
        naysayer = loaded.by_key("naysayer-pr-review")
        assert naysayer is not None
        # Only naysayer is a participant.
        assert naysayer.kind == "participant"
        assert naysayer.legitimate == frozenset({"naysayer"})
        # The other three are machines.
        for name in ("orchestrator", "pr-gate-relay", "conductor-probe"):
            entry = loaded.by_key(name)
            assert entry is not None
            assert entry.kind == "machine"
            assert entry.legitimate == frozenset()

    def test_kind_decides_the_bidirectional_side_without_any_traffic(self) -> None:
        """`kind` alone fixes which side of the msg-1489 §4 biconditional applies.

        The YAML's two invariants (machine ⇒ ``legitimate = []``; participant ⇒
        ``len(legitimate) >= 1``) plus ``allowed_roles := legitimate`` mean the
        answer to "does this identity get ``independence_class``?" is decided by
        the classification alone — with ``observed = []`` supplied here, i.e.
        with no traffic at all. Under the withdrawn intersection the answer
        depended on whether the corpus happened to cover the roles. DoD 19
        (msg-1585 §8).
        """
        loaded = load_legitimate_roles(default_classification_path())
        for entry in loaded.entries:
            derived = derive_allowed_and_residual([], entry.legitimate)
            allowed = sorted(derived.allowed_roles)
            assert (entry.kind == "participant") == (derived.allowed_roles != frozenset()), (
                f"{entry.name!r}: kind={entry.kind} but allowed_roles={allowed}"
            )
