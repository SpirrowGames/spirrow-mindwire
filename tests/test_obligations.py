"""Canaries and pointer-existence guard for the loop-readable obligations manifest.

Two canaries + one grep, no skip conditions. Together they enforce the invariants
the Tier-C GO msg-737 nailed down:

- **two-prime** wiring: the *rendered* system prompt each adapter assembles from a manifest
  contains that manifest's body verbatim. This is the "prompt builder receives the
  manifest by injection" invariant in test form — the assertion is on the prompt the
  loop actually sends, not on the manifest text alone. Made cheap by the injection
  shape of the two adapter constructors.
- **two-double-prime** verbatim length: every obligation whose ``origin.moved_from`` claims a
  verbatim move from source has ``len(body) == origin.original_length``. A
  well-meaning "cleanup" that shortens or reflows the moved body reds this canary
  rather than silently drifting the loop's actual instruction away from what was
  reviewed.

Canary ① (a hardcoded ``_EXPECTED_IDS_BY_ROLE`` shadow list of manifest ids in
this module) was **removed** on the naysayer round-3 finding: production code
(:meth:`~spirrow_mindwire.obligations.ObligationsManifest.render_role_obligations`)
is fully data-driven — it iterates ``manifest.for_role(role)`` and renders
whatever is present — so a rename in YAML does not break prompt construction,
and canary two-prime already asserts that whatever the manifest contains
reaches the assembled prompt. Maintaining a separate shadow list of ids in the
test suite carried the cost of dual management with no mechanical benefit to
the loop's correctness. The advisory ``obligations-readback (advisory)`` CI
check (``scripts/obligations_readback.py``, invoked from
``.github/workflows/obligations-readback.yml``) is the reader-facing check for
disappearing / renamed ids across a PR — it derives its expected set from the
PR **base revision** each run and never carries a shadow list of its own.

The CLAUDE.md §N pointer + `spec/process/README.md` imperative-pointer grep tests
live in this same module deliberately — the pointers are what keep the "put
loop-facing regulations where the loop reads them" instruction discoverable, so
their existence is checked alongside the canaries the pointers refer to. The
imperative verb ("置け") is asserted on the README, not on CLAUDE.md §N, because
the msg-733 §11.2 layout deliberately makes §N a pointer only (three lines) and
puts the imperative in the destination (`spec/process/README.md`).
"""

from __future__ import annotations

from pathlib import Path

from spirrow_mindwire.adapters.implementer import ImplementerSdkAdapter
from spirrow_mindwire.adapters.naysayer_sdk import (
    NaysayerSdkAdapter,
    build_naysayer_system_prompt,
)
from spirrow_mindwire.naysayer.pr_review import _PR_REVIEW_SYSTEM_PROMPT
from spirrow_mindwire.naysayer.pr_review_adr_pointers import (
    build_pr_review_pass1_system_prompt,
)
from spirrow_mindwire.obligations import (
    ObligationsManifest,
    default_manifest_path,
    load_manifest,
)
from spirrow_mindwire.value_objects import Role

# --------------------------------------------------------------------------- #
# canary two-prime — the rendered system prompt actually carries the manifest body
# --------------------------------------------------------------------------- #


def test_canary_2_prime_implementer_prompt_carries_the_injected_obligations(tmp_path: Path) -> None:
    """The implementer's assembled ``_system_prompt`` contains every implementer
    obligation body from the manifest it was constructed with.

    Constructing the adapter with a **freshly loaded** manifest (rather than a
    module-global default) is what the "prompt builder receives the manifest by
    injection" invariant guarantees can be tested cheaply. If the wiring silently
    drops an obligation (mis-ordered ``\\n`` join, a role-filter bug), the exact
    body no longer appears in the rendered prompt and this fires.
    """
    manifest = load_manifest()
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=manifest, inference_base_url="http://lx"
    )
    rendered = adapter._system_prompt
    for obligation in manifest.for_role(Role.IMPLEMENTER):
        assert obligation.body in rendered, (
            f"implementer obligation {obligation.id!r} body is not in the rendered "
            "system prompt — the injection wiring is broken"
        )
        # The id label must also travel with the body so a reviewer of the prompt
        # can name what binds them, not just read the text.
        assert f"[{obligation.id}]" in rendered


def test_canary_2_prime_naysayer_prompt_carries_the_injected_obligations() -> None:
    """The naysayer's assembled system prompt contains every naysayer obligation
    body from the manifest it was constructed with.

    Uses the module-level builder (``build_naysayer_system_prompt``) so the check
    is on the same rendering path production uses.
    """
    manifest = load_manifest()
    rendered = build_naysayer_system_prompt(obligations=manifest)
    for obligation in manifest.for_role(Role.NAYSAYER):
        assert obligation.body in rendered, (
            f"naysayer obligation {obligation.id!r} body is not in the rendered "
            "system prompt — the injection wiring is broken"
        )
        assert f"[{obligation.id}]" in rendered


def test_canary_2_prime_naysayer_adapter_prompt_carries_the_injected_obligations(
    tmp_path: Path,
) -> None:
    """Also assert on the adapter's assembled prompt — the wiring under test is
    the constructor path, which is where the production naysayer actually reads it.
    """
    manifest = load_manifest()
    adapter = NaysayerSdkAdapter(cwd=tmp_path, obligations=manifest, inference_base_url="http://lx")
    rendered = adapter._system_prompt
    for obligation in manifest.for_role(Role.NAYSAYER):
        assert obligation.body in rendered


# --------------------------------------------------------------------------- #
# canary two-double-prime — moved bodies preserve the original character length
# --------------------------------------------------------------------------- #


def test_canary_2_double_prime_moved_bodies_preserve_original_length() -> None:
    """Every obligation with ``origin.moved_from`` has body length equal to the
    recorded ``origin.original_length``.

    The loader enforces this at load time too (fail-closed at the composition root
    is one instance of it), but keeping the check here as an explicit canary keeps
    the invariant visible in the test index and gives a targeted failure message
    if a future PR relaxes the loader.
    """
    manifest = load_manifest()
    moved = [o for o in manifest.obligations if o.origin is not None]
    assert moved, (
        "spec/process/obligations.yaml contains no obligations with an origin.moved_from "
        "block, yet the whole design of this manifest is to be the destination of verbatim "
        "moves out of Python source or documentation sections — check whether an OBL entry "
        "lost its origin block"
    )
    for obligation in moved:
        assert obligation.origin is not None  # for the type checker
        assert len(obligation.body) == obligation.origin.original_length, (
            f"obligation {obligation.id!r}: body length {len(obligation.body)} != "
            f"origin.original_length {obligation.origin.original_length} — restore the "
            f"verbatim text moved from {obligation.origin.moved_from!r}, or bump the "
            "recorded length in the same commit and expect it to be discussed in PR review"
        )


# --------------------------------------------------------------------------- #
# pointer chain: CLAUDE.md §N (pointer only) → spec/process/README.md (imperative).
# msg-733 §11.2 deliberately splits the human-facing regulation out of CLAUDE.md,
# so the imperative verb no longer lives in §N — it lives at the destination.
# Both checks travel with the canaries because a vanished pointer is exactly the
# fail-open the fail-open-placement rule warns against (correctly-implemented
# but invisible).
# --------------------------------------------------------------------------- #


def test_claude_md_section_n_points_at_spec_process() -> None:
    """CLAUDE.md §N must exist and name ``spec/process/obligations.yaml`` as the SOT.

    The old §N.4 imperative moved out of CLAUDE.md into ``spec/process/README.md``
    per msg-733 §11.2 (§N is pointer-only now). The invariant kept here is that
    §N still names both the section header and the target file, so a reader who
    starts at CLAUDE.md still finds their way to the manifest.
    """
    # The repo root sits three levels above `spec/process/obligations.yaml`
    # (obligations -> process -> spec -> root).
    repo_root = default_manifest_path().parent.parent.parent
    claude_md = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## §N." in claude_md, "CLAUDE.md §N section header is missing"
    assert "spec/process/obligations.yaml" in claude_md
    assert "spec/process/README.md" in claude_md


def test_spec_process_readme_carries_the_imperative_pointer() -> None:
    """``spec/process/README.md`` must be phrased in the imperative — that is where
    the "put loop-readable obligations here" rule now lives (msg-733 §11.2).

    Named file + imperative verb (Japanese "置け" is the imperative used in the
    ported §N.4). A descriptive "is where the manifest lives" is not enough (see
    the fail-open-placement rule in the same README).
    """
    repo_root = default_manifest_path().parent.parent.parent
    readme = (repo_root / "spec" / "process" / "README.md").read_text(encoding="utf-8")
    assert "./obligations.yaml" in readme
    assert "置け" in readme, (
        "spec/process/README.md must be phrased in the imperative — a descriptive "
        '"is where the manifest lives" is not enough (see the fail-open-placement rule)'
    )


# --------------------------------------------------------------------------- #
# defensive smoke on the loader itself — the composition root treats an error
# here as SystemExit, so its shape must be a proper subclass of RuntimeError
# --------------------------------------------------------------------------- #


def test_loader_produces_an_immutable_manifest() -> None:
    """The manifest and its obligations are frozen dataclasses — a downstream
    caller cannot mutate them and reintroduce the very drift the canaries guard.
    """
    manifest = load_manifest()
    assert isinstance(manifest, ObligationsManifest)
    assert isinstance(manifest.obligations, tuple)
    for o in manifest.obligations:
        # frozen=True means attribute assignment raises FrozenInstanceError
        import dataclasses

        with __import__("pytest").raises(dataclasses.FrozenInstanceError):
            o.body = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# negative tripwire — the PR-gate pass-1 prompt carries NO obligation body/id.
#
# This is the executable declaration that the Tier B PR-gate face is NOT a
# delivery destination for `spec/process/obligations.yaml`. Only the
# design-time naysayer face (`build_naysayer_system_prompt`) renders the
# manifest into a prompt — the PR-gate's pass-1 prompt is preamble +
# _PR_REVIEW_SYSTEM_PROMPT + PASS_1_ADR_INDEX_SELF_DECLARATION only, with no
# call to `render_role_obligations` anywhere on that path (verified 2026-08-30
# in T-obligations-not-reaching-pr-gate — a full-history pickaxe on the two
# PR-gate prompt modules for "OBL-" and for verbatim obligation body fragments
# returned zero commits: this face has never carried any obligation body).
#
# SCOPE — this test is a WIRING tripwire, not an INTENT detector.
# It fails only when a body/id from the manifest actually appears in the
# rendered PR-gate pass-1 prompt. It does NOT detect that a newly added
# `role: naysayer` obligation was meant to reach the PR-gate: adding an entry
# to `spec/process/obligations.yaml` without also wiring the PR-gate builder
# to `render_role_obligations` leaves this test green. There is no machine
# detector for intent-side face-mismatch — see
# `spec/process/README.md` §「`./obligations.yaml` の配送範囲は naysayer
# の片面 (design-time) だけである」and specifically its §「意図の申告を
# 検出する機械は存在しない」. If you are adding a route for a PR-gate
# obligation, you must edit BOTH this test (positive assertion of the new
# body appearing in the PR-gate prompt) AND the PR-gate builder — a green
# CI on an obligations-only change is not proof of delivery.
# --------------------------------------------------------------------------- #


def test_pr_gate_pass1_prompt_carries_no_obligation_body() -> None:
    """The PR-gate pass-1 system prompt contains no obligation body or id from
    the loop-readable manifest — for any role.

    Asserts on ALL entries in the manifest (not just `role: naysayer`) so that
    if `_MANIFEST_ROLES` is ever extended (e.g. a `proposer` face) this test
    will still hold the PR-gate face empty until the wiring is explicitly
    added. The wiring change is what should red this test, at which point the
    author must edit here to declare which entries the PR-gate is now
    delivering (positive assertion), together with the builder change.
    """
    manifest = load_manifest()
    rendered = build_pr_review_pass1_system_prompt(verdict_task_prompt=_PR_REVIEW_SYSTEM_PROMPT)
    for obligation in manifest.obligations:
        assert obligation.body not in rendered, (
            f"obligation {obligation.id!r} body appears in the PR-gate pass-1 prompt "
            "— this face has never been a manifest delivery destination; if you are "
            "adding routing on purpose, edit this test to a positive assertion in the "
            "same commit and see spec/process/README.md §「`./obligations.yaml` の"
            "配送範囲は naysayer の片面 (design-time) だけである」"
        )
        assert f"[{obligation.id}]" not in rendered, (
            f"obligation id label [{obligation.id}] appears in the PR-gate pass-1 "
            "prompt — same rule as the body assertion above"
        )
