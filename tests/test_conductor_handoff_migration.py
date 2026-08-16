"""Migration guard: the rewrite must not lose a shape the parser it replaced could route.

The defect this file exists to prevent has now happened once, and neither of the two mechanisms
already in place could see it. `45b767d` replaced a subtractive parser (strip the decoration) with
an additive one (match the payload) — the right move, and it closed the shapes it aimed at — while
silently dropping seven the old one had handled, among them ``_NEXT: Bohr_`` and
``**NEXT:** Heisenberg``. The suite was green (1195 tests) and the real-traffic corpus was green
(898 records), because:

- **a test pins a shape somebody thought of.** The seven were not among them; nobody writes a test
  for a capability they do not know they are about to remove.
- **a corpus pins what people wrote.** Nobody has ever typed ``_NEXT: human_`` into these
  chatrooms, so no record of it exists — and a corpus can never record a *capability* being lost,
  only a *line* being mishandled. It is necessary and it is not sufficient (msg-1150 §2).

What does see it is comparing the two implementations directly, over an input set that nobody
chose. So:

- the old parser is vendored verbatim in :mod:`tests.legacy_handoff_c4c66c2`;
- the inputs are **generated** — every combination of leading noise, wrapper marker, wrapper span,
  target and trailing gloss (:func:`decorated_lines`), plus every line of the real corpus;
- the assertion is one-directional: *legacy routed it ⇒ current routes it to the same place*.
  Anything the current parser routes that the legacy one could not is an improvement, not a
  finding, so widening is never penalised here.

One-directional is what keeps this cheap to keep. It is a floor on **coverage**, not a snapshot of
behaviour, so it does not have to be revisited every time the parser is refined — only when a
capability is deliberately dropped, which is exactly when a human should be writing down why
(:data:`_ACCEPTED_LOSSES`).

Deleting this file (msg-1150 §3 permits it once the rewrite lands) also deletes the only mechanism
that would catch the *next* rewrite of this parser, which has now been rewritten three times in
three days. It is kept for that reason. It becomes genuinely removable when Layer 3 lands the
structured ``next_participant`` field and this regex scaffold stops being the thing that decides
who acts next; the legacy reference should go out with the scaffold, not before it.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from legacy_handoff_c4c66c2 import parse_next_token as legacy_parse_next_token
from legacy_handoff_c4c66c2 import resolve_handoff as legacy_resolve_handoff

from spirrow_mindwire.conductor.handoff import parse_next_token, resolve_handoff
from spirrow_mindwire.github.client import parse_pr_ref
from spirrow_mindwire.value_objects import Role

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}

# --------------------------------------------------------------------------- #
# The input set. Generated, never listed: hand-listing is what failed, twice.
# --------------------------------------------------------------------------- #

# A Markdown wrapper is a pair of identical markers that OPENS at one structural position of a
# handoff line and CLOSES at a later one. Enumerating the positions (4) and letting the product
# produce the spans (7, including "no wrapper") is what reaches ``**NEXT:** X`` — the span whose
# close lands between the keyword and the target, which is not a shape anyone in the thread wrote
# down, and which the underscore fix alone does not reach because ``*`` is not a word character.
_POSITIONS = (
    0,  # line start, before the keyword     -> **NEXT: X
    1,  # between the keyword and the colon  -> **NEXT**: X
    2,  # after the colon, before the target -> **NEXT:** X
    3,  # end of line, after any gloss       -> NEXT: X**
)
_MARKERS = ("*", "**", "_", "__", "`")
_LEADS = ("", "  ", "> ", "# ", "- ", "1. ", "| ", ">>> ", "→ ")
_TARGETS = (
    "Bohr",
    "Heisenberg",
    "Einstein",
    "human",
    "none",
    "pr-review acme/widgets#7",
    "pr-review acme/my_repo#7",
    "pr-review https://github.com/acme/widgets/pull/7",
)
_GLOSSES = ("", ",  because", ".", "。", " — go", "（実装を進める）", " |")  # noqa: RUF001


def _wrappers() -> list[tuple[str, tuple[int, int] | None]]:
    """``(marker, span)`` for every wrapper, plus the one un-wrapped case.

    The un-wrapped case is emitted once rather than once per marker: an undecorated line is the
    same line whichever marker was not used, and letting the product duplicate it would inflate
    every count in this file into a number that does not mean what it says.
    """
    return [("", None)] + [
        (marker, span) for marker in _MARKERS for span in itertools.combinations(_POSITIONS, 2)
    ]


def decorated_lines() -> Iterator[str]:
    """Every (lead x wrapper x target x gloss) handoff line."""
    for lead, (marker, span), target, gloss in itertools.product(
        _LEADS, _wrappers(), _TARGETS, _GLOSSES
    ):
        at: dict[int, str] = dict.fromkeys(_POSITIONS, "")
        if span is not None:
            at[span[0]] += marker
            at[span[1]] += marker
        yield f"{lead}{at[0]}NEXT{at[1]}:{at[2]} {target}{gloss}{at[3]}"


def _corpus_lines() -> list[str]:
    """The real-traffic corpus, read straight from the fixture.

    Deliberately re-read here rather than shared with ``test_conductor_handoff.py``: this file is
    the transitional half and has to be deletable in one piece, without leaving a helper behind or
    taking a fixture reader with it.
    """
    path = Path(__file__).parent / "data" / "next_line_corpus.tsv"
    return [
        line.split("\t", 2)[2]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


def _destination(handoff: Any) -> tuple[str, str | None]:
    """Where a parsed handoff actually sends the loop — the thing that must not change.

    ``Any`` because the two modules define structurally identical but nominally distinct
    ``Handoff`` dataclasses; comparing them by ``kind.value`` is the point.

    Not the raw token: the legacy parser hands the PR route a token with the author's decoration
    still on it (``acme/widgets#7**``) and the conductor re-runs ``parse_pr_ref`` on it before
    firing the gate (``core.py`` L289), so the destination is the *parsed ref*. Comparing raw
    tokens instead would report a cleanup as a regression.
    """
    kind = str(handoff.kind)
    if kind == "role":
        return "role", handoff.identity
    if kind == "pr_review":
        ref = parse_pr_ref(handoff.token or "")
        return "pr_review", None if ref is None else f"{ref.owner}/{ref.repo}#{ref.number}"
    return kind, None


def _losses(lines: Iterator[str] | list[str]) -> list[tuple[str, tuple[str, str | None]]]:
    """Lines the legacy parser routed that the current one does not route the same way."""
    lost = []
    for line in lines:
        legacy = _destination(legacy_resolve_handoff(line, _ROSTER))
        if legacy[0] == "absent":
            continue  # only losses count; anything newly routed is an improvement
        current = _destination(resolve_handoff(line, _ROSTER))
        if current != legacy and line not in _ACCEPTED_LOSSES:
            lost.append((line, legacy))
    return lost


# Shapes the current parser drops ON PURPOSE, each with the reason it is acceptable to stop routing
# a handoff a human wrote. Empty — the rewrite in this PR loses nothing. Adding an entry is meant to
# be a decision somebody makes and signs, not a way to make this file go green.
_ACCEPTED_LOSSES: dict[str, str] = {}


class TestTheInputSetIsNotChosenByHand:
    def test_the_generated_set_is_a_real_product(self) -> None:
        lines = list(decorated_lines())
        assert len(_wrappers()) == 1 + len(_MARKERS) * 6  # 6 = C(4 positions, 2)
        assert len(lines) == len(_LEADS) * len(_wrappers()) * len(_TARGETS) * len(_GLOSSES)
        assert len(set(lines)) == len(lines), "the axes must not collide into duplicates"

    def test_it_contains_every_shape_the_review_found_by_hand(self) -> None:
        # msg-1148 §5-4 / §5-5 listed these after reading the diff and running both parsers. The
        # point of generating the input set is that it reaches them without being told; if a
        # future edit narrows the axes, this is the assertion that notices.
        lines = set(decorated_lines())
        for shape in (
            "_NEXT: Heisenberg_",
            "_NEXT: Bohr_",
            "__NEXT: Einstein__",
            "_NEXT: human_",
            "_NEXT: pr-review acme/widgets#7_",
            "**NEXT:** Heisenberg",
            "*NEXT:* Bohr",
        ):
            assert shape in lines, shape

    def test_the_legacy_reference_routes_enough_of_both_inputs_to_be_a_test(self) -> None:
        # The differential is vacuously green if the legacy side routes nothing — a broken import,
        # a reference that no longer parses, an input set that degenerated. Both floors are set
        # well under what is measured today (6272 of 15624 generated; 461 of 898 corpus records),
        # so they catch a collapse without failing on a refinement.
        def routed(lines: Iterator[str] | list[str]) -> int:
            return sum(
                1
                for line in lines
                if _destination(legacy_resolve_handoff(line, _ROSTER))[0] != "absent"
            )

        assert routed(decorated_lines()) > 5_000
        assert routed(_corpus_lines()) > 300


class TestTheLegacyReferenceIsTheParserItClaimsToBe:
    """Pins the vendored copy to ``c4c66c2`` specifically.

    Without these, the cheapest way to make the differential pass would be to replace the vendored
    file with a copy of the current parser. Each assertion below fails if that is done.
    """

    def test_it_still_has_the_bug_the_rewrite_fixed(self) -> None:
        # `c4c66c2` could not handle emphasis closed against punctuation; that is what the rewrite
        # was for. A copy of the current parser would route these.
        for shape in ("**NEXT: Bohr**, because", "**NEXT: human**。", "**NEXT: Bohr**."):
            assert legacy_resolve_handoff(shape, _ROSTER).kind == "absent", shape

    def test_it_still_has_the_capability_the_rewrite_lost(self) -> None:
        # ...and it handled the underscore wrapper, which `45b767d` did not. A copy of the
        # *previous* generation (`1836387`) would fail here too.
        assert legacy_resolve_handoff("_NEXT: Bohr_", _ROSTER).identity == "Bohr"
        assert legacy_resolve_handoff("**NEXT:** Heisenberg", _ROSTER).identity == "Heisenberg"


class TestNothingRoutableBecameUnroutable:
    def test_no_generated_shape_was_lost(self) -> None:
        lost = _losses(decorated_lines())
        assert not lost, f"{len(lost)} shape(s) the previous parser routed, e.g. {lost[:8]}"

    def test_no_real_corpus_line_was_lost(self) -> None:
        lost = _losses(_corpus_lines())
        assert not lost, f"{len(lost)} real line(s) the previous parser routed, e.g. {lost[:8]}"

    def test_head_skip_lost_no_token(self) -> None:
        # ``parse_next_token`` is the roster-free API ``head_skip`` uses; it fails independently of
        # ``resolve_handoff`` (a lost line loses both, but the reverse is not guaranteed).
        lost = [
            line
            for line in decorated_lines()
            if legacy_parse_next_token(line) is not None
            and parse_next_token(line) is None
            and line not in _ACCEPTED_LOSSES
        ]
        assert not lost, f"{len(lost)} shape(s), e.g. {lost[:8]}"

    def test_the_accepted_losses_list_has_no_stale_entries(self) -> None:
        # An allowlist that outlives the loss it excuses silently weakens the assertion above.
        still_lost = [
            line
            for line in _ACCEPTED_LOSSES
            if _destination(legacy_resolve_handoff(line, _ROSTER))
            != _destination(resolve_handoff(line, _ROSTER))
        ]
        assert sorted(still_lost) == sorted(_ACCEPTED_LOSSES)
