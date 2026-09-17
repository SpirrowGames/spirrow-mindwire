"""Tests for :func:`spirrow_mindwire.github.reviews.landed`.

DESIGN v3 §2 / T-gate-review-submit-failure-handling: the one predicate three
different call sites in the naysayer driver share (round cap / skip / replay).
The interesting invariants are:

* ``reviews is None`` (the read failed) and ``head_sha is None`` (no head to
  compare against) both collapse to :class:`LandedState.UNKNOWN` — NEVER
  ``NOT_LANDED``. Collapsing them would let the dedup guard fail open at
  exactly the moment (a terminal read failure) when a duplicate POST is most
  likely (DESIGN v3 D-7 + Q7).
* ``commit_id=None`` on a wire review is NOT a match (fail-safe: a review that
  cannot be pinned to a commit cannot discharge a specific head).
* The ``states`` argument is deliberately per-caller: the replay-discharge use
  passes the FULL state set (COMMENT included) to close the
  APPROVE→422→COMMENT loop DESIGN v3 §2 flagged.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.github.client import ReviewEvent, ReviewInfo
from spirrow_mindwire.github.reviews import (
    LandedState,
    ReviewReceipt,
    landed,
    parse_verdict_footer,
)

_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED")
_ALL_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING")


def _rev(state: str, commit_id: str | None, login: str = "spirrowgames-ops") -> ReviewInfo:
    return ReviewInfo(
        login=login,
        state=state,
        commit_id=commit_id,
        submitted_at="2026-08-29T00:00:00Z",
    )


def test_landed_returns_landed_when_matching_review_present() -> None:
    reviews = [_rev("APPROVED", "sha-abc")]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.LANDED


def test_landed_returns_not_landed_when_read_is_empty() -> None:
    result = landed([], head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.NOT_LANDED


def test_landed_returns_unknown_when_read_is_none() -> None:
    # The fail-soft fetch returned "I could not read" (D-7 hook). Distinct from
    # `[]` (asked-and-empty). The dedup guard MUST NOT proceed on UNKNOWN.
    result = landed(None, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.UNKNOWN


def test_landed_returns_unknown_when_head_sha_is_none() -> None:
    # CI-status path can produce a null head; without one, we cannot say a
    # landed review belongs to *this* head.
    result = landed(
        [_rev("APPROVED", "sha-abc")],
        head_sha=None,
        login="spirrowgames-ops",
        states=_VERDICT_STATES,
    )
    assert result is LandedState.UNKNOWN


def test_landed_ignores_review_at_different_head() -> None:
    # Before the extraction the round-cap counter did NOT enforce head_sha match;
    # the extracted predicate does, which is DESIGN v3 §2's intended sharpening.
    reviews = [_rev("APPROVED", "sha-old")]
    result = landed(reviews, head_sha="sha-new", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.NOT_LANDED


def test_landed_ignores_review_from_different_login() -> None:
    reviews = [_rev("APPROVED", "sha-abc", login="copilot")]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.NOT_LANDED


def test_landed_ignores_state_not_in_set() -> None:
    reviews = [_rev("COMMENTED", "sha-abc")]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.NOT_LANDED


def test_landed_ignores_review_without_commit_id() -> None:
    # A wire review missing `commit_id` cannot be pinned to a head → fail-safe
    # NOT_LANDED (never LANDED).
    reviews = [_rev("APPROVED", None)]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.NOT_LANDED


@pytest.mark.parametrize("state", _VERDICT_STATES)
def test_landed_matches_each_verdict_state(state: str) -> None:
    reviews = [_rev(state, "sha-abc")]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.LANDED


def test_replay_discharge_use_treats_comment_as_landed() -> None:
    # The replay-discharge call site passes _ALL_STATES so a COMMENT (from the
    # 422 fallback) counts as discharged — otherwise the driver would loop
    # APPROVE→422→COMMENT forever, per DESIGN v3 §2.
    reviews = [_rev("COMMENTED", "sha-abc")]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_ALL_STATES)
    assert result is LandedState.LANDED


def test_round_cap_use_ignores_comment_at_same_head() -> None:
    # Contrast with the replay case: the round-cap / skip call sites pass
    # _VERDICT_STATES, so a COMMENT-only prior does NOT count as a landed
    # verdict at this head (it did not spend a Gemini review).
    reviews = [_rev("COMMENTED", "sha-abc")]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.NOT_LANDED


def test_landed_finds_review_among_many() -> None:
    reviews = [
        _rev("COMMENTED", "sha-abc", login="copilot"),
        _rev("DISMISSED", "sha-old"),
        _rev("APPROVED", "sha-abc"),
        _rev("PENDING", "sha-abc"),
    ]
    result = landed(reviews, head_sha="sha-abc", login="spirrowgames-ops", states=_VERDICT_STATES)
    assert result is LandedState.LANDED


# ── Verdict footer (DESIGN v3 Q5-A: HTML comment sentinel, head_sha + event) ──
# The single generation site is
# :func:`spirrow_mindwire.naysayer.pr_review._insert_verdict_footer_before_marker`;
# these tests exercise the parser directly against literal footer strings so we
# can pin the parser's own contract independent of any helper (msg-3218 PR-gate
# review of #280: no dead-code producer next to the parser).


def test_parse_verdict_footer_roundtrip_for_each_event() -> None:
    for event in (ReviewEvent.APPROVE, ReviewEvent.REQUEST_CHANGES, ReviewEvent.COMMENT):
        body = f"x\n\n<!-- mindwire:verdict head_sha=abcdef1234567890 event={event.value} -->"
        parsed = parse_verdict_footer(body)
        assert parsed is not None
        sha, ev = parsed
        assert sha == "abcdef1234567890"
        assert ev is event


def test_parse_verdict_footer_returns_none_when_no_marker() -> None:
    # A body without the footer is not eligible for replay (a pre-footer post from
    # an older version of the driver, or an ordinary chatroom message).
    assert parse_verdict_footer("just some text\n\nVERDICT: APPROVE") is None


def test_parse_verdict_footer_returns_none_on_multiple_markers() -> None:
    # More than one footer means the invariant "exactly one footer per body"
    # is broken — fail-safe: refuse replay (DESIGN v3 §1 UNKNOWN direction).
    body = (
        "<!-- mindwire:verdict head_sha=abc1234 event=APPROVE -->\n"
        "<!-- mindwire:verdict head_sha=def5678 event=REQUEST_CHANGES -->"
    )
    assert parse_verdict_footer(body) is None


def test_parse_verdict_footer_rejects_short_sha_below_seven_chars() -> None:
    # Regex requires >=7 chars; 6 does not match.
    body = "<!-- mindwire:verdict head_sha=abc123 event=APPROVE -->"
    assert parse_verdict_footer(body) is None


def test_parse_verdict_footer_accepts_truncated_sha_at_seven_chars() -> None:
    # The debounce-skip body uses ``head[:12]``; a >=7-char sha is accepted so
    # replay can prefix-match it against the current full head_sha.
    body = "<!-- mindwire:verdict head_sha=abcdef1 event=REQUEST_CHANGES -->"
    parsed = parse_verdict_footer(body)
    assert parsed is not None
    sha, event = parsed
    assert sha == "abcdef1"
    assert event is ReviewEvent.REQUEST_CHANGES


def test_review_receipt_is_frozen_and_carries_the_four_fields() -> None:
    receipt = ReviewReceipt(
        head_sha="abcdef1234567890",
        event=ReviewEvent.APPROVE,
        chatroom_msg_id="msg-42",
        body="the body\n\n<!-- mindwire:verdict head_sha=abcdef1234567890 event=APPROVE -->",
    )
    assert receipt.head_sha == "abcdef1234567890"
    assert receipt.event is ReviewEvent.APPROVE
    assert receipt.chatroom_msg_id == "msg-42"
    with pytest.raises((AttributeError, TypeError)):
        # frozen=True dataclass → cannot mutate; the exact exception type varies
        # (dataclasses raises FrozenInstanceError, an AttributeError subclass).
        receipt.head_sha = "other"  # type: ignore[misc]
