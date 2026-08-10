"""Tests for the conductor NEXT-handoff parser + resolver (msg-520 / msg-522 Obj3)."""

from __future__ import annotations

from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    Handoff,
    HandoffKind,
    TierCMarker,
    TierCReason,
    build_handoff_protocol_block,
    parse_next_token,
    parse_tier_c_marker,
    resolve_handoff,
)
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
    url = "https://github.com/acme/widgets/pull/7"
    h = resolve_handoff(f"done\n\nNEXT: pr-review {url}", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == url


def test_resolve_bare_pr_review_without_ref_falls_through_to_absent() -> None:
    # "pr-review" with no ref is not the PR-gate sentinel (the regex needs a ref); it falls through
    # to a roster lookup, which fails → ABSENT (route to human), not a half-formed gate fire.
    h = resolve_handoff("oops\n\nNEXT: pr-review", _ROSTER)
    assert h.kind is HandoffKind.ABSENT


def test_resolve_pr_review_strips_trailing_gloss() -> None:
    # LLMs often append a parenthetical gloss; the ref is the single non-whitespace token, not the
    # whole remainder of the line — else an invalid ref reaches GitHub (Tier B PR #103 round 2).
    h = resolve_handoff("done\n\nNEXT: pr-review acme/widgets#7 (please review)", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "acme/widgets#7"


def test_resolve_pr_review_strips_trailing_punctuation() -> None:
    # A ref directly followed by sentence punctuation (no space) must still resolve cleanly, else an
    # invalid ``...#7.`` / ``...#7,`` reaches GitHub (Tier B PR #103 round 3).
    dot = resolve_handoff("a\n\nNEXT: pr-review acme/widgets#7.", _ROSTER)
    assert dot.kind is HandoffKind.PR_REVIEW
    assert dot.token == "acme/widgets#7"
    comma = resolve_handoff("b\n\nNEXT: pr-review acme/widgets#7, please review now", _ROSTER)
    assert comma.token == "acme/widgets#7"


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


def test_implementer_block_hands_back_to_proposer_and_never_merges() -> None:
    block = build_handoff_protocol_block(Role.IMPLEMENTER)
    assert "proposer" in block
    assert "never merge" in block


def test_naysayer_block_is_advisory() -> None:
    block = build_handoff_protocol_block(Role.NAYSAYER)
    assert "advisory, not a veto" in block


def test_implementer_block_names_the_tier_c_marker_convention() -> None:
    # T-human-terminal-overuse: the prompt-side half of the design. The block must (a)
    # narrow when ``NEXT: human`` is warranted and (b) name the ``TIER-C: <reason>`` marker
    # convention with the same enum values the parser accepts, so a cooperating implementer
    # produces the marker the parser measures. Emit and parse read the SAME enum
    # (_TIER_C_FIXED_REASONS): this pin catches drift between them.
    block = build_handoff_protocol_block(Role.IMPLEMENTER)
    assert "TIER-C:" in block
    for reason in TierCReason:
        if reason is TierCReason.OTHER:
            assert "other:" in block  # OTHER carries a ``:<why>`` suffix
        else:
            assert reason.value in block
    # The intent-narrowing sentence pair: in-loop uncertainty routes to the proposer.
    assert "In-loop uncertainty" in block
    assert "proposer" in block


# --------------------------------------------------------------------------- #
# parse_tier_c_marker — non-blocking observation (T-human-terminal-overuse §7)
# --------------------------------------------------------------------------- #


def test_parse_tier_c_marker_absent_when_no_next_human() -> None:
    # No ``NEXT: human`` line → None (not "missing marker"; the observation doesn't apply).
    assert parse_tier_c_marker("just a design reply\n\nNEXT: Bohr") is None
    assert parse_tier_c_marker("no handoff at all") is None


def test_parse_tier_c_marker_missing_when_no_preceding_line() -> None:
    # ``NEXT: human`` is the very first line → no n-1 exists → None (= missing).
    assert parse_tier_c_marker("NEXT: human") is None


def test_parse_tier_c_marker_missing_when_preceding_line_is_prose() -> None:
    body = "some conclusion the implementer wrote\n\nNEXT: human"
    # Blank line separates the prose from ``NEXT: human``, so n-1 is the blank — no marker.
    assert parse_tier_c_marker(body) is None


def test_parse_tier_c_marker_missing_when_blank_line_separates_marker() -> None:
    # Strict n-1: a blank line between the marker and ``NEXT: human`` breaks the match
    # (design choice per Einstein §7-5 — a wider window would let stray TIER-C mentions
    # earlier in the reply score, defeating the count).
    body = "reason...\n\nTIER-C: merge-protected\n\nNEXT: human"
    assert parse_tier_c_marker(body) is None


def test_parse_tier_c_marker_recognises_every_fixed_reason() -> None:
    for reason in (
        TierCReason.IRREVERSIBLE,
        TierCReason.BILLING,
        TierCReason.SCOPE,
        TierCReason.MERGE_PROTECTED,
        TierCReason.RELEASE_CROSS_REPO,
    ):
        body = f"work is done\nTIER-C: {reason.value}\nNEXT: human"
        marker = parse_tier_c_marker(body)
        assert marker == TierCMarker(reason=reason, detail=None)


def test_parse_tier_c_marker_other_captures_free_text_detail() -> None:
    body = (
        "unexpected environment failure\nTIER-C: other:host clock skew locks the sweep\nNEXT: human"
    )
    marker = parse_tier_c_marker(body)
    assert marker == TierCMarker(reason=TierCReason.OTHER, detail="host clock skew locks the sweep")


def test_parse_tier_c_marker_other_with_empty_detail() -> None:
    # ``other:`` with nothing after is still a match — the enum's escape hatch. Detail is
    # None (not the empty string) so callers do not have to guard on ``if detail``.
    body = "prose\nTIER-C: other:\nNEXT: human"
    marker = parse_tier_c_marker(body)
    assert marker == TierCMarker(reason=TierCReason.OTHER, detail=None)


def test_parse_tier_c_marker_rejects_unknown_reason() -> None:
    # A reason not in the enum → None (missing), not a false-positive. Strictness is what
    # keeps the count meaningful (Einstein §7-5).
    body = "prose\nTIER-C: whatever\nNEXT: human"
    assert parse_tier_c_marker(body) is None


def test_parse_tier_c_marker_rejects_wrong_prefix() -> None:
    # Case-sensitive on the marker prefix on purpose (a lowercase ``tier-c:`` would
    # under-count into MISSING, which is the right side to err on).
    body = "prose\ntier-c: scope\nNEXT: human"
    assert parse_tier_c_marker(body) is None


def test_parse_tier_c_marker_last_next_human_wins() -> None:
    # Same "last wins" rule as resolve_handoff: an earlier quoted ``NEXT: human`` (e.g.
    # inside a relay) must not defeat the marker check against the author's final handoff.
    body = (
        "quoting a review:\n"
        "TIER-C: irreversible\n"
        "NEXT: human\n"
        "\n"
        "my reply:\n"
        "TIER-C: merge-protected\n"
        "NEXT: human"
    )
    marker = parse_tier_c_marker(body)
    assert marker is not None
    assert marker.reason is TierCReason.MERGE_PROTECTED


def test_parse_tier_c_marker_tolerates_leading_whitespace() -> None:
    # Indentation on either line does not break the match (real messages often carry
    # trailing spaces or accidental leading tabs from markdown editors).
    body = "prose\n  TIER-C: billing  \n  NEXT: human  "
    marker = parse_tier_c_marker(body)
    assert marker is not None
    assert marker.reason is TierCReason.BILLING


def test_parse_tier_c_marker_ignores_quoted_next_human_in_body_position() -> None:
    # A ``NEXT: Bohr`` last-handoff overrides a preceding ``NEXT: human`` — the observation
    # doesn't apply because the routing verdict is not HUMAN.
    body = "TIER-C: scope\nNEXT: human\n\nactually, on reflection:\nNEXT: Bohr"
    assert parse_tier_c_marker(body) is None
