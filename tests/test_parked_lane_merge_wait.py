"""A PR-gate merge-wait relay is filed in the merge-wait lane, not as a decision (msg-4361).

Takahito: a merge into ``main`` is requested by opening the PR and carried by the merge-wait PR
list, so it must not appear on the to-do board as a Tier-C decision.
"""

from __future__ import annotations

from spirrow_mindwire.conductor.gate_records import (
    MERGE_WAIT_LINE,
    is_merge_wait_relay,
    render_relay_heading,
)
from spirrow_mindwire.conductor.parked_lane import ParkedLane, classify_parked


def _relay(tail: str) -> str:
    return (
        f"{render_relay_heading('o/r#7', 'abc1234')}\n\n"
        "VERDICT: APPROVE (ci=success)\n\nLGTM\n\n"
        f"{tail}"
    )


def test_merge_wait_relay_is_its_own_lane() -> None:
    body = _relay(f"{MERGE_WAIT_LINE}\nNEXT: human")
    assert is_merge_wait_relay(body)
    assert classify_parked(body).lane is ParkedLane.MERGE_WAIT


def test_a_plain_human_park_stays_a_decision() -> None:
    body = "design question\n\nTIER-C: goal\nNEXT: human"
    assert not is_merge_wait_relay(body)
    assert classify_parked(body).lane is ParkedLane.DECISION


def test_an_inline_mention_is_not_a_merge_wait() -> None:
    # Only a column-zero line counts; quoting the mark inside prose does not.
    body = "the relay said `MERGE-WAIT:` earlier\n\nTIER-C: cost\nNEXT: human"
    assert not is_merge_wait_relay(body)
    assert classify_parked(body).lane is ParkedLane.DECISION
