"""Tests for the conductor NEXT-handoff parser + resolver (msg-520 / msg-522 Obj3)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

import spirrow_mindwire.conductor.handoff as handoff_mod
from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    TIER_C_LABELS,
    Handoff,
    HandoffKind,
    build_handoff_protocol_block,
    parse_next_token,
    resolve_handoff,
)
from spirrow_mindwire.github.client import PrRef, parse_pr_ref
from spirrow_mindwire.value_objects import Role

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}


def test_no_authorisation_marker_is_parsed_from_message_bodies() -> None:
    # The DELEGATE marker that used to grant design→implement autonomy per thread is gone;
    # authorisation is per-project state (conductor/control.py), not something written into a
    # message. Pinned as a test because a marker re-introduced here would be a *second* source of
    # the same truth, and the two would drift the moment they disagreed.
    import spirrow_mindwire.conductor.handoff as handoff_mod

    assert not [name for name in dir(handoff_mod) if "delegate" in name.casefold()]


# --------------------------------------------------------------------------- #
# parse_next_token
# --------------------------------------------------------------------------- #


def test_parse_plain_token() -> None:
    assert parse_next_token("blah blah\n\nNEXT: Heisenberg") == "Heisenberg"


def test_parse_strips_cjk_parenthetical_gloss() -> None:
    # The real msg-520 handoff: a CJK-paren gloss follows the name.
    body = "design...\n\nNEXT: Einstein（独立 naysayer review）→ Tier-C"  # noqa: RUF001
    assert parse_next_token(body) == "Einstein"


def test_parse_strips_ascii_parenthetical_and_trailing_punct() -> None:
    assert parse_next_token("NEXT: Bohr (disposition ...)") == "Bohr"
    assert parse_next_token("NEXT: human.") == "human"


def test_parse_takes_last_next_line() -> None:
    # An earlier quoted NEXT (e.g. inside a relayed critique) must not win over the final handoff.
    body = "I am relaying a review that ended with\nNEXT: human\n\n...my reply...\nNEXT: Bohr"
    assert parse_next_token(body) == "Bohr"


def test_parse_none_when_absent() -> None:
    assert parse_next_token("a reply with no handoff line at all") is None
    # 'NEXT' must be the 'NEXT:' directive on its own line, not prose mentioning next.
    assert parse_next_token("the next thing we do is unclear") is None


# --------------------------------------------------------------------------- #
# resolve_handoff — PR-gate sentinel (PR-2b-2)
# --------------------------------------------------------------------------- #


def test_resolve_pr_review_sentinel() -> None:
    h = resolve_handoff("opened the PR\n\nNEXT: pr-review acme/widgets#7", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "acme/widgets#7"  # the whole ref, not just a leading name


def test_resolve_pr_review_case_insensitive_and_strips_surrounding_space() -> None:
    h = resolve_handoff("x\n\nNEXT:  PR-Review   owner/repo#42  ", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "owner/repo#42"


def test_resolve_pr_review_accepts_a_url_ref() -> None:
    # CHANGED with the delegation (msg-1158 blocking / msg-1159 §2): the token is the owner's
    # canonical slug, not the URL substring the author typed. This module no longer knows what a
    # PR ref looks like, so it cannot report where one began and ended — only what `parse_pr_ref`
    # made of the operand, and that function normalises a URL. The destination is unchanged:
    # `core.py` already re-parsed this token to `parsed_ref.slug` before firing the gate, so the
    # PR the gate runs on is byte-identical to what this assertion pinned before.
    url = "https://github.com/acme/widgets/pull/7"
    h = resolve_handoff(f"done\n\nNEXT: pr-review {url}", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "acme/widgets#7"
    assert parse_pr_ref(h.token or "") == parse_pr_ref(url)  # same PR, spelled canonically


def test_resolve_bare_pr_review_without_ref_falls_through_to_absent() -> None:
    # "pr-review" alone is not the PR-gate sentinel (the sentinel is the word plus an operand); it
    # falls through to a roster lookup, which fails → ABSENT (route to human), not a half-formed
    # gate fire.
    h = resolve_handoff("oops\n\nNEXT: pr-review", _ROSTER)
    assert h.kind is HandoffKind.ABSENT


def test_resolve_pr_review_ignores_a_trailing_gloss() -> None:
    # LLMs often append a parenthetical gloss. The ref pattern matches a ref; the gloss is simply
    # not one, so it is outside the match (Tier B PR #103 round 2).
    h = resolve_handoff("done\n\nNEXT: pr-review acme/widgets#7 (please review)", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "acme/widgets#7"


def test_resolve_pr_review_ignores_punctuation_against_the_ref() -> None:
    # A ref directly followed by sentence punctuation (no space) still resolves cleanly: `.` and `,`
    # are not ref characters, so the match ends at the PR number (Tier B PR #103 round 3).
    dot = resolve_handoff("a\n\nNEXT: pr-review acme/widgets#7.", _ROSTER)
    assert dot.kind is HandoffKind.PR_REVIEW
    assert dot.token == "acme/widgets#7"
    comma = resolve_handoff("b\n\nNEXT: pr-review acme/widgets#7, please review now", _ROSTER)
    assert comma.token == "acme/widgets#7"


class TestTheSentinelMayCarryItsOwnDecoration:
    """A wrapper may close between the sentinel and its operand (msg-1163 §1, blocking).

    ``_pr-review_`` failed where ``*pr-review*`` and ``` `pr-review` ``` worked, because the
    sentinel was terminated with ``\\b`` and ``\\b`` is defined by ``\\w``, which counts ``_`` as a
    word character. The line rule had already moved ``_`` to the decoration side; the terminator
    had not, so the two disagreed and the directive fell out as ABSENT — a silent stop, which is
    the failure this whole module exists to prevent.
    """

    def test_the_sentinel_may_be_wrapped_on_its_own(self) -> None:
        for line in (
            "NEXT: _pr-review_ acme/widgets#7",
            "NEXT: __pr-review__ acme/widgets#7",
            "NEXT: *pr-review* acme/widgets#7",
            "NEXT: **pr-review** acme/widgets#7",
            "NEXT: `pr-review` acme/widgets#7",
        ):
            h = resolve_handoff(f"done\n\n{line}", _ROSTER)
            assert h.kind is HandoffKind.PR_REVIEW, line
            assert h.token == "acme/widgets#7", line

    def test_the_wrapper_may_close_anywhere_the_author_put_it(self) -> None:
        # The four spans of one underscore pair over `pr-review <ref>`: sentinel only, ref only,
        # both, and the whole line with a gloss outside it. All four mean the same handoff.
        for line in (
            "NEXT: _pr-review_ acme/widgets#7",
            "NEXT: pr-review _acme/widgets#7_",
            "NEXT: _pr-review acme/widgets#7_",
            "_NEXT: pr-review acme/widgets#7_ — go",
        ):
            h = resolve_handoff(f"done\n\n{line}", _ROSTER)
            assert h.kind is HandoffKind.PR_REVIEW, line
            assert h.token == "acme/widgets#7", line

    def test_a_longer_word_is_still_not_the_sentinel(self) -> None:
        # The terminator is looser about `_` and no looser about anything else: a word that merely
        # STARTS with the sentinel is not the sentinel, in ASCII or beyond it.
        for line in (
            "NEXT: pr-reviewing acme/widgets#7",
            "NEXT: pr-review7 acme/widgets#7",
            "NEXT: pr-reviewあ acme/widgets#7",
        ):
            assert resolve_handoff(f"x\n\n{line}", _ROSTER).kind is HandoffKind.ABSENT, line


class TestThePrRefGrammarHasOneOwner:
    """`parse_pr_ref` decides what a PR ref is; this module asks it (msg-1158 blocking / 1159 §2).

    The revision before this one carried a second copy of that grammar, under a comment asserting
    the two "agree by construction". They already disagreed: `acme/widgets#7abc` resolved to
    `acme/widgets#7` here and to `None` there (msg-1158 §5). These pin the *ownership*, not the
    shapes — shapes are exactly what a second spelling gets right until the day it does not.
    """

    def test_a_ref_shape_only_the_owner_knows_is_routed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The concrete cost of a second spelling is not that today's shapes are wrong, it is that
        # tomorrow's are withheld: when `parse_pr_ref` learns an enterprise host or a short link,
        # a copy here has to be edited too or the gate keeps firing on the old vocabulary. So teach
        # the owner a shape this module has never heard of and require it to arrive unedited.
        monkeypatch.setattr(
            handoff_mod,
            "parse_pr_ref",
            lambda text: PrRef("acme", "widgets", 7) if "ENTERPRISE" in text else None,
        )
        h = resolve_handoff("done\n\nNEXT: pr-review ENTERPRISE-widgets-seven", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_the_sentinel_never_claims_a_ref_the_owner_rejects(self) -> None:
        # The measured divergence, inverted. The author still asked for a gate (so this is the
        # sentinel, not ABSENT), but the operand resolves to whatever the owner says about it —
        # here `None`, which `core.py` turns into a stop at the human rather than a gate fired on
        # a ref one component invented and the other does not recognise.
        h = resolve_handoff("x\n\nNEXT: pr-review acme/widgets#7abc", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert parse_pr_ref(h.token or "") is None

    def test_the_two_components_agree_on_every_shape_this_file_pins(self) -> None:
        # "Agree by construction" was a claim, and claims about two spellings are what failed. It
        # is now checkable: for every operand shape in this file, what the conductor reports and
        # what the validator downstream extracts must be the same PR.
        for operand in (
            "acme/widgets#7",
            "acme/widgets#7.",
            "acme/widgets#7, please review now",
            "acme/widgets#7 (please review)",
            "acme/my_repo#7",
            "acme/widgets#7** — please gate",
            "https://github.com/acme/my_repo/pull/7",
            "acme/widgets#7abc",  # the one they used to split on
            "please gate this",  # no ref at all
        ):
            h = resolve_handoff(f"body\n\nNEXT: pr-review {operand}", _ROSTER)
            assert h.kind is HandoffKind.PR_REVIEW
            assert parse_pr_ref(h.token or "") == parse_pr_ref(operand), operand

    def test_no_pattern_here_spells_out_a_pr_ref(self) -> None:
        # A smoke guard in the spirit of the DELEGATE-marker test above: it cannot prove the
        # grammar is absent, but a re-introduced copy would almost certainly carry one of these
        # two literals, and the test names what it is guarding so the next author sees the rule.
        spellings = [
            name
            for name, value in vars(handoff_mod).items()
            if isinstance(value, re.Pattern) and ("pull/" in value.pattern or "#" in value.pattern)
        ]
        assert spellings == [], f"{spellings} re-spell a grammar spirrow_mindwire.github owns"


# --------------------------------------------------------------------------- #
# resolve_handoff
# --------------------------------------------------------------------------- #


def test_resolve_role_from_roster() -> None:
    h = resolve_handoff("...\nNEXT: Heisenberg", _ROSTER)
    assert h == Handoff(
        kind=HandoffKind.ROLE, identity="Heisenberg", role=Role.IMPLEMENTER, token="Heisenberg"
    )


def test_resolve_is_case_insensitive_and_canonicalizes_identity() -> None:
    h = resolve_handoff("NEXT: einstein", _ROSTER)
    assert h.kind is HandoffKind.ROLE
    assert h.role is Role.NAYSAYER
    assert h.identity == "Einstein"  # canonical roster spelling, not the raw token


def test_resolve_human_and_none_sentinels() -> None:
    assert resolve_handoff("NEXT: human", _ROSTER).kind is HandoffKind.HUMAN
    assert resolve_handoff("NEXT: Human", _ROSTER).kind is HandoffKind.HUMAN
    assert resolve_handoff("NEXT: none", _ROSTER).kind is HandoffKind.NONE


def test_resolve_absent_when_no_next_line() -> None:
    assert resolve_handoff("no handoff here", _ROSTER).kind is HandoffKind.ABSENT


def test_resolve_unknown_participant_is_absent() -> None:
    # Obj3: an unknown name is not silently dropped — it resolves to ABSENT so the conductor routes
    # to a human rather than stranding the thread.
    h = resolve_handoff("NEXT: Schrodinger", _ROSTER)
    assert h.kind is HandoffKind.ABSENT
    assert h.token == "Schrodinger"


# --------------------------------------------------------------------------- #
# build_handoff_protocol_block (emission side — PR-2b-1)
# --------------------------------------------------------------------------- #


def test_protocol_block_uses_the_reserved_sentinels_for_every_role() -> None:
    # The emitted instructions and the parser read the SAME vocabulary (single SOT): the block
    # spells the reserved words from HUMAN_TOKEN / NONE_TOKEN so emit and parse cannot drift.
    for role in Role:
        block = build_handoff_protocol_block(role)
        assert "NEXT:" in block
        assert f"NEXT: {HUMAN_TOKEN}" in block
        assert f"NEXT: {NONE_TOKEN}" in block


def test_proposer_block_forbids_direct_implementer_handoff() -> None:
    block = build_handoff_protocol_block(Role.PROPOSER)
    assert "naysayer" in block
    assert "Do NOT hand a design straight to the implementer" in block


def test_proposer_block_hands_back_to_naysayer_after_disposition() -> None:
    # A (T-human-terminal-overuse, msg-890 §1): the proposer's post-disposition handoff goes to
    # the NAYSAYER, not to the human. This is what unlocks carve-out ③ (the naysayer's own proceed
    # is the only path to autonomous implementation) for a design the proposer has finished
    # answering objections on. If the guidance ever regresses to pointing at `human` here, the
    # observed defect (msg-882 §実測1: 85.8% of stops are on `human`, mostly non-judgement) comes
    # straight back — this pins the fix in words.
    block = build_handoff_protocol_block(Role.PROPOSER)
    assert "hand BACK to the naysayer" in block
    # And the reason the redirect is safe: the proposer never advances a design to code itself.
    assert "only the naysayer may advance a design to code" in block


def test_proposer_block_is_control_state_agnostic() -> None:
    # Bohr msg-890 §1: the proposer's guidance is written to be true under any loop control state
    # (`run` / `supervised` / `hold`), so the proposer never has to know what state the loop is
    # in — state-dependent branching is closed inside the conductor. This test pins that the
    # proposer's block does not name any of the control-state vocabulary; if it did, the text
    # would go stale the moment loop control changed. Naysayer guidance IS state-aware (existing)
    # and is explicitly out of scope; only the proposer block is asserted here.
    block = build_handoff_protocol_block(Role.PROPOSER)
    for state_word in ("`run`", "`hold`", "`supervised`", "autonomously"):
        assert state_word not in block, (
            f"proposer guidance leaked control state vocabulary: {state_word!r}"
        )


def test_proposer_block_teaches_tier_c_syntax_and_enum() -> None:
    # C (msg-890 §3): the proposer's block documents the `TIER-C: <label>` syntax and lists the
    # enum, so a cooperating proposer can name the class of Tier-C decision it is asking for. The
    # tag itself is non-blocking (see the parser tests below), but the guidance has to spell the
    # syntax somewhere or the tag is invisible to the author. This lives with the proposer because
    # explicit-human handoffs from disposition turns are the dominant class msg-882 counted.
    block = build_handoff_protocol_block(Role.PROPOSER)
    assert "TIER-C:" in block
    for label in TIER_C_LABELS:
        assert f"`{label}`" in block, f"enum label {label!r} missing from proposer guidance"
    assert "other:<one-line reason>" in block
    assert "does NOT redefine" in block  # calibration-not-definition mantra kept in the text


def test_implementer_block_hands_back_to_proposer_and_never_merges() -> None:
    block = build_handoff_protocol_block(Role.IMPLEMENTER)
    assert "proposer" in block
    assert "never merge" in block


def test_naysayer_block_is_advisory() -> None:
    block = build_handoff_protocol_block(Role.NAYSAYER)
    assert "advisory, not a veto" in block


# --------------------------------------------------------------------------- #
# C — TIER-C: <label> look-back parse (msg-890 §3, Einstein msg-891 §4)
#
# Both sides of the measurement are exercised: presence records the label, absence records None.
# NON-BLOCKING is the load-bearing property — the `Handoff.kind` on the human terminal is HUMAN
# whether or not the tag is there; the tag NEVER changes the routing decision. If it did, the
# calibrator would destroy its own calibration (missing labels would stop being observable).
# --------------------------------------------------------------------------- #


class TestTierCLabelLookback:
    """`TIER-C: <label>` on line n-1 of the LAST `NEXT: human`, else None."""

    def test_records_enum_label_from_the_line_above_next_human(self) -> None:
        body = "body\n\nTIER-C: merge-protected\nNEXT: human"
        h = resolve_handoff(body, _ROSTER)
        assert h.kind is HandoffKind.HUMAN
        assert h.tier_c_label == "merge-protected"

    def test_case_of_label_is_normalised_but_reason_of_other_is_preserved(self) -> None:
        # `SCOPE` and `scope` are the same label for aggregation, but the reason text on an
        # `other:` label carries information the observer wrote — preserve its case.
        h = resolve_handoff("TIER-C: SCOPE\nNEXT: human", _ROSTER)
        assert h.tier_c_label == "scope"
        h = resolve_handoff("TIER-C: other: Redistribute Weekly Budget\nNEXT: human", _ROSTER)
        assert h.tier_c_label == "other:Redistribute Weekly Budget"

    def test_all_enum_labels_parse(self) -> None:
        # Guard against a rename of the enum silently dropping one label from parse (a shape the
        # test-per-label pattern would miss because tests are per-label written by the author who
        # knew the current list).
        for label in TIER_C_LABELS:
            h = resolve_handoff(f"TIER-C: {label}\nNEXT: human", _ROSTER)
            assert h.tier_c_label == label, label

    def test_absence_of_tier_c_line_records_none_and_does_not_reject(self) -> None:
        # The load-bearing non-blocking property: no tag, still HUMAN, tag is None.
        h = resolve_handoff("just a body\n\nNEXT: human", _ROSTER)
        assert h.kind is HandoffKind.HUMAN
        assert h.tier_c_label is None

    def test_unknown_label_is_treated_as_absent_not_as_reject(self) -> None:
        # A label that is not in the closed enum and does not begin with `other:` is silently
        # ignored — we record its absence, not a syntax error. Rejecting would block, which is
        # explicitly out of scope for v1 (msg-890 §3: 非ブロッキング).
        h = resolve_handoff("TIER-C: fabricated-label\nNEXT: human", _ROSTER)
        assert h.kind is HandoffKind.HUMAN
        assert h.tier_c_label is None

    def test_blank_line_between_tier_c_and_next_defeats_the_lookback(self) -> None:
        # Einstein msg-891 §4 pinned the lookback to line n-1 (strict adjacency). A blank line
        # makes the label's home ambiguous — we take that as "no label" rather than scanning
        # backwards. Loose scanning would false-positive on quoted TIER-C: text elsewhere in the
        # reply, and would corrupt the very measurement this parser exists to enable.
        h = resolve_handoff("TIER-C: scope\n\nNEXT: human", _ROSTER)
        assert h.kind is HandoffKind.HUMAN
        assert h.tier_c_label is None

    def test_last_next_human_wins_over_earlier_tier_c_label(self) -> None:
        # If an earlier quoted `NEXT: human` had a TIER-C: line above it and the REAL final
        # handoff does not, we record None. The tag belongs to the FINAL handoff — same
        # last-wins rule the NEXT: parser uses, applied to the label lookback.
        body = (
            "I am quoting an earlier decision that read:\n"
            "TIER-C: billing\n"
            "NEXT: human\n"
            "\n"
            "...my reply...\n"
            "NEXT: human"
        )
        h = resolve_handoff(body, _ROSTER)
        assert h.kind is HandoffKind.HUMAN
        assert h.tier_c_label is None

    def test_tier_c_line_without_a_next_human_line_at_all_does_not_leak(self) -> None:
        # A TIER-C: line with no NEXT: line at all resolves to ABSENT (no handoff), and the tag
        # is NOT smuggled onto the ABSENT handoff. v1 explicitly parses tags only for HUMAN.
        h = resolve_handoff("TIER-C: scope\n(nothing else)", _ROSTER)
        assert h.kind is HandoffKind.ABSENT
        assert h.tier_c_label is None

    def test_role_handoff_carries_no_tier_c_label_even_when_line_is_present(self) -> None:
        # If someone writes TIER-C: above a ROLE handoff, we do not record it. In v1 the tag is a
        # measurement of explicit HUMAN terminals only (msg-890 §3), so ROLE / PR_REVIEW / NONE
        # stay clean.
        h = resolve_handoff("TIER-C: scope\nNEXT: Heisenberg", _ROSTER)
        assert h.kind is HandoffKind.ROLE
        assert h.tier_c_label is None

    def test_tier_c_line_is_case_insensitive_at_the_keyword(self) -> None:
        # `tier-c:` / `Tier-C:` / `TIER-C:` all mean the same to the parser — the author's shift
        # key is not part of the enum vocabulary.
        for keyword in ("TIER-C:", "tier-c:", "Tier-C:"):
            h = resolve_handoff(f"{keyword} scope\nNEXT: human", _ROSTER)
            assert h.tier_c_label == "scope", keyword

    def test_whitespace_after_the_keyword_is_optional(self) -> None:
        # Gemini PR-gate critique on #173: requiring `\s+` after `TIER-C:` silently misclassifies
        # `TIER-C:scope` (no space) as ABSENT, corrupting the calibration denominator. Zero, one,
        # or many spaces between the colon and the label MUST all resolve to the same tag —
        # otherwise the "missing label" baseline is inflated by every unspaced author, and A2's
        # pre-registered threshold (>20% missing) reads a phantom signal.
        for spacing in ("", " ", "  ", "\t"):
            body = f"TIER-C:{spacing}scope\nNEXT: human"
            h = resolve_handoff(body, _ROSTER)
            assert h.tier_c_label == "scope", repr(spacing)
        # `other:` label similarly: the space between `TIER-C:` and `other:` is also optional.
        h = resolve_handoff("TIER-C:other:reason\nNEXT: human", _ROSTER)
        assert h.tier_c_label == "other:reason"

    def test_missing_colon_after_tier_c_still_does_not_match(self) -> None:
        # The relaxation to `\s*` narrows only the whitespace requirement, not the colon
        # requirement. `TIER-Cscope` (missing colon entirely) must still be treated as ABSENT so
        # a stray typo does not silently mint a label from the following word.
        h = resolve_handoff("TIER-Cscope\nNEXT: human", _ROSTER)
        assert h.kind is HandoffKind.HUMAN
        assert h.tier_c_label is None


# --------------------------------------------------------------------------- #
# Layer 3 — the structured ``next_participant`` envelope field (Bohr msg-179 §3).
#
# The field is the source of truth for routing when it is present; the body's ``NEXT:`` line is
# kept only as a **lint** against it (§3-2: same resolver, no second parser). Five quadrants:
#
#   1. field=None,     body no NEXT       → ABSENT  (pre-Layer-3 fallback)
#   2. field=None,     body has NEXT      → follow body  (pre-Layer-3 fallback)
#   3. field=<target>, body no NEXT       → follow field, NO event  ← msg-1438's silent quadrant
#   4. field=<target>, body agrees        → follow field, NO event
#   5. field=<target>, body disagrees     → HUMAN + field_mismatch=True (safety valve)
#
# Row 3 is the exact quadrant that stalled the loop for 2 days: a judgement-page decide carried
# ``next_participant: "human"`` but wrote no ``NEXT:`` line in the body. The consumer resolved
# ABSENT and stopped on NO_HANDOFF while the field named a target. This section pins that quadrant
# closed, plus §6's regression guard (a field-bearing msg cannot yield NO_HANDOFF).
# --------------------------------------------------------------------------- #


class TestNextParticipantFieldConsumer:
    """The 5 quadrants of `(field, body)` from Bohr msg-179 §3-1, and the §6 invariant."""

    def test_row1_no_field_no_body_next_is_absent(self) -> None:
        # Pre-Layer-3 fallback preserved: no field, no body NEXT → ABSENT (routes to human via
        # the conductor's Obj3 path, not by this resolver).
        h = resolve_handoff("just a body\nno handoff at all", _ROSTER)
        assert h.kind is HandoffKind.ABSENT
        assert h.field_mismatch is False
        assert h.mismatch_body_token is None

    def test_row2_no_field_body_next_follows_body(self) -> None:
        # Pre-Layer-3 fallback: no field, body has NEXT → follow the body. Backward compatible
        # for every caller that has not started passing the envelope field yet.
        h = resolve_handoff("thinking...\n\nNEXT: Bohr", _ROSTER)
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Bohr"
        assert h.field_mismatch is False

    def test_row2_explicit_none_field_is_treated_as_absent_field(self) -> None:
        # A magickit that ships `next_participant: null` states the same fact as omitting the key
        # entirely. Empty / whitespace-only strings are treated the same way (the sender did not
        # name anyone). All fall through to body-only resolution — the pre-Layer-3 path.
        body = "reply\n\nNEXT: Bohr"
        for empty in (None, "", "   ", "\n"):
            h = resolve_handoff(body, _ROSTER, next_participant=empty)
            assert h.kind is HandoffKind.ROLE, repr(empty)
            assert h.identity == "Bohr", repr(empty)
            assert h.field_mismatch is False, repr(empty)

    def test_row3_field_present_body_absent_follows_field_quietly(self) -> None:
        # msg-1438's silent quadrant: judgement-page decide carries the field but no body NEXT.
        # This must route to the field's target WITHOUT firing a mismatch event - the field is
        # authoritative and the body has nothing to disagree with. This row is a *normal* path
        # (§3-1 rows 3: field-present + body-absent is spec'd as normal, not an escalation).
        body = "approved for implementation."
        h = resolve_handoff(body, _ROSTER, next_participant="human")
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is False
        assert h.mismatch_body_token is None

    def test_row3_field_routes_a_persona_when_body_has_no_next(self) -> None:
        # Same row 3 but with a persona field: the judgement page selected a specific participant,
        # the body carries no NEXT: line, and the routing follows the field silently.
        h = resolve_handoff("nothing to add.", _ROSTER, next_participant="Einstein")
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Einstein"
        assert h.role is Role.NAYSAYER
        assert h.field_mismatch is False

    def test_row4_field_and_body_agree_follows_field_quietly(self) -> None:
        # The cooperative case: an author wrote a NEXT: line AND set the field, and they agree.
        # No event, no mismatch — the field wins (both sides say the same thing anyway).
        h = resolve_handoff("reply\n\nNEXT: Bohr", _ROSTER, next_participant="Bohr")
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Bohr"
        assert h.field_mismatch is False

    def test_row4_agreement_is_case_insensitive_on_the_identity(self) -> None:
        # Roster lookup canonicalises `einstein` → `Einstein` on both sides, so agreement is
        # measured post-canonicalisation. This matters because the field may come from a form
        # (already canonical) while the body was hand-typed (may be lowercased).
        h = resolve_handoff("reply\n\nNEXT: einstein", _ROSTER, next_participant="Einstein")
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Einstein"
        assert h.field_mismatch is False

    def test_row4_human_sentinel_agreement(self) -> None:
        h = resolve_handoff("done\n\nNEXT: human", _ROSTER, next_participant="human")
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is False

    def test_row5_mismatch_escalates_to_human_with_mismatch_flag(self) -> None:
        # The safety valve: field says Einstein, body says Bohr. Neither wins the routing — the
        # turn is escalated to the human and the divergence is flagged so the conductor can log
        # it as observability. The `token` records what the FIELD said (the authoritative
        # decision on the wire), `mismatch_body_token` records what the body's fallback would
        # have routed to.
        h = resolve_handoff("reply\n\nNEXT: Bohr", _ROSTER, next_participant="Einstein")
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True
        assert h.token == "Einstein"
        assert h.mismatch_body_token == "Bohr"

    def test_row5_mismatch_body_pr_review_vs_field_persona(self) -> None:
        # A body pr-review sentinel and a field persona are different targets → mismatch → human.
        h = resolve_handoff(
            "opened it\n\nNEXT: pr-review acme/widgets#7",
            _ROSTER,
            next_participant="Bohr",
        )
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True
        assert h.token == "Bohr"
        assert h.mismatch_body_token == "acme/widgets#7"

    def test_row5_field_pointing_at_unknown_participant_escalates(self) -> None:
        # A field value that fails to resolve to any known participant (unknown token / not a
        # sentinel) is treated as a mismatch: escalate to human. The write side is supposed to
        # reject such a field with NextParticipantUnknownError; making the read side loud is the
        # fail-safe for the case a bad field slipped past write-side validation (§3-3 escape hatch
        # is "drop field and re-send", which a mismatch-to-human turn asks the human to do).
        h = resolve_handoff("done\n\nNEXT: Bohr", _ROSTER, next_participant="Schrodinger")
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True
        assert h.token == "Schrodinger"
        assert h.mismatch_body_token == "Bohr"

    def test_row5_disagreement_between_sentinels_escalates(self) -> None:
        # Body says NEXT: none (settled), field says human. The kinds differ, so this is a
        # mismatch: escalate to human with the flag set.
        h = resolve_handoff("done\n\nNEXT: none", _ROSTER, next_participant="human")
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True
        assert h.mismatch_body_token == "none"

    def test_no_second_grammar_is_introduced_for_the_lint(self) -> None:
        # §3-2: "lint は新しい文法を持たない — フォールバックの resolver をそのまま使う". The lint's
        # question is exactly "would the body-only resolver have routed to a different target?",
        # so the two sides read the same vocabulary. This test pins that the field resolves via
        # the SAME name / sentinel matching the body uses: casefold on sentinels, roster lookup
        # on personas, ABSENT on unknowns.
        # Body-only resolution of "Einstein":
        body_only = resolve_handoff("NEXT: Einstein", _ROSTER)
        # Field-only resolution of "Einstein" (empty body):
        field_only = resolve_handoff("no next line", _ROSTER, next_participant="Einstein")
        assert body_only.kind is field_only.kind
        assert body_only.identity == field_only.identity
        assert body_only.role is field_only.role

    def test_body_only_resolve_is_backward_compatible(self) -> None:
        # Every pre-Layer-3 call site that passes no next_participant must see identical output
        # to before this change. Signature widened with a keyword-only default; the body-only
        # branch is byte-identical to the old function.
        # A sampling of pre-Layer-3 cases:
        for body, expected_kind in (
            ("NEXT: Bohr", HandoffKind.ROLE),
            ("NEXT: human", HandoffKind.HUMAN),
            ("NEXT: none", HandoffKind.NONE),
            ("NEXT: pr-review acme/widgets#7", HandoffKind.PR_REVIEW),
            ("no handoff", HandoffKind.ABSENT),
            ("NEXT: Schrodinger", HandoffKind.ABSENT),
        ):
            h = resolve_handoff(body, _ROSTER)
            assert h.kind is expected_kind, body
            assert h.field_mismatch is False, body
            assert h.mismatch_body_token is None, body


class TestNextParticipantFieldPrReviewSentinel:
    """The `pr-review <ref>` sentinel on the FIELD side (msg-#184 PR-gate REQUEST_CHANGES fix).

    §3-2 forbids a second grammar for the lint, and the docstring in ``core.py`` promises the
    Layer-3 resolver rewrites every field-bearing case into ROLE / HUMAN / NONE / PR_REVIEW.
    Missing the `pr-review` sentinel on the field side breaks the field-driven PR-gate
    (ADR-19 N-1): a field of ``pr-review acme/widgets#7`` falls out of every persona branch,
    resolves to ABSENT, is seen as a mismatch by ``_reconcile``, and escalates to the human —
    silently disabling the synchronous Tier B review whenever the envelope field is the
    authoritative handoff. These tests pin that the field side speaks the same PR-gate
    vocabulary as the body side.
    """

    def test_field_pr_review_with_body_absent_dispatches_the_gate(self) -> None:
        # Row 3, PR-gate variant: judgement-page decide sets next_participant to a pr-review
        # directive, body has no NEXT: line. This must route to PR_REVIEW quietly — the exact
        # normal-path guarantee msg-1438 was about, but for the gate-firing quadrant.
        h = resolve_handoff(
            "opened the PR.",
            _ROSTER,
            next_participant="pr-review acme/widgets#7",
        )
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"
        assert h.field_mismatch is False
        assert h.mismatch_body_token is None

    def test_field_pr_review_accepts_a_pr_url_and_normalises_to_the_slug(self) -> None:
        # Same normalisation the body path gets for free from parse_pr_ref: a raw URL in the
        # field arrives here as the canonical owner/repo#n slug. Symmetric with body_only.
        url = "https://github.com/acme/widgets/pull/7"
        h = resolve_handoff("done.", _ROSTER, next_participant=f"pr-review {url}")
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_field_pr_review_agrees_with_body_pr_review_no_mismatch(self) -> None:
        # Row 4, PR-gate variant: both sides asked for the gate on the same ref → route quietly,
        # no mismatch event. _same_target already compares by canonical slug so both sides
        # agree post-normalisation even if they typed different shapes of the same ref.
        h = resolve_handoff(
            "opened it.\n\nNEXT: pr-review acme/widgets#7",
            _ROSTER,
            next_participant="pr-review acme/widgets#7",
        )
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"
        assert h.field_mismatch is False

    def test_field_pr_review_disagrees_with_body_pr_review_on_different_ref(self) -> None:
        # Row 5, PR-gate variant: both sides asked for the gate but named different PRs. This is
        # a genuine divergence (someone typed the wrong number in one place) → escalate to human
        # with the flag set, and record what BOTH sides said so the divergence is loud.
        h = resolve_handoff(
            "opened it.\n\nNEXT: pr-review acme/widgets#8",
            _ROSTER,
            next_participant="pr-review acme/widgets#7",
        )
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True
        assert h.token == "acme/widgets#7"
        assert h.mismatch_body_token == "acme/widgets#8"

    def test_field_pr_review_disagrees_with_body_persona_escalates(self) -> None:
        # Row 5, mixed kinds: field says gate this PR, body says hand to Bohr. Different
        # targets → escalate. Mirror of test_row5_mismatch_body_pr_review_vs_field_persona
        # (which pinned the reverse: body pr-review vs field persona) so both directions are
        # symmetric under §3-2 (the same resolver on both sides).
        h = resolve_handoff(
            "reply.\n\nNEXT: Bohr",
            _ROSTER,
            next_participant="pr-review acme/widgets#7",
        )
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True
        assert h.token == "acme/widgets#7"
        assert h.mismatch_body_token == "Bohr"

    def test_field_pr_review_agrees_when_body_has_no_next(self) -> None:
        # Row 3 restated: body absent must NOT be reported as a mismatch, even for pr-review.
        # This is the exact silent quadrant the sentinel-omission bug turned into a HUMAN
        # escalation before the fix.
        h = resolve_handoff("", _ROSTER, next_participant="pr-review acme/widgets#7")
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.field_mismatch is False

    def test_field_pr_review_bare_word_without_operand_is_absent(self) -> None:
        # Symmetric with the body path: ``pr-review`` alone is not the sentinel. The body path
        # falls through to the participant matcher, which sees it as an unknown persona and
        # returns ABSENT. On the field side we do the same, which _reconcile then turns into
        # a row-5 mismatch (safety valve) — the sender wrote a partial directive and we do
        # NOT silently swallow it.
        h = resolve_handoff("", _ROSTER, next_participant="pr-review")
        # Field resolved to ABSENT → row-5 escalation with field_mismatch=True.
        assert h.kind is HandoffKind.HUMAN
        assert h.field_mismatch is True

    def test_field_pr_review_with_unparseable_ref_carries_operand_forward(self) -> None:
        # Symmetric with the body path: the sender asked for a gate; the ref is unparseable to
        # parse_pr_ref. Carry the raw operand forward as the token so the conductor's
        # re-validation in core.py fails safe to the human (Tier B PR #103 round 4). The route
        # still resolves to PR_REVIEW here — this side of the boundary does not shape-check
        # PR refs, that is parse_pr_ref's job (msg-1158 §5).
        h = resolve_handoff("", _ROSTER, next_participant="pr-review not-a-real-ref")
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "not-a-real-ref"
        assert h.field_mismatch is False

    def test_field_pr_review_vocabulary_matches_body(self) -> None:
        # §3-2 pin: "lint は新しい文法を持たない". A field ``pr-review <ref>`` and a body
        # ``NEXT: pr-review <ref>`` must resolve to the SAME Handoff — kind, token, and
        # everything else. The one lit-up difference is that the body-side sees `NEXT:` text
        # (which the field never had); post-resolution the two are indistinguishable.
        body_only = resolve_handoff("NEXT: pr-review acme/widgets#7", _ROSTER)
        field_only = resolve_handoff("", _ROSTER, next_participant="pr-review acme/widgets#7")
        assert body_only.kind is field_only.kind
        assert body_only.token == field_only.token


# --------------------------------------------------------------------------- #
# Layer-2 Markdown tolerance — the *safety* tests come first.
#
# Layer 2 is a transitional bridge: an LLM sometimes wraps its final `NEXT:` line in
# Markdown noise, and the parser must still find the handoff. It is explicitly a
# temporary layer — Layer 3 (a structured `next_participant` field on the message)
# will supersede it and this scaffolding is to be removed then, NOT kept as a
# permanent legacy fallback.
#
# The tolerance is ADDITIVE (msg-1129 §3). Two earlier rounds tried to *remove* the
# decoration and both shipped green while the real shape stayed broken, because the
# set of characters to remove does not close. Nothing is removed now: each target is
# matched by a pattern describing that target, so whatever surrounds it is simply
# outside the match.
#
# The order is deliberate: the FIRST tests below pin that the payload survives —
# most importantly the `#` inside a `pr-review owner/repo#n` ref. Under the additive
# design that holds for a structural reason, and the tests say which: the `#` and the
# `_` are matched AS PART OF the ref pattern. They are not spared by a strip that was
# carefully aimed elsewhere; there is no strip.
# --------------------------------------------------------------------------- #


class TestPrReviewPayloadIsMatchedNotSalvaged:
    """`#` in `pr-review owner/repo#n` is payload — it is part of the ref pattern."""

    def test_blockquote_prefixed_pr_review_keeps_the_hash_ref(self) -> None:
        # `> NEXT: pr-review owner/repo#7` — the blockquote `>` is outside the ref
        # pattern, the `#7` is inside it.
        h = resolve_handoff("done\n\n> NEXT: pr-review acme/widgets#7", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_heading_prefixed_pr_review_keeps_the_hash_ref(self) -> None:
        # `# NEXT: pr-review owner/repo#7`. The leading `#` and the `#` in the ref are
        # the same character in two roles; the bug Einstein flagged is a rule about the
        # character (`.strip('#')`) rather than about the two things it appears in.
        h = resolve_handoff("done\n\n# NEXT: pr-review acme/widgets#7", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_bold_wrapped_pr_review_keeps_the_hash_ref(self) -> None:
        h = resolve_handoff("done\n\n**NEXT: pr-review acme/widgets#7**", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_list_bullet_prefixed_pr_review_keeps_the_hash_ref(self) -> None:
        h = resolve_handoff("done\n\n- NEXT: pr-review acme/widgets#7", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_pr_review_ref_hash_is_never_treated_as_trailing_punct(self) -> None:
        # Combined stressor: blockquote + bold + list, still no damage to `#7`.
        h = resolve_handoff("x\n\n> - **NEXT: pr-review acme/widgets#7**", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_atx_heading_close_after_ref_does_not_extend_the_ref(self) -> None:
        # An ATX heading may end with a run of `#` (`## Heading ##`). A trailing ` #`
        # is not part of a ref (a ref's `#` is followed by digits and preceded by no
        # space), so the ref pattern stops before it without a closing-marker rule.
        h = resolve_handoff("y\n\n# NEXT: pr-review acme/widgets#42 #", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#42"


class TestPersonaHandoffToleratesMarkdownNoise:
    """A single closed set of Markdown wrappers around the NEXT line is tolerated."""

    def test_blockquote_prefix(self) -> None:
        assert parse_next_token("body\n\n> NEXT: Heisenberg") == "Heisenberg"

    def test_heading_prefix(self) -> None:
        # `# NEXT: Bohr` (a bare ATX heading) is a common shape when an LLM stresses
        # its handoff line for emphasis.
        assert parse_next_token("body\n\n# NEXT: Bohr") == "Bohr"

    def test_list_bullet_prefix(self) -> None:
        assert parse_next_token("body\n\n- NEXT: Einstein") == "Einstein"
        assert parse_next_token("body\n\n* NEXT: Einstein") == "Einstein"
        assert parse_next_token("body\n\n+ NEXT: Einstein") == "Einstein"

    def test_bold_wrapper(self) -> None:
        assert parse_next_token("body\n\n**NEXT: Heisenberg**") == "Heisenberg"

    def test_italic_wrapper(self) -> None:
        assert parse_next_token("body\n\n*NEXT: Heisenberg*") == "Heisenberg"

    def test_inline_code_wrapper(self) -> None:
        # An LLM often quotes its own protocol literally in backticks.
        assert parse_next_token("body\n\n`NEXT: Heisenberg`") == "Heisenberg"

    def test_stacked_shell_markers_all_stripped(self) -> None:
        # blockquote + list + bold, still resolves to the persona.
        assert parse_next_token("body\n\n> - **NEXT: Bohr**") == "Bohr"

    def test_markdown_wrapped_sentinel_still_resolves(self) -> None:
        assert resolve_handoff("body\n\n> **NEXT: human**", _ROSTER).kind is HandoffKind.HUMAN
        assert resolve_handoff("body\n\n- `NEXT: none`", _ROSTER).kind is HandoffKind.NONE

    def test_markdown_wrapped_persona_resolves_case_insensitive(self) -> None:
        h = resolve_handoff("body\n\n**NEXT: einstein**", _ROSTER)
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Einstein"  # canonical spelling

    def test_underscore_wrapper(self) -> None:
        # `_` is a WORD character to Python's `re`, so a leading-decoration rule written as
        # `[^\w\n]*` cannot consume it and the whole line stops matching. This test and the two
        # below are the three shapes the additive rewrite dropped (msg-1148 §5-4); each is a
        # documented wrapper that the parser had handled the round before.
        assert parse_next_token("body\n\n_NEXT: Heisenberg_") == "Heisenberg"
        assert parse_next_token("body\n\n__NEXT: Einstein__") == "Einstein"
        assert resolve_handoff("body\n\n_NEXT: human_", _ROSTER).kind is HandoffKind.HUMAN

    def test_wrapper_closed_between_the_keyword_and_the_target(self) -> None:
        # `**NEXT:** X` — the close lands BETWEEN the colon and the name, so the token starts
        # `** Heisenberg`. `*` is not a word character, which is why the underscore fix alone
        # does not reach this one (msg-1148 §5-5: the second axis).
        assert parse_next_token("body\n\n**NEXT:** Heisenberg") == "Heisenberg"
        assert parse_next_token("body\n\n*NEXT:* Bohr") == "Bohr"
        assert parse_next_token("body\n\n`NEXT:` Bohr") == "Bohr"
        assert parse_next_token("body\n\nNEXT:** Bohr**") == "Bohr"

    def test_wrapper_closed_before_the_colon(self) -> None:
        # `**NEXT**: X` — bolding the keyword alone, the colon outside. Neither this parser's
        # predecessors nor this one handled it before; admitting decoration at two of the three
        # positions a wrapper can close in and not the third is an enumeration pretending to be a
        # rule, so all three are admitted.
        assert parse_next_token("body\n\n**NEXT**: Bohr") == "Bohr"
        assert resolve_handoff("body\n\n_NEXT_: human", _ROSTER).kind is HandoffKind.HUMAN

    def test_underscore_wrapped_pr_review_keeps_its_ref(self) -> None:
        h = resolve_handoff("body\n\n_NEXT: pr-review acme/widgets#7_", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_prose_is_still_refused_after_the_underscore_carve_out(self) -> None:
        # Moving `_` from "word character" to "decoration" must not let a sentence through: the
        # rest of the prose is still word characters, in either script.
        assert parse_next_token("…if the human writes NEXT: human, then the loop stops") is None
        assert parse_next_token("snake_case NEXT: Bohr") is None
        assert parse_next_token("その場合は NEXT: Bohr に渡す") is None


class TestLayer2DoesNotDoubleTheExtractionLoop:
    """Structural: the extraction stays a single pass (Einstein review, principle 3).

    The tolerance for Markdown shell characters must not be implemented by running the
    NEXT-scan twice (once RAW, once normalised) — that duplication would give two
    sources of truth for "which line is the handoff" and let them drift. The regex
    itself absorbs the shell, so a single ``findall`` still selects the last line.
    """

    def test_last_wins_still_holds_under_markdown_noise(self) -> None:
        # An earlier bare `NEXT: human` inside a relayed quote must still lose to the
        # final wrapped handoff — proof the last-wins scan runs once over the whole
        # body (not once RAW + once normalised, which could pick different winners).
        body = (
            "I am relaying a review that ended with\n"
            "NEXT: human\n"
            "\n"
            "...my reply...\n"
            "> **NEXT: Bohr**"
        )
        assert parse_next_token(body) == "Bohr"

    def test_only_one_next_line_regex_is_exported(self) -> None:
        # A guard against silently introducing a second parser path. If a future
        # refactor adds `_NEXT_LINE_RE_NORMALISED` next to `_NEXT_LINE_RE`, this
        # test forces the change to be conscious — the design review specifically
        # forbade duplicating the extraction loop.
        import spirrow_mindwire.conductor.handoff as handoff_mod

        next_line_regexes = [n for n in dir(handoff_mod) if "NEXT_LINE_RE" in n]
        assert len(next_line_regexes) == 1, next_line_regexes


# --------------------------------------------------------------------------- #
# The emphasis CLOSE is not always the last thing on the line.
#
# msg-1074 §2 spelled out that this bug fails twice, and that fixing one end
# leaves it broken: the line head `**` defeated `^\s*`, AND — even once the head
# was tolerated — the token was cut at whitespace so the name came out as
# `Heisenberg**`, which no roster lookup matches. (Both mechanisms named there are
# gone: there is no strip step and no name split in the module any more. The
# shapes stay pinned; the explanation is history, not a description of the code.)
#
# Tolerating the close only when it sits at end-of-line fixes `**NEXT: X**` but
# NOT the shape that actually stopped the loop, because a real handoff line
# carries a trailing gloss AFTER the closing `**`:
#
#     **NEXT: Heisenberg** — ③ fixture field-fidelity audit（…）に着手する。  # noqa: RUF003
#
# That is the verbatim line from `spirrow-voxelworld` msg-2488, the one that
# produced `exit=0 reason=no_handoff_to_human rounds=0`. msg-1074 §6-2 requires
# it to be pinned by its real text — "「直したつもり」を防ぐ唯一の機構がこれ".
# --------------------------------------------------------------------------- #


class TestEmphasisCloseFollowedByGloss:
    """The real incident shape: bold wrapper CLOSED mid-line, gloss after it."""

    def test_msg_2488_verbatim_line_resolves(self) -> None:
        # The exact body that stopped `T-slope-extension-dead-mode` on 2026-08-15.
        body = "**NEXT: Heisenberg** — ③ fixture field-fidelity audit（…）に着手する。"  # noqa: RUF001
        h = resolve_handoff(body, _ROSTER)
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Heisenberg"

    def test_bold_close_then_ascii_gloss(self) -> None:
        assert parse_next_token("**NEXT: Bohr** - please pick this up") == "Bohr"

    def test_bold_close_then_emdash_gloss(self) -> None:
        assert parse_next_token("**NEXT: Bohr** — next step") == "Bohr"

    def test_sentinel_with_close_then_gloss(self) -> None:
        h = resolve_handoff("**NEXT: human** — Tier-C の判断が要る", _ROSTER)
        assert h.kind is HandoffKind.HUMAN

    def test_inline_code_close_then_gloss(self) -> None:
        assert parse_next_token("`NEXT: Einstein` — independent review please") == "Einstein"

    def test_emphasis_on_the_name_only(self) -> None:
        # The wrapper need not surround `NEXT:` — an author may bold just the name.
        assert parse_next_token("NEXT: **Heisenberg** — go") == "Heisenberg"

    def test_pr_review_ref_does_not_absorb_the_closing_wrapper(self) -> None:
        # A gloss after `**` used to leave the ref as `acme/widgets#7**`.
        #
        # CORRECTION to this test's earlier comment (and to msg-1128 §5-3 / msg-1129 §1,
        # which both inherited the claim): that token is NOT "handed to GitHub as an
        # invalid ref". `Conductor` re-parses it with `parse_pr_ref` before firing, and
        # `parse_pr_ref("acme/widgets#7**")` returns the slug `acme/widgets#7` — measured,
        # not reasoned. So the Tier-B gate was never actually broken by this; what the
        # dirty token really costs is a `Handoff.token` that does not mean what its
        # docstring says and a second component silently repairing the first. It is
        # pinned clean here because the ref is *matched*, not salvaged downstream.
        h = resolve_handoff("**NEXT: pr-review acme/widgets#7** — please gate", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"


class TestPayloadCharactersAreInsideTheRefPattern:
    """`_` and `#` are matched as part of the ref, so no rule has to spare them."""

    def test_underscore_inside_a_repo_name_survives(self) -> None:
        # `_` is a Markdown emphasis marker AND a legal character in a repo name / URL.
        # Under a strip-based parser that collision needs a word-edge rule to arbitrate;
        # under a match-based one the `_` is simply inside the ref pattern.
        h = resolve_handoff("**NEXT: pr-review acme/my_repo#7**", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/my_repo#7"

    def test_underscored_url_ref_survives(self) -> None:
        # The `_` inside `my_repo` is payload and survives; the surrounding `**` and the gloss do
        # not reach the ref. CHANGED with the delegation: the token is the owner's canonical slug
        # (see `test_resolve_pr_review_accepts_a_url_ref`), so what this pins is that the
        # underscore reached the owner intact — `acme/my_repo#7`, not `acme/my`.
        h = resolve_handoff(
            "**NEXT: pr-review https://github.com/acme/my_repo/pull/7** — gate it",
            _ROSTER,
        )
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/my_repo#7"

    def test_bare_forms_are_untouched(self) -> None:
        # msg-1074 §6-3: prove the pre-existing plain shapes still work.
        assert parse_next_token("NEXT: Heisenberg") == "Heisenberg"
        assert parse_next_token("NEXT: Heisenberg — do the thing") == "Heisenberg"
        assert parse_next_token("NEXT: Heisenberg（実装を進める）") == "Heisenberg"  # noqa: RUF001
        assert resolve_handoff("NEXT: none", _ROSTER).kind is HandoffKind.NONE


# --------------------------------------------------------------------------- #
# Round 2's escape: punctuation attached to the closing wrapper.
#
# `c4c66c2` tolerated the emphasis close only where a whitespace followed it, so
# every shape where an LLM writes ordinary punctuation against the `**` fell out
# as ABSENT — and on the `pr-review` route it did not even fall out, it carried
# `acme/widgets#7**` forward. Both were measured on that commit before this
# rewrite (msg-1128 §5); the table below is that measurement, inverted.
#
# These are pinned individually rather than left to the corpus because they are
# the specific escapes of the previous two rounds. The corpus is what protects
# against the NEXT escape, which by definition is not in this list.
# --------------------------------------------------------------------------- #


class TestPunctuationAttachedToTheClosingWrapper:
    """msg-1128 §5: every shape that fell through when the close needed whitespace."""

    def test_comma_against_the_close(self) -> None:
        assert parse_next_token("**NEXT: Bohr**, because...") == "Bohr"

    def test_full_stop_against_the_close(self) -> None:
        assert parse_next_token("**NEXT: Heisenberg**.") == "Heisenberg"

    def test_colon_against_the_close(self) -> None:
        assert parse_next_token("**NEXT: Einstein**: wait") == "Einstein"

    def test_fullwidth_stop_against_the_close(self) -> None:
        h = resolve_handoff("**NEXT: human**。", _ROSTER)
        assert h.kind is HandoffKind.HUMAN

    def test_unspaced_emdash_against_the_close(self) -> None:
        # The naysayer's own example of what the spaced `** — ` gloss test masked.
        assert parse_next_token("**NEXT: Bohr**— next step") == "Bohr"

    def test_fullwidth_paren_against_the_close(self) -> None:
        h = resolve_handoff("**NEXT: Bohr**）", _ROSTER)  # noqa: RUF001
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Bohr"

    def test_pr_review_ref_with_punctuation_against_the_close(self) -> None:
        # The route that did NOT fail loudly: it produced a PR_REVIEW whose token
        # carried the wrapper. Three shapes, one expectation.
        for body in (
            "**NEXT: pr-review acme/widgets#7**, please gate",
            "**NEXT: pr-review acme/widgets#7**.",
            "**NEXT: pr-review acme/widgets#7**（gate してほしい）",  # noqa: RUF001
        ):
            h = resolve_handoff(body, _ROSTER)
            assert h.kind is HandoffKind.PR_REVIEW, body
            assert h.token == "acme/widgets#7", body


class TestDecorationBeyondAnyEnumeratedSet:
    """The leading rule is a property, not a list — shapes nobody enumerated work."""

    def test_arrow_prefix_from_real_traffic(self) -> None:
        # `spirrow-mindwire` msg-494, a real handoff by the proposer. No round of this
        # thread listed `→` among the shells to tolerate, and an enumeration never
        # would have: the point is that it does not need to be listed.
        h = resolve_handoff(
            "→ **NEXT: human**(Takahito: ① 256K で override-merge / ② 別値、を判断)。",
            _ROSTER,
        )
        assert h.kind is HandoffKind.HUMAN

    def test_table_row(self) -> None:
        # msg-1076 listed `| NEXT: X |` as a shape the enumeration did not cover.
        assert parse_next_token("body\n\n| NEXT: Bohr |") == "Bohr"

    def test_ordered_list_item(self) -> None:
        # The one word-character allowance in the leading rule.
        assert parse_next_token("body\n\n1. NEXT: Einstein") == "Einstein"
        assert parse_next_token("body\n\n2) NEXT: Einstein") == "Einstein"

    def test_fullwidth_colon_and_spaced_colon(self) -> None:
        # Required by the revised DoD (msg-1078). Recorded honestly: unlike every other
        # shape here, neither appears even once in the real-traffic corpus — this pair
        # is carried on the DoD's authority, not on evidence.
        assert parse_next_token("NEXT： Bohr") == "Bohr"  # noqa: RUF001
        assert parse_next_token("NEXT : Bohr") == "Bohr"

    def test_a_sentence_that_merely_mentions_the_keyword_is_not_a_handoff(self) -> None:
        # The other side of the same rule, and the reason it is stated as "no word
        # characters before the keyword" rather than "anything before the keyword".
        # Both are real critique lines from the corpus.
        assert parse_next_token("if the human explicitly writes `NEXT: human`, then") is None
        assert parse_next_token("スレは引き続き Bohr の番。`NEXT: Bohr` と書けばよい") is None

    def test_capitalised_next_heading_is_not_a_handoff(self) -> None:
        # `**Next:** PIE A/B (...)` is a real line — a section heading meaning "next
        # steps", not a handoff. Matching the keyword case-insensitively would take it.
        assert parse_next_token("**Next:** PIE A/B (launch with the flag)") is None


class TestKnownResidualMidLineHandoffs:
    """Characterisation, NOT an endorsement: a handoff written mid-sentence is lost.

    Ten real handoffs in the corpus end a prose line with the directive
    (``…私の実装側タスクは完了。NEXT: Bohr``). They resolve to ABSENT — the leading rule
    requires that nothing but decoration precede the keyword, and a sentence is not
    decoration. Widening it is NOT in this PR's scope and is not obviously right: the
    same corpus holds ~1,000 critique lines that quote the directive mid-sentence, and
    several of those are the LAST such line in their message, so a mid-line rule would
    dispatch on quoted text. Reported to the proposer with the ten lines rather than
    decided here. These assertions exist so the residual is visible in the suite
    instead of being discovered a third time.
    """

    def test_mid_line_handoff_is_absent(self) -> None:
        for body in (
            "スレは引き続き Bohr の番（awaiting_reply）。NEXT: Bohr",  # noqa: RUF001
            "c2 default-flip cycle is COMPLETE and SHIPPED. NEXT: none (thread settled).",
            "capability テストが誤前提 revert を防いだ ＝ 判断が正しかった。NEXT: human",  # noqa: RUF001
        ):
            assert resolve_handoff(body, _ROSTER).kind is HandoffKind.ABSENT, body


# --------------------------------------------------------------------------- #
# The real-traffic corpus (msg-1129 §4).
#
# Rounds 1 and 2 both chose their test shapes by imagining them, and both times the
# shape that actually broke was the neighbour of the one pinned. So the shapes here
# are not chosen at all: `tests/data/next_line_corpus.tsv` is every `NEXT:`-bearing
# line of every message in the live `spirrow-mindwire` + `spirrow-voxelworld`
# chatrooms (~3.5k lines), cut to its decision head and de-duplicated by
# `scripts/gen_next_line_corpus.py`. Both the cut and the de-duplication are
# mechanical, and the generator verifies the cut cannot change an answer.
#
# It is a characterisation corpus: prose lines are recorded as ABSENT on purpose.
# Asserting only that handoffs resolve would have missed the false positive this
# corpus actually caught — a quoted code comment, `# NEXT: pr-review <owner/repo#n>`,
# which the previous parser reported as a PR-gate directive.
# --------------------------------------------------------------------------- #


class TestRealTrafficCorpus:
    """Every distinct NEXT-line shape the two live chatrooms have ever produced."""

    @staticmethod
    def _records() -> list[tuple[str, str, str]]:
        path = Path(__file__).parent / "data" / "next_line_corpus.tsv"
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            kind, token, text = line.split("\t", 2)
            records.append((kind, token, text))
        return records

    def test_every_real_line_resolves_as_recorded(self) -> None:
        mismatches = []
        for kind, token, text in self._records():
            handoff = resolve_handoff(text, _ROSTER)
            actual_token = handoff.token if handoff.kind is not HandoffKind.ABSENT else None
            if (handoff.kind.value, actual_token or "") != (kind, token):
                mismatches.append((text, (kind, token), (handoff.kind.value, actual_token)))
        assert not mismatches, mismatches[:10]

    def test_the_corpus_is_actually_populated(self) -> None:
        # Guards the assertion above against quietly becoming vacuous — an empty or
        # truncated fixture would make it pass. The counts are floors, not equalities,
        # so regenerating against a busier chatroom does not fail the suite.
        records = self._records()
        kinds = Counter(kind for kind, _, _ in records)
        assert len(records) > 500, len(records)
        assert kinds["role"] > 100, kinds
        assert kinds["human"] > 20, kinds
        assert kinds["none"] > 10, kinds
        assert kinds["pr_review"] > 5, kinds
        assert kinds["absent"] > 100, kinds  # the prose side, which must NOT resolve

    def test_the_corpus_contains_the_shape_that_stopped_the_loop(self) -> None:
        # msg-1074 §6-2 wants the real incident string pinned, and it is (verbatim, in
        # TestEmphasisCloseFollowedByGloss). This checks the corpus independently
        # carries that shape, i.e. that the harvest really reached the traffic rather
        # than silently collecting an empty or unrelated slice.
        texts = [text for _, _, text in self._records()]
        assert any(t.startswith("**NEXT: Heisenberg**") for t in texts)
        assert any(t.startswith("→ **NEXT: human**") for t in texts)
