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
  target and trailing gloss (:func:`decorated_lines`), plus every line of the real corpus. The
  wrapper *positions* are derived from each line's own token structure rather than listed, because
  a listed position set is still an enumeration and it failed the same way the hand-written tests
  did: see :func:`_fragments`;
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
# handoff line and CLOSES at a later one. The positions are NOT listed here, and that is the whole
# design of this block.
#
# They used to be: four constants, described as "line start / before the colon / after the colon /
# end of line". All four are OUTSIDE the target, and ``_TARGETS`` held ``pr-review acme/widgets#7``
# as ONE indivisible string — so no term of the product could put a closing marker BETWEEN the
# sentinel and its ref, and ``NEXT: _pr-review_ acme/widgets#7`` was unreachable *by construction*.
# The shipped parser dropped exactly that shape while 15,624 generated lines stayed green
# (msg-1163 §4 / msg-1164 §2). A listed position set is an enumeration wearing a product's
# clothes, and it failed the way every enumeration in this thread has failed: not by being wrong,
# by being incomplete in a direction nobody thought to look.
#
# So a target is a TUPLE OF PARTS, a line is the sequence of literal fragments those parts imply,
# and the decoration slots are the gaps between consecutive fragments plus the two ends. A
# two-part target therefore gets more slots than a one-part target automatically, and a marker can
# wrap the sentinel alone, the ref alone, both, or the whole line — without anyone counting.
_MARKERS = ("*", "**", "_", "__", "`")
_LEADS = ("", "  ", "> ", "# ", "- ", "1. ", "| ", ">>> ", "→ ")
_TARGETS = (
    ("Bohr",),
    ("Heisenberg",),
    ("Einstein",),
    ("human",),
    ("none",),
    ("pr-review", "acme/widgets#7"),
    ("pr-review", "acme/my_repo#7"),
    ("pr-review", "https://github.com/acme/widgets/pull/7"),
)
_GLOSSES = ("", ",  because", ".", "。", " — go", "（実装を進める）", " |")  # noqa: RUF001


def _fragments(target: tuple[str, ...], gloss: str) -> tuple[str, ...]:
    """The literal pieces of one handoff line, in order — the thing decoration goes *between*.

    This is the position set, stated once as structure. Every gap between two consecutive
    fragments is a place a marker can sit, so the slots follow from the target's own shape instead
    of from a constant somebody has to remember to extend.

    Empty pieces are dropped, which is what keeps the slots distinct: with no gloss, "after the
    target" and "end of line" are the same offset, and two different spans would render the same
    line — silently shrinking a product that still reports its full size.
    """
    pieces: list[str] = ["NEXT", ":", " "]
    for index, part in enumerate(target):
        if index:
            pieces.append(" ")  # the separator inside a multi-part target is itself a fragment
        pieces.append(part)
    pieces.append(gloss)
    return tuple(piece for piece in pieces if piece)


def _slots(fragments: tuple[str, ...]) -> range:
    """Where decoration can go: every gap between fragments, plus the two outer ends."""
    return range(len(fragments) + 1)


def _wrappers(fragments: tuple[str, ...]) -> list[tuple[str, tuple[int, int] | None]]:
    """``(marker, span)`` for every wrapper over ``fragments``, plus the one un-wrapped case.

    The un-wrapped case is emitted once rather than once per marker: an undecorated line is the
    same line whichever marker was not used, and letting the product duplicate it would inflate
    every count in this file into a number that does not mean what it says.
    """
    return [("", None)] + [
        (marker, span)
        for marker in _MARKERS
        for span in itertools.combinations(_slots(fragments), 2)
    ]


def decorated_lines() -> Iterator[str]:
    """Every (lead x target x gloss x wrapper) handoff line."""
    for lead, target, gloss in itertools.product(_LEADS, _TARGETS, _GLOSSES):
        fragments = _fragments(target, gloss)
        for marker, span in _wrappers(fragments):
            at: dict[int, str] = dict.fromkeys(_slots(fragments), "")
            if span is not None:
                at[span[0]] += marker
                at[span[1]] += marker
            body = "".join(at[index] + fragment for index, fragment in enumerate(fragments))
            yield f"{lead}{body}{at[len(fragments)]}"


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


# The one destination that is not a destination: the human fallback. ABSENT goes there (Obj3), and
# so does a PR_REVIEW whose token nobody can parse — ``core.py`` re-runs ``parse_pr_ref`` before
# firing and stops at the human when it returns ``None``::
#
#     parsed_ref = parse_pr_ref(handoff.token) if handoff.token else None
#     if self._orchestrator is None or parsed_ref is None:
#         return self._stop(round_index, StopReason.HUMAN, ...)
#
# Telling those two apart here is not a stricter test, it is a wrong one: it reported 420
# IMPROVEMENTS as losses — lines like ``NEXT: pr-review _acme/widgets#7_.`` where the legacy parser
# produced a token that parses to nothing and the current one produces the real ref. Both "route"
# to PR_REVIEW; only one of them reaches the gate.
_UNROUTED = ("absent", None)


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
        return _UNROUTED if ref is None else ("pr_review", f"{ref.owner}/{ref.repo}#{ref.number}")
    return kind, None


def _losses(lines: Iterator[str] | list[str]) -> list[tuple[str, tuple[str, str | None]]]:
    """Lines the legacy parser routed that the current one does not route the same way."""
    lost = []
    for line in lines:
        legacy = _destination(legacy_resolve_handoff(line, _ROSTER))
        if legacy == _UNROUTED:
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
        expected = sum(
            len(_wrappers(_fragments(target, gloss)))
            for _lead, target, gloss in itertools.product(_LEADS, _TARGETS, _GLOSSES)
        )
        assert len(lines) == expected
        assert len(set(lines)) == len(lines), "the axes must not collide into duplicates"

    def test_the_position_set_grows_with_the_target_it_is_describing(self) -> None:
        # The property the old constant could not have: a target made of two parts has strictly
        # more places to put a marker than a target made of one. This is what makes "between the
        # sentinel and the ref" a position at all, and it is derived, so it cannot be forgotten
        # when a third-part target is added.
        one = _fragments(("Bohr",), "")
        two = _fragments(("pr-review", "acme/widgets#7"), "")
        assert len(_slots(two)) > len(_slots(one))

    def test_a_wrapper_can_close_inside_a_multi_part_target(self) -> None:
        # Round 5, as an assertion. Every position this file used to list was outside the target,
        # so a marker could only ever wrap the whole thing — and the sentinel-only span, which the
        # shipped parser dropped, was not in the product at all. Read the shapes off `_TARGETS`
        # rather than typing them, so this keeps testing whatever targets the file grows.
        lines = set(decorated_lines())
        for target in (target for target in _TARGETS if len(target) > 1):
            head, tail = target[0], " ".join(target[1:])
            for marker in _MARKERS:
                assert f"NEXT: {marker}{head}{marker} {tail}" in lines  # the sentinel alone
                assert f"NEXT: {head} {marker}{tail}{marker}" in lines  # the ref alone
                assert f"NEXT: {marker}{head} {tail}{marker}" in lines  # both together

    def test_it_contains_every_shape_the_review_found_by_hand(self) -> None:
        # msg-1148 §5-4 / §5-5 and msg-1163 §3 listed these after reading the diff and running both
        # parsers. The point of generating the input set is that it reaches them without being
        # told; if a future edit narrows the axes, this is the assertion that notices.
        lines = set(decorated_lines())
        for shape in (
            "_NEXT: Heisenberg_",
            "_NEXT: Bohr_",
            "__NEXT: Einstein__",
            "_NEXT: human_",
            "_NEXT: pr-review acme/widgets#7_",
            "**NEXT:** Heisenberg",
            "*NEXT:* Bohr",
            # msg-1163 §3: the sentinel's own wrapper — what the listed positions could not reach.
            "NEXT: _pr-review_ acme/widgets#7",
            "NEXT: __pr-review__ acme/widgets#7",
            "NEXT: pr-review _acme/widgets#7_",
            # and two more the derivation reached on its own: a wrapper closing before the gloss,
            # and one hugging the target rather than the colon.
            "_NEXT: pr-review acme/widgets#7_ — go",
            "NEXT: **Bohr**",
        ):
            assert shape in lines, shape

    def test_the_legacy_reference_routes_enough_of_both_inputs_to_be_a_test(self) -> None:
        # The differential is vacuously green if the legacy side routes nothing — a broken import,
        # a reference that no longer parses, an input set that degenerated. Both floors are set
        # well under what is measured today (23947 of 48519 generated; 459 of 898 corpus records),
        # so they catch a collapse without failing on a refinement.
        def routed(lines: Iterator[str] | list[str]) -> int:
            return sum(
                1
                for line in lines
                if _destination(legacy_resolve_handoff(line, _ROSTER)) != _UNROUTED
            )

        assert routed(decorated_lines()) > 15_000
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
