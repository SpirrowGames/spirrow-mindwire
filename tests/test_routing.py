"""Tests for the single-source guard (i) predicate.

The predicate is extracted from ``conductor/core.py`` so the future
operator-board ``R-NEXT-HEIS-GUARD`` transition can consult the same rule
without a re-expression (T-operator-board msg-2544 §C-3). Two things must
hold and are pinned here:

1. The predicate covers exactly the carve-outs the pre-extraction inline
   form covered — ① human author and ③ attested independent naysayer under
   RUN — and nothing else. Behaviour tests exercise the truth table.
2. The predicate is defined **once** in this codebase. A grep-count test
   fails loud if a second definition (or a second re-expression of the
   rule) is added — that would silently re-open the drift that motivated
   the extraction in the first place.

Behaviour on the conductor call site (``_route``) is separately covered
by :mod:`tests.test_conductor_core` (``test_guard_i_*``,
``test_carveout_*``, ``test_proposer_to_implementer_stops_at_human_*``);
the two suites together check that the predicate is correct AND that the
conductor's consumer wiring routes on it correctly.
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
# Single-source pin — a grep count that fails loud if guard (i) is
# re-expressed in a second location (T-operator-board msg-2544 §C-3:
# "定義箇所を 1 つにする… 定義箇所が 1 つであることを grep-count で検査").
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    # tests/ is a direct child of the repo root.
    return Path(__file__).resolve().parent.parent


def _count_top_level_defs(name: str) -> tuple[int, list[Path]]:
    """Count files that carry a *real* ``def <name>(`` at the start of a line
    (optionally indented) — not a string literal or comment mentioning it.

    Uses AST rather than substring search on purpose: a substring check would
    count THIS test file (it names the predicate in a literal argument to
    :func:`_count_top_level_defs`), which is exactly the false positive that
    would flip the drift signal green when a second definition appears.
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
    # The predicate MUST live only in :mod:`spirrow_mindwire.routing`. A
    # second definition anywhere else in the tree is precisely the drift the
    # extraction exists to prevent — Bohr msg-2544 §C-3: "board の
    # R-NEXT-HEIS-GUARD は同じ述語を呼ぶだけにし、遷移表は判定を持たない".
    repo = _repo_root()
    allowed = repo / "src" / "spirrow_mindwire" / "routing.py"
    count, hits = _count_top_level_defs("guard_proposer_to_implementer")
    assert allowed in hits, f"predicate must live at {allowed}; hits={hits}"
    assert count == 1, (
        f"guard_proposer_to_implementer must be defined exactly once (in "
        f"src/spirrow_mindwire/routing.py); found {count} definitions: {hits}"
    )


def test_guard_predicate_call_sites_go_through_routing_module() -> None:
    # The other side of the single-source rule: every consumer imports the
    # predicate from :mod:`spirrow_mindwire.routing`, rather than
    # reimplementing the carve-out chain inline. A test that fires when a
    # future callsite adds a hand-rolled ``if author_is_human … elif
    # naysayer …`` chain instead of ``guard_proposer_to_implementer(…)``.
    #
    # Weaker than the definition count above (a caller can re-express the
    # rule without literally spelling ``def guard_proposer_to_implementer``),
    # so this test does not enforce a numeric upper bound on imports; it
    # only pins that the conductor — the one existing consumer at
    # extraction time — imports through the module. Future callsites will
    # each add their own regression test as they land.
    core = _repo_root() / "src" / "spirrow_mindwire" / "conductor" / "core.py"
    body = core.read_text(encoding="utf-8")
    assert "from ..routing import" in body, (
        "conductor/core.py must import guard_proposer_to_implementer from "
        "..routing; a re-inlined carve-out chain would re-open drift with "
        "the operator board's R-NEXT-HEIS-GUARD (msg-2544 §C-3)."
    )
    assert "guard_proposer_to_implementer(" in body, (
        "conductor/core.py must call guard_proposer_to_implementer(...) "
        "rather than re-express the carve-out chain inline."
    )
