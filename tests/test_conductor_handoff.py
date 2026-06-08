"""Tests for the conductor NEXT-handoff parser + resolver (msg-520 / msg-522 Obj3)."""

from __future__ import annotations

from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    Handoff,
    HandoffKind,
    build_handoff_protocol_block,
    parse_next_token,
    resolve_handoff,
)
from spirrow_mindwire.value_objects import Role

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}


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
