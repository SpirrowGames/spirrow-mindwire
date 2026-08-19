"""Tests for the Stage 3 implementer allow-list (WIRING_ALLOWLIST_SPEC §B).

Exercises the SDK-agnostic decision logic directly via :class:`ClassifiedAction`
— the safety core. The SDK-tool classifier is tested in
``test_implementer_adapter.py``; the composition-root preflight (P0/P1/P2) is
tested in ``test_preflight.py``.

Invariants pinned here (post 2026-08-19 simplification,
T-drop-branch-prediction-from-allowlist §3):

* Every Tier-A operation (``exec.code`` / ``fs.write`` / ``fs.read`` /
  ``search`` / ``git.commit`` / ``git.push`` / ``git.merge`` / ``force_push`` /
  ``history_rewrite`` / ``github.pr.open`` / ``github.read``) allows a bare
  ``ClassifiedAction(op)`` — no branch predicate, no path predicate.
* ``git.push`` still refuses ``force=True`` (defence-in-depth: a
  misclassification that emitted GIT_PUSH with the force flag should route to
  FORCE_PUSH, not through).
* ``github.pr.open`` still respects its ``target_glob`` (``develop``/``main``).
  This is out of scope for the 2026-08-19 change (msg-1265 §9) and stays.
* ``git.merge_to_main`` is the sole Tier-C forbidden — the anchor of the I-2
  invariant (D-5, "main への merge は自動化しない").
* The retired keys (``branch_glob`` / ``path_glob``) are refused fail-loud at
  config load time (``_parse_allow_rule``): a config-only reintroduction cannot
  slip in silently.
* The retired operations (``FS_DELETE`` / ``DRIVE_WRITE`` / ``EXTERNAL_PUBLISH``)
  are not present on the :class:`Operation` enum — the removal is a
  compile-time / import-time break, not a config decision.
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


# --- Tier A: every allow rule is unconstrained (no branch, no path) --------- #


@pytest.mark.parametrize(
    "op",
    [
        Operation.EXEC_CODE,
        Operation.FS_READ,
        Operation.SEARCH,
        Operation.GITHUB_READ,
        Operation.FS_WRITE,
        Operation.GIT_COMMIT,
        Operation.GIT_MERGE,
        Operation.FORCE_PUSH,
        Operation.HISTORY_REWRITE,
    ],
)
def test_unconstrained_tier_a_allowed(tmp_path: Path, op: Operation) -> None:
    """No branch_glob / path_glob after 2026-08-19: a bare op allows."""
    d = _al(tmp_path).check(ClassifiedAction(op))
    assert d.allowed is True, f"{op.value}: {d.reason}"


def test_git_push_bare_allowed(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_PUSH, branch="main"))
    assert d.allowed is True  # branch is no longer consulted at this layer


def test_git_push_force_flag_still_denied(tmp_path: Path) -> None:
    """Defence in depth (msg-1272 yaml comment on git.push):

    The classifier routes any force flag to FORCE_PUSH, but if a
    misclassification ever emitted GIT_PUSH with ``force=True``, this constraint
    denies it and the FORCE_PUSH rule below is the correct route (both Tier A
    now, so the effect is provenance — the log line reports the right verb).
    """
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_PUSH, branch="feature/x", force=True))
    assert d.allowed is False
    assert "force" in d.reason


def test_fs_write_bare_allowed(tmp_path: Path) -> None:
    """fs.write path_glob retired 2026-08-19 (msg-1272 §1): unconstrained now."""
    assert _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path=None)).allowed is True
    p = str(tmp_path.parent / "elsewhere" / "y.py")
    assert _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path=p)).allowed is True
    import tempfile

    assert (
        _al(tmp_path)
        .check(
            ClassifiedAction(Operation.FS_WRITE, path=str(Path(tempfile.gettempdir()) / "pr.md"))
        )
        .allowed
        is True
    )


# --- git.merge unconstrained (source_glob/target_glob dropped 2026-08-19) --- #


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("feature/x", "develop"),
        ("origin/main", "feature/x"),
        ("feature/x", "main"),  # local merge into main — dangerous only if it can be pushed
        ("origin/main", "main"),
        ("origin/main", "develop"),
    ],
)
def test_git_merge_unconstrained_at_this_layer(tmp_path: Path, source: str, target: str) -> None:
    """T-drop-branch-prediction-from-allowlist §3 answer (b): local merge is
    always allow at this layer. Ephemeral clone containment + GitHub server
    push-rejection carry the invariant.
    """
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_MERGE, source=source, target=target))
    assert d.allowed is True, f"{source} → {target}: {d.reason}"


# --- github.pr.open (target_glob still applies — out of scope, msg-1265 §9) - #


@pytest.mark.parametrize("target", ["develop", "main", None])
def test_github_pr_open_allowed(tmp_path: Path, target: str | None) -> None:
    assert _al(tmp_path).check(ClassifiedAction(Operation.GITHUB_PR_OPEN, target=target)).allowed


def test_github_pr_open_off_target_denied(tmp_path: Path) -> None:
    """The one glob that survives: PR base must be develop / main.

    Per-project narrowing is T-per-project-deploy-rule's job; here we ensure
    the ceiling still bites at bases the ceiling never covered.
    """
    d = _al(tmp_path).check(ClassifiedAction(Operation.GITHUB_PR_OPEN, target="release/2026-08"))
    assert d.allowed is False


def test_github_pr_open_does_not_imply_merge_to_main(tmp_path: Path) -> None:
    """D-5: merges to `main` are never automated. Opening a PR against main is
    Tier A (reversible, a human still merges); MERGING there is not, and stays
    denied regardless.
    """
    al = _al(tmp_path)
    # opening → allowed
    assert al.check(ClassifiedAction(Operation.GITHUB_PR_OPEN, target="main")).allowed is True
    # merging → denied (name-only, no branch/base predicate)
    assert al.check(ClassifiedAction(Operation.GIT_MERGE_TO_MAIN)).allowed is False


# --- Tier C: only git.merge_to_main remains -------------------------------- #


def test_git_merge_to_main_denied_with_reason(tmp_path: Path) -> None:
    """The sole surviving Tier-C forbidden. Named regardless of any branch /
    base predicate (name-only routing in the classifier).
    """
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_MERGE_TO_MAIN))
    assert d.allowed is False
    assert d.operation is Operation.GIT_MERGE_TO_MAIN
    assert d.reason


# --- default-deny ---------------------------------------------------------- #


def test_unknown_operation_default_denied(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.UNKNOWN))
    assert d.allowed is False
    assert "default: deny" in d.reason


# --- config loading -------------------------------------------------------- #


def test_default_allowlist_has_rules(tmp_path: Path) -> None:
    al = _al(tmp_path)
    assert al.check(ClassifiedAction(Operation.EXEC_CODE)).allowed
    # The one Tier-C-still-forbidden op, present in the packaged §B.3 config.
    assert al.check(ClassifiedAction(Operation.GIT_MERGE_TO_MAIN)).allowed is False


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


def test_default_allow_still_honours_forbidden(tmp_path: Path) -> None:
    al = Allowlist.from_mapping(
        {
            "default": "allow",
            "forbidden": [{"operation": "git.merge_to_main", "reason": "no"}],
        },
        repo_root=tmp_path,
    )
    assert al.check(ClassifiedAction(Operation.GIT_MERGE_TO_MAIN)).allowed is False


# --- N-5: the retired config keys stay retired ----------------------------- #


@pytest.mark.parametrize("retired_key", ["branch_glob", "path_glob"])
def test_retired_config_keys_are_refused_at_load(tmp_path: Path, retired_key: str) -> None:
    """T-drop-branch-prediction-from-allowlist §I-4 / N-5: a config-only
    reintroduction of a retired key must fail LOUDLY at load, not silently
    fail-OPEN (with no consumer, an accepted key would look enforcing while
    enforcing nothing). The parser refuses both keys.
    """
    payload = {
        "default": "deny",
        "allow": [{"operation": "git.push", retired_key: "*"}],
    }
    with pytest.raises(AllowlistConfigError) as exc:
        Allowlist.from_mapping(payload, repo_root=tmp_path)
    assert retired_key in str(exc.value)
    assert "2026-08-19" in str(exc.value)


def test_retired_operations_are_not_on_the_enum() -> None:
    """N-5's companion: the retired *operations* are gone from the enum, so a
    classifier that tried to emit one would fail at compile / import time.
    """
    names = {op.name for op in Operation}
    assert "FS_DELETE" not in names
    assert "DRIVE_WRITE" not in names
    assert "EXTERNAL_PUBLISH" not in names


def test_the_yaml_ships_no_branch_or_path_glob() -> None:
    """N-5's third leg: the packaged yaml itself must not carry a retired key.

    If a future editor re-adds one, this test fires alongside the parser test
    above so the failure is visible at both the schema-parse level and the
    config-payload level.
    """
    from importlib.resources import files

    text = (
        files("spirrow_mindwire.adapters")
        .joinpath("implementer_allowlist.yaml")
        .read_text(encoding="utf-8")
    )
    # Comments are fine; a live YAML key is `key:` at the start of a line
    # (possibly indented, possibly with a trailing value).
    import re

    for retired in ("branch_glob", "path_glob"):
        # Match an active YAML key: any leading whitespace + key + colon.
        assert re.search(rf"^\s*{retired}\s*:", text, re.MULTILINE) is None, (
            f"{retired}: a retired key must not be a live YAML rule (comments are fine)"
        )
