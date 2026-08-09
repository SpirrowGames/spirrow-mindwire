"""Canaries and pointer-existence guard for the loop-readable obligations manifest.

Three canaries + one grep, no skip conditions. Together they enforce the invariants
the Tier-C GO msg-737 nailed down:

- **①** id-coverage: every obligation id the running code renders is present in the
  in-repo manifest, and every manifest id belongs to a role a running adapter
  actually renders. A drift between the code and the manifest (a rename, a stray
  entry, a role that no longer receives its clause) is caught here.
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
from spirrow_mindwire.obligations import (
    ObligationsManifest,
    default_manifest_path,
    load_manifest,
)
from spirrow_mindwire.value_objects import Role

# The manifest ids the running code renders today, keyed by the role that renders
# them. Any addition or removal of an obligation must be reflected here — that
# manual step is deliberate: the canary is what forces someone to notice a rename.
_EXPECTED_IDS_BY_ROLE: dict[Role, frozenset[str]] = {
    Role.IMPLEMENTER: frozenset(
        {"OBL-DECLARE-UNREADABLE", "OBL-READBACK-ENTRY", "OBL-READBACK-EXIT"}
    ),
    Role.NAYSAYER: frozenset({"OBL-VERDICT-CONSTRAINT"}),
}


# --------------------------------------------------------------------------- #
# canary ① — id coverage between the manifest and the code that renders it
# --------------------------------------------------------------------------- #


def test_canary_1_manifest_ids_match_the_ids_the_code_renders() -> None:
    """Every id the code renders is in the manifest, and every manifest id is rendered.

    A one-way check (e.g. only "manifest ⊂ rendered") would let a stale entry sit
    unreferenced in ``spec/process/obligations.yaml`` forever; a one-way check
    the other way would let a code path fabricate an id the manifest never
    defined. Both directions must hold, and both role buckets must too — an
    implementer obligation misfiled under ``role: naysayer`` never reaches the
    implementer.
    """
    manifest = load_manifest()
    for role, expected in _EXPECTED_IDS_BY_ROLE.items():
        actual = frozenset(o.id for o in manifest.for_role(role))
        assert actual == expected, (
            f"role={role.value}: manifest ids {sorted(actual)} do not match the ids "
            f"the code renders for that role ({sorted(expected)}). If you added or "
            "renamed an obligation, update spec/process/obligations.yaml AND "
            "tests/test_obligations.py::_EXPECTED_IDS_BY_ROLE."
        )
    all_manifest_ids = frozenset(o.id for o in manifest.obligations)
    all_expected = frozenset().union(*_EXPECTED_IDS_BY_ROLE.values())
    orphan = sorted(all_manifest_ids - all_expected)
    missing = sorted(all_expected - all_manifest_ids)
    assert all_manifest_ids == all_expected, (
        f"manifest contains ids the code does not render ({orphan!r}) "
        f"or the code expects ids the manifest does not define ({missing!r})"
    )


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
