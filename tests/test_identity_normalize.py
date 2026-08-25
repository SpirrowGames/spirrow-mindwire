"""Tests for :mod:`spirrow_mindwire.identity.normalize`.

The normalisation rule is deliberately weak (casefold + collapse whitespace / underscore /
hyphen runs to a single ``-``), so most of these tests pin the equivalence classes the rule
creates rather than the character-by-character output — a future refactor that changes the
canonical form but preserves the equivalence classes is not a behavioural change.

The exception is the empty-input case and the collision-detection contract, both of which
callers depend on shape-wise (the empty string return, the ``dict[key, list[raw]]`` shape
for reporting), and both of which are asserted directly.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.identity import (
    IdentityCollisionError,
    find_collisions,
    normalize_identity_key,
)


class TestNormalize:
    def test_casefold_and_separator_collapse_land_on_one_key(self) -> None:
        # The three canonical shapes for the same identity — all must land on one key.
        assert (
            normalize_identity_key("pr-gate-relay")
            == normalize_identity_key("pr_gate_relay")
            == normalize_identity_key("PR-Gate-Relay")
            == normalize_identity_key("PR Gate Relay")
            == normalize_identity_key("  pr---gate___relay  ")
        )

    def test_empty_returns_empty(self) -> None:
        assert normalize_identity_key("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        # "  \n\t  " after strip() is "", which the empty-input short-circuit hits.
        assert normalize_identity_key("  \n\t  ") == ""

    def test_result_lower_cased(self) -> None:
        # Not just the equivalence — the canonical form must be lower to survive a
        # comparison with a hand-typed lowercase constant in YAML.
        assert normalize_identity_key("NaySayer-PR-Review") == "naysayer-pr-review"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        # Whitespace is stripped before the separator collapse; leading/trailing hyphens
        # and underscores are collapsed but not removed (the rule is deliberately weak —
        # a raw name starting with `-` is a caller bug this rule does not paper over).
        assert normalize_identity_key("  foo-bar  ") == "foo-bar"
        assert normalize_identity_key(" \t naysayer-pr-review \n ") == "naysayer-pr-review"

    def test_case_only_difference_is_not_a_distinction(self) -> None:
        assert normalize_identity_key("Bohr") == normalize_identity_key("bohr")


class TestFindCollisions:
    def test_no_collisions_returns_empty_dict(self) -> None:
        assert find_collisions(["alpha", "beta", "gamma"]) == {}

    def test_case_only_variants_report_a_collision(self) -> None:
        result = find_collisions(["Bohr", "bohr", "Einstein"])
        assert result == {"bohr": ["Bohr", "bohr"]}

    def test_separator_variants_report_a_collision(self) -> None:
        # msg-1487 §6 point 1: "衝突は登録せず報告" — this is the measurement Bohr
        # asked for. `pr-gate-relay` and `pr_gate_relay` must NOT be silently merged.
        result = find_collisions(["pr-gate-relay", "pr_gate_relay"])
        assert result == {"pr-gate-relay": ["pr-gate-relay", "pr_gate_relay"]}

    def test_duplicate_raw_strings_are_not_collisions(self) -> None:
        # Same raw string twice is the same identity — folded, not reported.
        assert find_collisions(["orchestrator", "orchestrator", "orchestrator"]) == {}

    def test_group_preserves_first_seen_order(self) -> None:
        # A finding-writer wants to say "the older spelling is X" predictably.
        result = find_collisions(["pr_gate_relay", "pr-gate-relay", "PR_Gate_Relay"])
        assert result == {"pr-gate-relay": ["pr_gate_relay", "pr-gate-relay", "PR_Gate_Relay"]}

    def test_multiple_groups_reported_together(self) -> None:
        result = find_collisions(["Bohr", "bohr", "pr-gate-relay", "PR Gate Relay"])
        assert result == {
            "bohr": ["Bohr", "bohr"],
            "pr-gate-relay": ["pr-gate-relay", "PR Gate Relay"],
        }


class TestIdentityCollisionError:
    def test_message_names_the_colliding_raws(self) -> None:
        groups = {"foo": ["Foo", "FOO"], "bar": ["Bar", "bar"]}
        with pytest.raises(IdentityCollisionError) as excinfo:
            raise IdentityCollisionError(groups)
        message = str(excinfo.value)
        # The message is deliberately unstructured (human-readable) but MUST name the
        # raws so an operator can act without going back to the caller's log.
        assert "Foo" in message and "FOO" in message and "Bar" in message and "bar" in message
        # The count is stated so a grep on `\d+ normalised key` shows the size.
        assert "2 normalised key" in message

    def test_groups_attribute_preserved(self) -> None:
        groups = {"foo": ["Foo", "FOO"]}
        err = IdentityCollisionError(groups)
        # The caller can reach through the exception for programmatic handling — the
        # exception is a hard stop but the caller may still want to write the groups
        # to a findings JSON before re-raising.
        assert err.groups == groups
