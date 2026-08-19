"""Tests for the Stage 3 implementer allow-list (WIRING_ALLOWLIST_SPEC §B).

These exercise the SDK-agnostic decision logic directly via
:class:`ClassifiedAction` — the safety core. The SDK-tool classifier is tested
in ``test_implementer_adapter.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.allowlist import (
    Allowlist,
    AllowlistConfigError,
    ClassifiedAction,
    Operation,
    default_allowlist,
)


def _al(repo_root: Path) -> Allowlist:
    return default_allowlist(repo_root=repo_root)


# --- Tier A: unconstrained allows ------------------------------------------ #


# --- fs.write path constraint (<repo>/** only) ----------------------------- #


# --- the scratch-file lesson (2026-08-08) ---------------------------------- #
# The implementer finished both Step 0 commits and then died opening the PR: it wrote the
# PR body to the OS temp directory, which this rule denies. Nothing about the rule was
# wrong — the sanctioned alternative simply was not stated where the implementer reads
# (its system prompt). These two pin the pair of facts that make that guidance correct,
# so nobody "fixes" the recurrence by widening the constraint instead.


# --- git.commit / git.push branch constraints ------------------------------ #


# --- git.merge source/target ----------------------------------------------- #


# A merge with NO target reaches neither rule's containment (`_constraints_pass` skips a
# constraint whose action field is None). That is not this layer's job: the guard enriches the
# target from `.git/HEAD` and downgrades to UNKNOWN when it cannot, which is fail-closed and
# covered by `test_guard_merge_undeterminable_branch_fails_closed` in test_implementer_adapter.py.
# Asserting the permissive result here would read as an endorsement of it, so the assertion that
# matters lives at the layer that actually decides.


# --- github.pr.open -------------------------------------------------------- #


# --- Tier C: explicit forbidden -------------------------------------------- #


# --- force_push / history_rewrite: branch-scoped Tier A (2026-08-15) ------- #


# A None-branch action reaches the allow-list without a constraint to check,
# because `_branch_matches(None, ...)` falls through — this is fail-OPEN, and
# it is deliberate: the *guard* (`_AllowlistGuard._enrich` in adapters/implementer)
# resolves the current branch from `git rev-parse` and downgrades to UNKNOWN
# when HEAD is undecidable, which is fail-CLOSED. The assertion that matters
# lives at the layer that decides — see
# `test_guard_force_push_undeterminable_branch_fails_closed` and
# `test_guard_history_rewrite_undeterminable_branch_fails_closed` in
# test_implementer_adapter.py. Asserting the permissive result here would read as
# an endorsement of it (identical framing to the git.merge no-target case above).


# --- default-deny ---------------------------------------------------------- #


def test_unknown_operation_default_denied(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.UNKNOWN))
    assert d.allowed is False
    assert "default: deny" in d.reason


# --- config loading -------------------------------------------------------- #


def test_from_mapping_rejects_non_dict(tmp_path: Path) -> None:
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_mapping(["not", "a", "dict"], repo_root=tmp_path)


def test_from_mapping_rejects_unknown_default(tmp_path: Path) -> None:
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_mapping({"default": "maybe"}, repo_root=tmp_path)


def test_from_mapping_rejects_unknown_operation(tmp_path: Path) -> None:
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_mapping(
            {"default": "deny", "allow": [{"operation": "not.an.op"}]}, repo_root=tmp_path
        )


def test_default_allow_policy_permits_unlisted(tmp_path: Path) -> None:
    al = Allowlist.from_mapping({"default": "allow"}, repo_root=tmp_path)
    assert al.check(ClassifiedAction(Operation.UNKNOWN)).allowed is True
