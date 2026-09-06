"""Tests for the single-source guard (i) predicate.

The predicate is extracted from ``conductor/core.py`` so the future
operator-board ``R-NEXT-HEIS-GUARD`` transition can consult the same rule
without a re-expression (T-operator-board msg-2544 §C-3). Two things this
suite pins directly, and one thing it deliberately does NOT try to pin:

1. The predicate covers exactly the carve-outs the pre-extraction inline
   form covered — ① human author and ③ attested independent naysayer under
   RUN — and nothing else. Behaviour tests exercise the truth table.
2. A file-scoped drift alarm: no *second function named
   ``guard_proposer_to_implementer``* appears anywhere else in the tree,
   and ``conductor/core.py`` still ``from ..routing import`` the predicate
   (rather than deleting the import along with an inline hand-roll). This
   is a name-and-import check, not a semantics check.

What the file-scoped alarm CANNOT catch — and this suite makes no claim
that it can (PR-review msg-2551): a determined re-inliner who spells the
rule as, e.g., ``if is_human or (is_naysayer and run and attest):``
inside a helper with any other name will slip past both checks. The
name-defended-once assertion is a cheap early-warning, not a general
"re-expression" detector; the actual invariant — that ``_route`` (and
future callers) route on the imported predicate rather than on a hand-
rolled chain — is enforced by the behaviour suite in
:mod:`tests.test_conductor_core` (``test_guard_i_*``, ``test_carveout_*``,
``test_proposer_to_implementer_stops_at_human_*``, and the observation-
scope short-circuit tests added by PR-review msg-2551). Semantic drift
that agrees with the extracted predicate on the truth table isn't drift;
semantic drift that disagrees breaks those behaviour tests. That is the
protection this repository actually has, and the file-scoped tests below
sit on top of it, not in place of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.routing import GuardIVerdict, guard_proposer_to_implementer

# --------------------------------------------------------------------------- #
# Truth table — every combination of the four observation booleans.
# --------------------------------------------------------------------------- #
#
# The behaviour matrix is small (2^4 = 16 rows) so we enumerate it in full
# rather than sample it; the extraction is precisely worth this cost, since
# a silent semantics change is the drift the extraction exists to prevent.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("author_is_human", "author_is_naysayer", "run", "attested", "expected"),
    [
        # carve-out ①: any human author is honoured, regardless of every other bit.
        (True, False, False, False, GuardIVerdict.HONOR),
        (True, False, False, True, GuardIVerdict.HONOR),
        (True, False, True, False, GuardIVerdict.HONOR),
        (True, False, True, True, GuardIVerdict.HONOR),
        # A human author who ALSO carries a naysayer flag (impossible under the
        # current roster but not the predicate's job to police) still honours —
        # the carve-out ordering places ① first.
        (True, True, True, True, GuardIVerdict.HONOR),
        # carve-out ③: attested naysayer under RUN is honoured.
        (False, True, True, True, GuardIVerdict.HONOR),
        # ③ requires ALL of naysayer + RUN + attest; drop any one and it redirects.
        (False, True, True, False, GuardIVerdict.REDIRECT),  # missing attest
        (False, True, False, True, GuardIVerdict.REDIRECT),  # not RUN
        (False, False, True, True, GuardIVerdict.REDIRECT),  # not naysayer
        (False, True, False, False, GuardIVerdict.REDIRECT),
        (False, False, True, False, GuardIVerdict.REDIRECT),
        (False, False, False, True, GuardIVerdict.REDIRECT),
        (False, False, False, False, GuardIVerdict.REDIRECT),
    ],
)
def test_guard_i_truth_table(
    author_is_human: bool,
    author_is_naysayer: bool,
    run: bool,
    attested: bool,
    expected: GuardIVerdict,
) -> None:
    assert (
        guard_proposer_to_implementer(
            author_is_human=author_is_human,
            author_is_naysayer=author_is_naysayer,
            control_state_is_run=run,
            message_is_attested=attested,
        )
        is expected
    )


# --------------------------------------------------------------------------- #
# The named-carve-out cases, spelled out so a reader can grep by the ADR
# marker rather than by row index in the parametrised table.
# --------------------------------------------------------------------------- #


def test_carveout_1_human_authored_decide_honours() -> None:
    # Tier-C msg-553 / msg-557: a human-authored decide is the Tier-C gate
    # itself; no other check may withdraw it.
    assert (
        guard_proposer_to_implementer(
            author_is_human=True,
            author_is_naysayer=False,
            control_state_is_run=False,
            message_is_attested=False,
        )
        is GuardIVerdict.HONOR
    )


def test_carveout_3_attested_naysayer_under_run_honours() -> None:
    # P-3b, Tier-C msg-954 §2 / msg-970: the naysayer's own proceed is the
    # only autonomous door to code; requires RUN AND attest.
    assert (
        guard_proposer_to_implementer(
            author_is_human=False,
            author_is_naysayer=True,
            control_state_is_run=True,
            message_is_attested=True,
        )
        is GuardIVerdict.HONOR
    )


def test_carveout_3_unattested_naysayer_falls_through_to_redirect() -> None:
    # P-3b explicitly requires the harness's preflight stamp: un-attested,
    # the branch is not taken and the turn falls through to the human
    # terminal — the pre-existing safe path, no new failure mode.
    assert (
        guard_proposer_to_implementer(
            author_is_human=False,
            author_is_naysayer=True,
            control_state_is_run=True,
            message_is_attested=False,
        )
        is GuardIVerdict.REDIRECT
    )


def test_carveout_3_naysayer_under_supervised_redirects() -> None:
    # carve-out ③ is gated on the project's loop control state being ``run``;
    # ``supervised`` (the pre-inversion baseline) closes the door.
    assert (
        guard_proposer_to_implementer(
            author_is_human=False,
            author_is_naysayer=True,
            control_state_is_run=False,
            message_is_attested=True,
        )
        is GuardIVerdict.REDIRECT
    )


def test_proposer_to_implementer_redirects_by_default() -> None:
    # The load-bearing case: any handoff from a non-human, non-attested-
    # naysayer author to the implementer redirects. This is guard (i)'s
    # entire reason to exist.
    assert (
        guard_proposer_to_implementer(
            author_is_human=False,
            author_is_naysayer=False,
            control_state_is_run=True,
            message_is_attested=True,
        )
        is GuardIVerdict.REDIRECT
    )


# --------------------------------------------------------------------------- #
# Single-source pin (name-scoped) — an AST count that fails loud if a second
# *function named ``guard_proposer_to_implementer``* appears anywhere else
# in the tree. This is intentionally weaker than the T-operator-board
# msg-2544 §C-3 goal ("定義箇所を 1 つにする") — it catches a copy-with-
# same-name (the most likely accidental duplication) but NOT a re-inlined
# chain under a different name. That gap is covered by the conductor's
# behaviour suite in :mod:`tests.test_conductor_core` (semantic drift shows
# up as a failing carve-out test); this file-scoped alarm is a cheap early
# warning, not a general "re-expression" detector (PR-review msg-2551).
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    # tests/ is a direct child of the repo root.
    return Path(__file__).resolve().parent.parent


def _count_top_level_defs(name: str) -> tuple[int, list[Path]]:
    """Count files carrying a ``FunctionDef`` (or ``AsyncFunctionDef``) whose
    ``node.name`` equals ``name`` — anywhere in the module's AST.

    Scope of what this catches: only an exact-name collision. It does NOT
    catch a copy of the predicate's *logic* placed inside a helper with a
    different name, nor an inline ``if is_human or (is_naysayer and run
    and attest):`` chain at a call site. Those cases would be silent to
    this check and are covered by the conductor's behaviour tests, which
    would break the moment the alternative differs from the extracted
    predicate on any truth-table row.

    Uses AST rather than substring search only to keep the count honest:
    a substring check would count THIS test file (it names the predicate
    in a literal argument to :func:`_count_top_level_defs`), which would
    be a false positive under the exact-name check too.
    """
    import ast

    hits: list[Path] = []
    skip_dirs = {".venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
    for path in _repo_root().rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        # A ``def`` at module scope or nested inside a class / function counts
        # as a definition of the name (the drift we guard against would show
        # up the same way whether the second copy is top-level or method).
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                hits.append(path)
                break  # one hit per file is enough for the count
    return len(hits), hits


def test_guard_predicate_defined_exactly_once() -> None:
    # Name-collision alarm: no second *function named
    # ``guard_proposer_to_implementer``* exists outside
    # :mod:`spirrow_mindwire.routing`. This is the narrowest useful version
    # of Bohr msg-2544 §C-3's "定義箇所を 1 つにする" — it catches an
    # obvious accidental duplication, but not a differently-named re-
    # inlining (see the module docstring for what does catch that, and
    # PR-review msg-2551 for why the earlier prose overclaimed).
    repo = _repo_root()
    allowed = repo / "src" / "spirrow_mindwire" / "routing.py"
    count, hits = _count_top_level_defs("guard_proposer_to_implementer")
    assert allowed in hits, f"predicate must live at {allowed}; hits={hits}"
    assert count == 1, (
        f"guard_proposer_to_implementer must be defined exactly once (in "
        f"src/spirrow_mindwire/routing.py); found {count} definitions: {hits}"
    )


def test_guard_predicate_call_sites_go_through_routing_module() -> None:
    # Import-presence alarm: ``conductor/core.py`` still ``from ..routing
    # import`` the predicate and still spells ``guard_proposer_to_
    # implementer(`` at least once. This is deliberately a string check on
    # a specific file, not a general "no hand-rolled chain" scan: a
    # determined re-inliner who deleted the import along with the call
    # would slip past this AND leave a broken carve-out truth table, and
    # the behaviour suite in :mod:`tests.test_conductor_core` (in
    # particular ``test_guard_i_*`` / ``test_carveout_*`` / the
    # observation-scope short-circuit tests) is what actually catches
    # that. Documenting the split honestly (PR-review msg-2551): the
    # file-scoped check below is the cheap early warning; the behaviour
    # suite is the load-bearing guarantee.
    core = _repo_root() / "src" / "spirrow_mindwire" / "conductor" / "core.py"
    body = core.read_text(encoding="utf-8")
    assert "from ..routing import" in body, (
        "conductor/core.py must import guard_proposer_to_implementer from "
        "..routing; deleting the import would drop the extraction back "
        "into an inline chain (msg-2544 §C-3)."
    )
    assert "guard_proposer_to_implementer(" in body, (
        "conductor/core.py must call guard_proposer_to_implementer(...) "
        "at least once; the behaviour suite in tests/test_conductor_core.py "
        "enforces that the call still routes correctly."
    )
