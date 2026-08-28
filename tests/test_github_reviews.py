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

from spirrow_mindwire.github.client import ReviewInfo
from spirrow_mindwire.github.reviews import LandedState, landed

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
