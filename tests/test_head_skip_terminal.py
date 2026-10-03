"""Stage 1b — a terminal conductor outcome parks a thread until its head moves (design §6.2).

The failure being fixed: ``no_progress_to_human`` left no mark, so the next tick fell to Stage 2,
which is a backoff on the launch *rate* and by design never terminates. Two threads sat at one
launch per hour for days (72 retries each). These tests pin the two halves of the fix — it does
park (c), and it does NOT park forever (d) — plus the two ways the state could be lost by
accident, which is how a park quietly stops working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.conductor.core import StopReason
from spirrow_mindwire.conductor.head_skip import (
    TERMINAL_STOP_REASONS,
    Decision,
    Record,
    commit_launch,
    commit_observation,
    commit_terminal,
    decide,
    record_from_json,
    record_to_json,
)

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
_BODY = "F-1 起票\n\nNEXT: Bohr"


def _launched_record(*, head: str = "msg-244", attempts: int = 7) -> Record:
    """A thread that has been launched and got nowhere — the pre-park state."""
    return Record(
        last_launch_at=_NOW - timedelta(hours=2),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch=head,
        launch_attempts=attempts,
        head_observed_at=_NOW - timedelta(hours=2),
        last_observed_head_msg_id=head,
        last_observed_nomination="bohr",
    )


def test_terminal_reasons_are_real_stop_reasons() -> None:
    """The pointer, not the wording: head_skip names these as strings to avoid importing the
    conductor (and its MCP / GitHub dependencies) into the sweep CLI. This is what keeps the
    two spellings from drifting apart."""
    values = {reason.value for reason in StopReason}
    assert values >= TERMINAL_STOP_REASONS
    assert StopReason.NO_PROGRESS.value in TERMINAL_STOP_REASONS
    assert StopReason.SELF_HANDOFF.value in TERMINAL_STOP_REASONS
    assert StopReason.NO_HANDOFF.value in TERMINAL_STOP_REASONS
    # Not everything human-terminal parks: an explicit `NEXT: human` already parks through Stage
    # 1's stop token, and CI_WAIT / HOLD are waiting on something that WILL change on its own.
    assert StopReason.HUMAN.value not in TERMINAL_STOP_REASONS
    assert StopReason.CI_WAIT.value not in TERMINAL_STOP_REASONS


@pytest.mark.parametrize("reason", sorted(TERMINAL_STOP_REASONS))
def test_c_a_terminal_stop_parks_the_thread_on_an_unchanged_head(reason: str) -> None:
    """(c) After a terminal stop, the same head is SKIPped — not DEFERred, not launched."""
    record = commit_terminal(reason=reason, head_msg_id="msg-244", record=_launched_record())

    verdict = decide(
        now=_NOW, head_msg_id="msg-244", head_body=_BODY, control_state="run", record=record
    )

    assert verdict.decision is Decision.SKIP
    assert verdict.reason == f"terminal-stop:{reason}"
    # The counter survives: it is the audit trail of how long the spin ran (design §6.2), it is
    # simply no longer the input to a retry.
    assert verdict.attempts_before == 7
    assert verdict.attempts_after == 7
    assert verdict.delay == timedelta(0)


def test_c_the_park_does_not_expire_with_time() -> None:
    """The whole point: no amount of waiting makes an unchanged head worth running again."""
    record = commit_terminal(
        reason="no_progress_to_human", head_msg_id="msg-244", record=_launched_record()
    )

    for days in (1, 7, 90):
        verdict = decide(
            now=_NOW + timedelta(days=days),
            head_msg_id="msg-244",
            head_body=_BODY,
            control_state="run",
            record=record,
        )
        assert verdict.decision is Decision.SKIP, f"unparked itself after {days}d"


def test_d_a_moved_head_releases_the_park() -> None:
    """(d) Any new message is new input — the thread launches again immediately."""
    record = commit_terminal(
        reason="no_progress_to_human", head_msg_id="msg-244", record=_launched_record()
    )

    verdict = decide(
        now=_NOW, head_msg_id="msg-245", head_body=_BODY, control_state="run", record=record
    )

    assert verdict.decision is Decision.LAUNCH
    assert verdict.eligible_at == _NOW, "eligible now, not after another wait"
    # Note what this does NOT claim. Releasing the park hands the thread back to Stage 2 with its
    # backoff counter intact (§6.2 keeps the counter as the audit trail), so a reply that arrives
    # while the old backoff window is still open waits it out. That is pre-existing Stage 2
    # behaviour and is left alone: the park's job is to stop the retry loop, not to re-tune the
    # retry rate. In practice a parked thread has been idle for far longer than CAP by the time
    # anyone replies, which is why the delay has already elapsed here.
    assert verdict.reason == "backoff-elapsed"


def test_d_release_happens_even_when_the_nomination_is_identical() -> None:
    """The release key is the msg id, not the token — a reply that re-nominates Bohr still
    releases, because the thread now contains something the last run never saw.

    This is the one place head msg-id equality is load-bearing, and it is a different question
    from the one :func:`decide`'s progression check asks (see the Record docstring).
    """
    record = commit_terminal(
        reason="self_handoff_to_human", head_msg_id="msg-2775", record=_launched_record()
    )

    verdict = decide(
        now=_NOW,
        head_msg_id="msg-2776",
        head_body="human reply\n\nNEXT: Bohr",
        control_state="run",
        record=record,
    )

    assert verdict.decision is Decision.LAUNCH


def test_a_non_terminal_reason_clears_the_park() -> None:
    """The wrapper calls commit-terminal unconditionally; the reason table lives here."""
    parked = commit_terminal(
        reason="no_progress_to_human", head_msg_id="msg-244", record=_launched_record()
    )
    cleared = commit_terminal(reason="human", head_msg_id="msg-244", record=parked)

    assert cleared.terminal_stop_reason == ""
    assert cleared.terminal_head_msg_id == ""
    assert cleared.launch_attempts == 7, "clearing the park must not clear the audit trail"


def test_an_observation_does_not_erase_the_park() -> None:
    """The regression this guards: ``commit_observation`` rebuilds the Record field by field,
    and it is called on exactly the SKIP that the park produces. Dropping the two fields there
    would un-park the thread on the very next tick."""
    parked = commit_terminal(
        reason="no_progress_to_human", head_msg_id="msg-244", record=_launched_record()
    )

    observed = commit_observation(now=_NOW, head_msg_id="msg-244", token="bohr", record=parked)

    assert observed.terminal_stop_reason == "no_progress_to_human"
    assert observed.terminal_head_msg_id == "msg-244"
    assert (
        decide(
            now=_NOW,
            head_msg_id="msg-244",
            head_body=_BODY,
            control_state="run",
            record=observed,
        ).decision
        is Decision.SKIP
    )


def test_a_launch_clears_the_park() -> None:
    """A LAUNCH means the head moved off whatever we terminated on; keeping the old terminal
    head would make the NEXT run's outcome ambiguous."""
    parked = commit_terminal(
        reason="no_progress_to_human", head_msg_id="msg-244", record=_launched_record()
    )
    verdict = decide(
        now=_NOW, head_msg_id="msg-245", head_body=_BODY, control_state="run", record=parked
    )

    launched = commit_launch(
        now=_NOW,
        head_msg_id="msg-245",
        verdict=verdict,
        control_state="run",
        prior_record=parked,
    )

    assert launched.terminal_stop_reason == ""
    assert launched.terminal_head_msg_id == ""


def test_the_park_survives_the_state_file_round_trip() -> None:
    parked = commit_terminal(
        reason="self_handoff_to_human", head_msg_id="msg-2775", record=_launched_record()
    )
    assert record_from_json(record_to_json(parked)) == parked


def test_a_record_written_before_this_change_reads_as_unparked() -> None:
    """Forward compatibility: an old state file has neither field and must not park anything."""
    old = record_to_json(_launched_record())
    del old["terminal_stop_reason"]
    del old["terminal_head_msg_id"]

    record = record_from_json(old)

    assert record is not None
    assert record.terminal_stop_reason == ""
    assert (
        decide(
            now=_NOW, head_msg_id="msg-244", head_body=_BODY, control_state="run", record=record
        ).decision
        is not Decision.SKIP
    )


def test_a_stop_token_head_still_skips_through_stage_one() -> None:
    """Stage 1b is additive: the closed stop-token set keeps its own reason string."""
    parked = commit_terminal(
        reason="no_progress_to_human", head_msg_id="msg-244", record=_launched_record()
    )

    verdict = decide(
        now=_NOW,
        head_msg_id="msg-300",
        head_body="parked\n\nNEXT: human",
        control_state="run",
        record=parked,
    )

    assert verdict.decision is Decision.SKIP
    assert verdict.reason == "stop-token"


def test_an_empty_terminal_head_never_parks() -> None:
    """A run whose last_msg could not be parsed must not park the thread on the empty string —
    that would match nothing and park forever if the comparison were ever loosened."""
    record = commit_terminal(
        reason="no_progress_to_human", head_msg_id="", record=_launched_record()
    )

    assert record.terminal_head_msg_id == ""
    assert (
        decide(
            now=_NOW, head_msg_id="", head_body=_BODY, control_state="run", record=record
        ).decision
        is not Decision.SKIP
    )


# --------------------------------------------------------------------------- #
# T-no-field-no-next-handoff-silent-park (msg-6187 §4 (a)-(f)): a head with no ``NEXT:`` line and
# no handoff field ends on NO_HANDOFF. It is now terminal, and an edit that changes the token
# releases any terminal park.
# --------------------------------------------------------------------------- #

_NO_NEXT_BODY = "普通のコメントです。NEXT 行はありません。"
_NO_HANDOFF = StopReason.NO_HANDOFF.value


def _no_handoff_parked(head: str = "msg-6090") -> Record:
    """A human ``NEXT:``-less comment: launched once on token ``""``, ended on NO_HANDOFF."""
    first = decide(
        now=_NOW - timedelta(hours=2),
        head_msg_id=head,
        head_body=_NO_NEXT_BODY,
        control_state="run",
        record=None,
    )
    assert first.decision is Decision.LAUNCH
    assert first.token == ""
    launched = commit_launch(
        now=_NOW - timedelta(hours=2),
        head_msg_id=head,
        verdict=first,
        control_state="run",
    )
    assert launched.nomination_at_launch == ""
    return commit_terminal(reason=_NO_HANDOFF, head_msg_id=head, record=launched)


def test_nh_a_same_head_same_token_is_parked() -> None:
    """(a) / (e) Same head, token still ``""`` → SKIP, no re-LAUNCH loop."""
    record = _no_handoff_parked()
    for hours in (1, 24, 24 * 30):
        verdict = decide(
            now=_NOW + timedelta(hours=hours),
            head_msg_id="msg-6090",
            head_body=_NO_NEXT_BODY,
            control_state="run",
            record=record,
        )
        assert verdict.decision is Decision.SKIP
        assert verdict.reason == "terminal-stop:no_handoff_to_human"


def test_nh_a_control_state_change_does_not_release() -> None:
    """The ABSENT branch reads no control state, so hold→run is not a reason to re-run."""
    record = _no_handoff_parked()
    verdict = decide(
        now=_NOW,
        head_msg_id="msg-6090",
        head_body=_NO_NEXT_BODY,
        control_state="hold",
        record=record,
    )
    assert verdict.decision is Decision.SKIP


def test_nh_b_moved_head_launches_and_launch_clears_terminal() -> None:
    """(b) A new message (even another ``NEXT:``-less one) moves the head → LAUNCH; the LAUNCH's
    commit clears the terminal fields."""
    record = _no_handoff_parked()
    verdict = decide(
        now=_NOW,
        head_msg_id="msg-6091",
        head_body=_NO_NEXT_BODY,
        control_state="run",
        record=record,
    )
    assert verdict.decision is Decision.LAUNCH
    relaunched = commit_launch(
        now=_NOW,
        head_msg_id="msg-6091",
        verdict=verdict,
        control_state="run",
        prior_record=record,
    )
    assert relaunched.terminal_stop_reason == ""
    assert relaunched.terminal_head_msg_id == ""


def test_nh_c_observation_after_terminal_keeps_the_park() -> None:
    """(c) The SKIP tick's observation refresh must not erase the terminal state."""
    record = _no_handoff_parked()
    observed = commit_observation(now=_NOW, head_msg_id="msg-6090", token="", record=record)
    assert observed.terminal_stop_reason == _NO_HANDOFF
    assert observed.terminal_head_msg_id == "msg-6090"
    verdict = decide(
        now=_NOW + timedelta(minutes=5),
        head_msg_id="msg-6090",
        head_body=_NO_NEXT_BODY,
        control_state="run",
        record=observed,
    )
    assert verdict.decision is Decision.SKIP


def test_nh_d_edit_adding_next_role_launches() -> None:
    """(d) Same msg id, body edited to add ``NEXT: Bohr`` (token ``""`` → ``"bohr"``) → LAUNCH
    with ``progressed=True``, not a permanent park."""
    record = _no_handoff_parked()
    verdict = decide(
        now=_NOW,
        head_msg_id="msg-6090",
        head_body=_NO_NEXT_BODY + "\n\nNEXT: Bohr",
        control_state="run",
        record=record,
    )
    assert verdict.decision is Decision.LAUNCH
    assert verdict.progressed is True
    assert verdict.token == "bohr"


@pytest.mark.parametrize("reason", sorted(TERMINAL_STOP_REASONS))
def test_nh_f_a_token_changing_edit_releases_every_terminal_reason(reason: str) -> None:
    """(f) The edit rule is uniform: no reason-specific exception in Stage 1b."""
    record = commit_terminal(reason=reason, head_msg_id="msg-244", record=_launched_record())
    verdict = decide(
        now=_NOW,
        head_msg_id="msg-244",
        head_body="F-1 起票\n\nNEXT: Einstein",
        control_state="run",
        record=record,
    )
    assert verdict.decision is Decision.LAUNCH
    assert verdict.progressed is True


@pytest.mark.parametrize("reason", sorted(TERMINAL_STOP_REASONS))
def test_nh_g_a_failed_fetch_does_not_release_the_park(reason: str) -> None:
    """A failed body fetch makes the CLI pass an empty body (token ``""``). That is not an edit.
    Releasing on it would turn every fetch failure on a parked ``NEXT: Bohr`` head into a paid
    LAUNCH, so an empty token never passes Stage 1b."""
    record = commit_terminal(reason=reason, head_msg_id="msg-244", record=_launched_record())
    verdict = decide(
        now=_NOW, head_msg_id="msg-244", head_body="", control_state="run", record=record
    )
    assert verdict.decision is Decision.SKIP
    assert verdict.reason == f"terminal-stop:{reason}"


def test_nh_h_no_launch_baseline_keeps_the_park() -> None:
    """A terminal record with no launch baseline has no launch token to compare, so a non-empty
    token is not evidence of an edit and the park holds."""
    record = commit_terminal(reason=_NO_HANDOFF, head_msg_id="m7", record=None)
    verdict = decide(
        now=_NOW, head_msg_id="m7", head_body="NEXT: Bohr", control_state="run", record=record
    )
    assert verdict.decision is Decision.SKIP
    assert verdict.reason == "terminal-stop:no_handoff_to_human"
