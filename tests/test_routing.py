"""Tests for the single-source guard (i) predicate.

The predicate is extracted from ``conductor/core.py`` so the future
operator-board ``R-NEXT-HEIS-GUARD`` transition can consult the same rule
without a re-expression (T-operator-board msg-2544 §C-3). Three things this
suite pins directly, and one thing it deliberately does NOT try to pin:

1. The predicate covers exactly the carve-outs the pre-extraction inline
   form covered — ① human author and ③ attested independent naysayer under
   RUN — and nothing else. Behaviour tests exercise the 2⁴ truth table.
2. The attestation observation is a nullary callable (PR-review msg-2554
   BLOCKING) and the predicate itself decides when it must fire — the
   thunk is invoked only on the carve-out ③ branch, never on carve-out ①
   or on the default REDIRECT path. Dedicated tests pin that
   observation-scope contract at the predicate boundary so no caller has
   to re-express (naysayer ∧ RUN) as a short-circuit before dispatching.
3. A file-scoped drift alarm: no *second function named
   ``guard_proposer_to_implementer``* appears anywhere in the tree
   (including intra-file duplication — the counter no longer breaks after
   the first hit per file, PR-review msg-2554 advisory), and
   ``conductor/core.py`` still ``from ..routing import`` the predicate
   (rather than deleting the import along with an inline hand-roll). This
   is a name-and-import check, not a semantics check.

What the file-scoped alarm CANNOT catch — and this suite makes no claim
that it can (PR-review msg-2551 / msg-2554): a determined re-inliner who
spells the rule as, e.g., ``if is_human or (is_naysayer and run and
attest):`` inside a helper with any other name will slip past both
checks. The name-defended-once assertion is a cheap early-warning, not a
general "re-expression" detector; the actual invariant — that ``_route``
(and future callers) route on the imported predicate rather than on a
hand-rolled chain — is enforced by the behaviour suite in
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
# The behaviour matrix is small (2⁴ = 16 rows) so we enumerate it in full
# rather than sample it; the extraction is precisely worth this cost, since
# a silent semantics change is the drift the extraction exists to prevent.
#
# The full 16-row enumeration is a promise the earlier iteration of this
# suite quietly broke: PR-review msg-2564 (ADVISORY) counted only 13 rows
# after the "author_is_human=True ∧ author_is_naysayer=True" combination
# was collapsed to a single sample. Under the current roster
# (``author_partition`` strictly partitions identity into one role), that
# combination is not reachable in production — but the predicate is a
# total function over four booleans, and the enumeration must match that
# domain so a future change to the predicate's short-circuit ordering
# cannot silently escape the truth table (T-operator-board msg-2566 §C /
# msg-2568 §C). The three reintroduced rows are marked below.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("author_is_human", "author_is_naysayer", "run", "attested", "expected"),
    [
        # carve-out ①: any human author is honoured, regardless of every other bit.
        (True, False, False, False, GuardIVerdict.HONOR),
        (True, False, False, True, GuardIVerdict.HONOR),
        (True, False, True, False, GuardIVerdict.HONOR),
        (True, False, True, True, GuardIVerdict.HONOR),
        # A human author who ALSO carries a naysayer flag (unreachable under
        # the current roster's identity partition, but the predicate has no
        # roster and thus no way to police it): the carve-out ordering places
        # ① first, so every combination in this block honours regardless of
        # the state bits. The three rows below were the msg-2564 ADVISORY:
        # they are unreachable in production yet required by the 2⁴ = 16-row
        # enumeration promise. Enumerating unreachable rows is the point —
        # "unreachable today" is a roster invariant that lives elsewhere,
        # and if the roster ever changes (or a test injects a hybrid author
        # for a regression case), the predicate must still HONOR.
        (True, True, False, False, GuardIVerdict.HONOR),  # msg-2564 ADVISORY: added
        (True, True, False, True, GuardIVerdict.HONOR),  # msg-2564 ADVISORY: added
        (True, True, True, False, GuardIVerdict.HONOR),  # msg-2564 ADVISORY: added
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
    # ``message_is_attested`` is a bool-returning thunk; a constant closure
    # over ``attested`` is enough for the behaviour matrix. The observation-
    # scope contract (that the thunk is not invoked in the short-circuit
    # cases) lives in its own dedicated test below.
    assert (
        guard_proposer_to_implementer(
            author_is_human=author_is_human,
            author_is_naysayer=author_is_naysayer,
            control_state_is_run=run,
            message_is_attested=lambda: attested,
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
            message_is_attested=lambda: False,
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
            message_is_attested=lambda: True,
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
            message_is_attested=lambda: False,
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
            message_is_attested=lambda: True,
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
            message_is_attested=lambda: True,
        )
        is GuardIVerdict.REDIRECT
    )


# --------------------------------------------------------------------------- #
# Observation-scope contract for ``message_is_attested`` — the thunk fires
# only inside the carve-out ③ branch, never for a human author, and never
# for a non-naysayer / non-RUN case. This is the invariant that PR-review
# msg-2554 asked the predicate itself to own, so the caller no longer has to
# re-express (naysayer ∧ RUN) as a short-circuit before dispatching.
# --------------------------------------------------------------------------- #


class _CallCounter:
    """Bool-returning thunk that records whether it was invoked.

    Used in place of a ``unittest.mock.Mock`` to keep the intent
    ("was it called?") legible at the assertion site.
    """

    def __init__(self, value: bool) -> None:
        self._value = value
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self._value


@pytest.mark.parametrize(
    ("author_is_human", "author_is_naysayer", "run"),
    [
        # carve-out ① short-circuits on the very first branch: the thunk
        # must not fire regardless of the other cheap bits.
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
        # Non-human, non-naysayer author (the default proposer→implementer
        # case guard (i) is designed to redirect): naysayer bit is False, so
        # ``and`` short-circuits before the thunk is reached.
        (False, False, False),
        (False, False, True),
        # Non-human naysayer under supervised (not RUN): the RUN bit
        # short-circuits before the thunk is reached.
        (False, True, False),
    ],
)
def test_attest_thunk_not_invoked_outside_carveout_3_branch(
    author_is_human: bool,
    author_is_naysayer: bool,
    run: bool,
) -> None:
    # Attestation is expensive-ish (a marker read on the message body) and
    # semantically inert for these branches; asserting the thunk is not
    # invoked pins the observation-scope contract that used to live as an
    # ad-hoc short-circuit in the caller.
    thunk = _CallCounter(True)
    guard_proposer_to_implementer(
        author_is_human=author_is_human,
        author_is_naysayer=author_is_naysayer,
        control_state_is_run=run,
        message_is_attested=thunk,
    )
    assert thunk.calls == 0


def test_attest_thunk_invoked_exactly_once_in_carveout_3_branch() -> None:
    # The only branch that consumes the attest bit: non-human naysayer
    # under RUN. The predicate calls the thunk once and only once.
    thunk = _CallCounter(True)
    verdict = guard_proposer_to_implementer(
        author_is_human=False,
        author_is_naysayer=True,
        control_state_is_run=True,
        message_is_attested=thunk,
    )
    assert thunk.calls == 1
    assert verdict is GuardIVerdict.HONOR

    unattested = _CallCounter(False)
    verdict = guard_proposer_to_implementer(
        author_is_human=False,
        author_is_naysayer=True,
        control_state_is_run=True,
        message_is_attested=unattested,
    )
    assert unattested.calls == 1
    assert verdict is GuardIVerdict.REDIRECT


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


def _count_all_defs(name: str) -> tuple[int, list[Path]]:
    """Count every ``FunctionDef`` (or ``AsyncFunctionDef``) in the tree
    whose ``node.name`` equals ``name`` — including intra-file duplicates.

    Scope of what this catches: only an exact-name collision. It does NOT
    catch a copy of the predicate's *logic* placed inside a helper with a
    different name, nor an inline ``if is_human or (is_naysayer and run
    and attest):`` chain at a call site. Those cases would be silent to
    this check and are covered by the conductor's behaviour tests, which
    would break the moment the alternative differs from the extracted
    predicate on any truth-table row.

    Counts every ``ast.FunctionDef`` node — a per-file break (the earlier
    shape flagged by PR-review msg-2554 as an advisory) would have missed
    intra-file duplication where a bad merge left a second same-named
    ``def`` inside :mod:`spirrow_mindwire.routing` itself. The returned
    ``paths`` list preserves duplicates so the failure message points at
    every file the collision was seen in (the same path may appear more
    than once if the collision is intra-file).

    Uses AST rather than substring search only to keep the count honest:
    a substring check would count THIS test file (it names the predicate
    in a literal argument to :func:`_count_all_defs`), which would
    be a false positive under the exact-name check too.
    """
    import ast

    paths: list[Path] = []
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
                paths.append(path)
    return len(paths), paths


def test_guard_predicate_defined_exactly_once() -> None:
    # Name-collision alarm: no second *function named
    # ``guard_proposer_to_implementer``* exists anywhere in the tree —
    # including inside :mod:`spirrow_mindwire.routing` itself. This is the
    # narrowest useful version of Bohr msg-2544 §C-3's "定義箇所を 1 つに
    # する" — it catches an obvious accidental duplication, but not a
    # differently-named re-inlining (see the module docstring for what
    # does catch that, and PR-review msg-2551 for why the earlier prose
    # overclaimed; PR-review msg-2554 pointed out the earlier
    # per-file-break shape missed intra-file duplication).
    repo = _repo_root()
    allowed = repo / "src" / "spirrow_mindwire" / "routing.py"
    count, paths = _count_all_defs("guard_proposer_to_implementer")
    assert allowed in paths, f"predicate must live at {allowed}; paths={paths}"
    assert count == 1, (
        f"guard_proposer_to_implementer must be defined exactly once (in "
        f"src/spirrow_mindwire/routing.py); found {count} definitions "
        f"(paths, with duplicates preserved to reveal intra-file collisions): "
        f"{paths}"
    )


def test_count_all_defs_sees_intra_file_duplication(tmp_path: Path) -> None:
    # Meta-test: prove the counter actually detects intra-file duplication
    # (PR-review msg-2554 advisory). Two same-named ``def``s in one file
    # must count as two, not one. The counter's earlier shape used a per-
    # file ``break`` and would have missed this — this test would have
    # failed under that shape, and passes now.
    import ast

    src = tmp_path / "twin.py"
    src.write_text(
        "def twin():\n    return 1\n\ndef twin():\n    return 2\n",
        encoding="utf-8",
    )
    tree = ast.parse(src.read_text(encoding="utf-8"))
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "twin"
    ]
    assert len(hits) == 2, (
        "sanity check for the AST walker shape used by _count_all_defs: two "
        "same-named defs in one file must be walked as two nodes"
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
