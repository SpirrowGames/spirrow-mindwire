"""Canaries and pointer-existence guard for the loop-readable obligations manifest.

Two canaries + one INV-C guard + one grep, no skip conditions. Together they
enforce the invariants the Tier-C GO msg-737 nailed down:

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
- **INV-C** (consumer-visible placement, msg-2387 §3): a conditional obligation's
  antecedent and the landing site of the change it prescribes both reach the
  *rendered* implementer prompt, and the meta-commentary round 1 stripped stays
  out of it. All three halves in one test because the findings on that entry
  pulled in opposite directions and a guard on any half alone lets the others
  regress. Each asserts against that entry's own injected block, not against the
  whole prompt — msg-2392 §2 measured a whole-prompt ``in`` going green for a
  reason unrelated to the entry.

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
# INV-C (consumer-visible placement) — a conditional obligation's antecedent must
# survive into the string production actually injects.
#
# msg-2207 (PR-gate on #199) found the antecedent of
# OBL-GATE-BOOTSTRAP-CLOSE-CARVEOUT living only in a YAML `#` comment. YAML
# loaders discard comments, so the injected `body` began "その場合、" — an anaphor
# whose referent had been dropped on the way to the reader. Bohr msg-2387 §1(3)
# traced the miss to having *recorded* self-containment as a property that
# already held rather than *requiring* it: "a property written as a description
# is checked by nobody". §6(1) therefore makes it a check, and specifies that
# the check read the production injection path rather than re-parsing the YAML
# in the test — re-implementing the loader would measure a string no consumer
# ever sees, which is the same class of fault as the one being fixed.
#
# Round 3 (msg-2391) then found the *landing site* of the prescribed change —
# that it is a magickit change, not a mindwire one — living only in a comment
# too, and msg-2392 §2 showed by measurement why the obvious guard against that
# is not a guard at all: the rendered implementer prompt already contains the
# string "magickit", in the ADR index line for ADR-2026-06-04-18, so
# ``"magickit" in rendered`` was true at head b40d024 *before* any fix. Hence
# the rule this block now follows:
#
#   measuring the consumer-visible representation is not enough; the assertion
#   has to be scoped to the part of it that corresponds to the declaration under
#   test. An ``in`` against a large enough whole is true for reasons that have
#   nothing to do with the declaration.
#
# So all three halves below assert against ``_injected_block``, this entry's own
# slice of the rendered prompt (~270 chars), not against the ~17.7k-char prompt.
# The two pre-existing halves were moved onto the slice for the same reason and
# not merely for tidiness: on the whole prompt the positive half could go green
# because some *other* entry happened to carry the same sentence, and the
# negative half could go red because some other entry happened to use the phrase
# 「義務付けられなければならない」. Neither is happening today (measured: 0
# occurrences elsewhere), which is exactly the condition under which a broken
# check looks healthy.
# --------------------------------------------------------------------------- #

_CARVEOUT_ID = "OBL-GATE-BOOTSTRAP-CLOSE-CARVEOUT"


def _injected_block(manifest: ObligationsManifest, rendered: str, role: Role, entry_id: str) -> str:
    """Return the slice of ``rendered`` that is ``entry_id``'s own injected block.

    Nothing here re-implements the loader or the renderer. The obligations region
    is obtained by calling the production renderer
    (:meth:`ObligationsManifest.render_role_obligations`) and asserting that its
    output is a literal substring of the adapter's assembled prompt — which is
    itself the claim that the adapter ships what the renderer produced, so the
    architectural boundary the round-3 gate endorsed is kept and made explicit.
    The block then runs from this entry's ``[<id>]`` header to the next entry's
    header, using the id order the manifest itself reports. Splitting on ``\\n\\n``
    would be wrong: block-scalar bodies (e.g. OBL-SPEC-SCOPE-CLOSURE) contain
    blank lines of their own.
    """
    region = manifest.render_role_obligations(role)
    assert region, f"the manifest renders no obligations at all for role {role!r}"
    assert region in rendered, (
        "the adapter's system prompt does not contain the renderer's output verbatim — "
        "the injection path has changed shape, and every assertion below would be "
        "measuring a string production no longer ships"
    )
    ids = [o.id for o in manifest.for_role(role)]
    assert entry_id in ids, (
        f"{entry_id} is not among role {role!r}'s obligations — the entry was removed "
        "or its role changed"
    )
    index = ids.index(entry_id)
    start = region.index(f"[{entry_id}]\n")
    # Ends at the next entry's header, or at the end of the region for the last entry.
    next_header = f"\n\n[{ids[index + 1]}]\n" if index + 1 < len(ids) else None
    end = region.index(next_header, start) if next_header is not None else len(region)
    return region[start:end]


def test_obl_gate_bootstrap_close_carveout_body_carries_its_antecedent(
    tmp_path: Path,
) -> None:
    """This entry's *own injected block* carries its antecedent and its landing
    site, and does not carry the meta-commentary round 1 removed.

    All three halves are asserted together on purpose. The findings on this entry
    pulled in opposite directions — round 1 (msg-2111 §2) said the span was too
    wide and carried Einstein's meta-commentary, round 2 (msg-2207) said it was
    too narrow and had lost the antecedent, round 3 (msg-2391) said it never named
    the repository the prescribed change lands in — so a guard on any half alone
    leaves the others free to regress on the next edit. msg-2387 §5: the span is
    decided by role, not by length.

    The subject is ``adapter._system_prompt`` built from ``load_manifest()`` with
    no path argument, i.e. the in-repo manifest production loads, put through the
    renderer production uses — narrowed to this entry's block. Nothing here
    re-implements ``yaml.safe_load``.
    """
    manifest = load_manifest()
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=manifest, inference_base_url="http://lx"
    )
    rendered = adapter._system_prompt
    block = _injected_block(manifest, rendered, Role.IMPLEMENTER, _CARVEOUT_ID)

    # Positive half (msg-2387 §6(1)): the antecedent, verbatim from Einstein
    # msg-1968, where Einstein delimited it with 「」 inside his 処方 sentence.
    antecedent = "もし事前ロールチェックが存在して sweeper が弾かれる事実が確認された場合"
    assert antecedent in block, (
        f"the antecedent of {_CARVEOUT_ID} is not in the block this entry injects "
        "into the implementer's system prompt. A YAML `#` comment is not a place an "
        "obligation's antecedent can live: the loader discards comments, so the "
        "implementer is handed a bare 「その場合、」 with no referent (msg-2207). Put "
        f"the antecedent back in `body`.\nBlock as injected:\n{block}"
    )

    # Landing-site half (msg-2391 / msg-2392 §4): the body commands the reader to
    # 「コードとして追加実装すること」 against `chatroom_close_thread`, and
    # `chatroom_close_thread` has in-repo call sites here (src/spirrow_mindwire/
    # gate_bootstrap.py), so a reader holding only a mindwire checkout lands on the
    # wrong repository unless the body says which one. Asserted on the block, never
    # on the whole prompt: the prompt carries "magickit" in its ADR index line for
    # ADR-2026-06-04-18, so the whole-prompt form of this check passes at head
    # b40d024, before the fix (msg-2392 §2 — reproduced independently before
    # writing this).
    assert "magickit" in block, (
        f"{_CARVEOUT_ID} does not name the repository its prescribed change lands in. "
        "The carve-out is a change to magickit's server-side chatroom_close_thread; "
        "mindwire only calls that tool. Stated only in a YAML comment, the fact is "
        "discarded by the loader and the implementer searches this repository — where "
        "chatroom_close_thread really does appear — and fails to execute a cross-repo "
        f"requirement (msg-2391).\nBlock as injected:\n{block}"
    )

    # Negative half (msg-2387 §5): the framing clause of Einstein's 処方 sentence is
    # commentary *about* the obligation, not part of it, and round 1 was right to
    # strip it. Anchored on that specific clause rather than on a general
    # "no meta-commentary" heuristic, which would be unfalsifiable here.
    for meta_fragment in (
        "実装者は「コードを確認する」だけでなく",
        "義務付けられなければならない",
    ):
        assert meta_fragment not in block, (
            f"Einstein's meta-commentary {meta_fragment!r} is back in the block "
            f"{_CARVEOUT_ID} injects — round 1 (msg-2111 §2) removed it. Widening the "
            "span to restore the antecedent, or annotating it with the landing site, "
            "must not drag the framing clause back in; the antecedent is separately "
            f"quotable because msg-1968 delimits it with 「」.\nBlock as injected:\n{block}"
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
