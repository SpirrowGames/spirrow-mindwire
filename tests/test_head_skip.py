"""Tests for the head-skip nomination predicate (D3' final, Bohr msg-878).

The 14 tests here are the finalised list from the spec dispatched into the implementer:

1.  Acceptance (kept from prior review): a fresh thread with a record-less state and a role-name
    NEXT launches immediately.
2.  Stop-token closure: only ``NEXT: none`` / ``NEXT: human`` return SKIP (property-tested with
    random tokens so the closed set cannot silently expand).
3.  Fail-open on UNRESOLVED: missing / malformed / unknown-persona NEXT → LAUNCH, never SKIP; the
    parser does NOT consult a roster.
4.  Real-world head with a parenthetical gloss (``NEXT: Bohr(...but after C1 gate-1)``) parses
    to ``bohr`` and launches - the gloss is NOT interpreted (fullwidth-paren CJK exercised in
    the test body itself, below).
5.  Head-only progress (msg-id changes, nomination + control unchanged) does NOT reset the backoff
    counter; the launch schedule collapses onto the ``t=0, 15, 45, 105, 165, ...`` staircase and
    converges to 1 launch/hour at the CAP.
6.  Progressed threads (nomination changes every tick) launch with delay 0 on every tick — no
    ``MIN_GAP``-style floor exists on the progress path.
7.  Nomination change ⇒ reset attempts to 0 AND launch immediately.
8.  Control change ⇒ reset attempts to 0 AND launch immediately.
9.  A DEFER does NOT consume an attempt (attempts_after == attempts_before on DEFER).
10. LAUNCH commits a fully-populated Record; the record write is what the sweep persists BEFORE
    starting the conductor session (survives a 4H forced kill).
11. Backoff caps at CAP and never terminates in SKIP (property-tested with attempts up to 1000).
12. verdict_to_json emits every audit field required by the report-mode contract; a report-mode
    run does NOT mutate the state file (tested at the CLI layer).
13. Stage-1 stop-token judgment reads NOTHING but the token — passing a null / mock record still
    returns SKIP for a stop token (the closed-set invariant, per Einstein Q4).
14. HEAD_CACHE_TTL: after 60 min a re-parse is required even though the head msg-id is unchanged
    — this is the only automatic recovery path if the "operators post new messages rather than
    edit head" premise is broken.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.conductor.head_skip import (
    BASE,
    CAP,
    HEAD_CACHE_TTL,
    STOP_TOKENS,
    Decision,
    Record,
    Verdict,
    commit_launch,
    decide,
    needs_head_reparse,
    parse_head_token,
    record_from_json,
    record_to_json,
    verdict_to_json,
)

# --- helpers ------------------------------------------------------------------------------------

_T0 = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def _body(token: str, gloss: str = "") -> str:
    """A minimal head body with a NEXT line. ``gloss`` is appended after the token in parens."""
    line = f"NEXT: {token}"
    if gloss:
        line = f"NEXT: {token}{gloss}"
    return f"some content\nmaybe more\n\n{line}"


# --- 1. acceptance -------------------------------------------------------------------------------


def test_acceptance_launches_a_fresh_thread_with_role_next() -> None:
    """The exact acceptance case from Einstein's ACCEPT: no prior record + role-name NEXT ⇒ LAUNCH.

    The scenario reproduces the measured state ``head=msg-2242, NEXT: Bohr, control=run`` with a
    record-less state file. This is the D1 (predicate replacement) sanity check: even before any
    backoff logic engages, the very first tick on a live thread must reach LAUNCH, else the sweep
    silently parks new threads forever.
    """
    verdict = decide(
        now=_T0,
        head_msg_id="msg-2242",
        head_body=_body("Bohr"),
        control_state="run",
        record=None,
    )
    assert verdict.decision is Decision.LAUNCH
    assert verdict.reason == "no-prior-record"
    assert verdict.token == "bohr"
    assert verdict.delay == timedelta(0)


# --- 2. stop-token closure ----------------------------------------------------------------------


@pytest.mark.parametrize("token", ["none", "human", "None", "HUMAN", "  human  "])
def test_stop_tokens_return_skip(token: str) -> None:
    """Every rendering of ``none`` / ``human`` (case + whitespace variants) → SKIP."""
    verdict = decide(
        now=_T0,
        head_msg_id="msg-1",
        head_body=_body(token),
        control_state="run",
        record=None,
    )
    assert verdict.decision is Decision.SKIP
    assert verdict.reason == "stop-token"


@pytest.mark.parametrize(
    "token",
    [
        # Not in STOP_TOKENS: every one of these must NOT be SKIP.
        "bohr",
        "einstein",
        "heisenberg",
        "unknown-persona",
        "pr-review",
        "x",
        "",  # UNRESOLVED — Stage 1 does not catch it (empty is not in STOP_TOKENS)
        "humanoid",  # substring of "human" but a different token
        "nonesuch",  # substring of "none" but a different token
    ],
)
def test_only_the_two_stop_tokens_return_skip(token: str) -> None:
    """Property-style: every non-STOP_TOKENS token stays out of the SKIP branch.

    This is the closed-set invariant that fixes the failure mode this module was written for:
    the head-skip cache cannot silently swallow a live thread by mistaking its handoff for a
    stop. The set is fixed at ``{none, human}`` at import time (:data:`STOP_TOKENS`) and any
    change requires updating this test — a code refactor cannot expand the skip surface by
    accident.
    """
    verdict = decide(
        now=_T0,
        head_msg_id="msg-1",
        head_body=_body(token) if token else "no next line here",
        control_state="run",
        record=None,
    )
    assert verdict.decision is not Decision.SKIP


def test_stop_tokens_frozen_set_is_exactly_none_and_human() -> None:
    # A separate pin so a would-be expander of the closed set has to update this test AND remove
    # the invariant reason above. Frozenset equality would be brittle to reorderings; use set().
    assert set(STOP_TOKENS) == {"none", "human"}


# --- 3. fail-open on UNRESOLVED -----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "",  # empty body
        "no NEXT line here",  # missing NEXT
        "NEXT:",  # empty token
        "NEXT: !!!  ",  # unparseable (all-punctuation after strip)
        "NEXT: some-unknown-persona-not-in-any-roster",  # unknown persona
        "quoted `NEXT: Bohr` and nothing else",  # NEXT: appears but only backticked
    ],
)
def test_unresolved_next_falls_open_to_launch(body: str) -> None:
    """UNRESOLVED (empty / missing / malformed / unknown) → LAUNCH, NEVER SKIP.

    Written as multiple cases so the fail-open behaviour is visibly checked against every
    "unreadable" shape we can think of. The predicate MUST NOT consult a roster — an unknown
    persona name is treated identically to a missing NEXT: line (both LAUNCH). The failure this
    prevents is exactly the "silently park a live thread we cannot interpret" trap the module
    exists to close.
    """
    verdict = decide(
        now=_T0,
        head_msg_id="msg-1",
        head_body=body,
        control_state="run",
        record=None,
    )
    assert verdict.decision is Decision.LAUNCH
    assert verdict.reason == "no-prior-record"


# --- 4. real-world CJK gloss ---------------------------------------------------------------------


def test_real_head_with_cjk_gloss_parses_bare_name_and_launches() -> None:
    """The measured ``T-track-b-seam-octree-retirement`` head parses to ``bohr`` and LAUNCHes.

    The parenthetical gloss (CJK fullwidth parens) carries the operational instruction
    'not now' but the *token* is still ``Bohr`` - and the predicate MUST NOT try to interpret
    prose. Interpreting the gloss would collapse the parser into ad-hoc English NLP; the
    invariant it holds is 'the token is the token; the gloss is comment; the SCHEDULING is what
    backoff is for'.
    """
    body = (
        "本スレは C1 の後ろに queue します。\n\n"
        "NEXT: Bohr（ただし C1 gate-1 の verdict が出た後。いま起こすターンではない）"  # noqa: RUF001
    )
    assert parse_head_token(body) == "bohr"
    verdict = decide(
        now=_T0,
        head_msg_id="msg-1",
        head_body=body,
        control_state="run",
        record=None,
    )
    assert verdict.decision is Decision.LAUNCH


# --- 5. head-only progress: staircase to CAP ----------------------------------------------------


def test_head_only_progress_climbs_the_backoff_staircase() -> None:
    """head changes but nomination + control do NOT: backoff schedule 0, 15m, 45m, ..., capped.

    Simulates the exact degeneracy the module was written to bound. Each launch produces a new
    msg-id but no new nomination or control change. The delay between successive launches must
    follow ``BASE * 2^(n-1)``, capped at CAP, so the steady-state rate collapses to 1
    launch/hour (the CAP-defined floor).
    """
    # Prime the record with the first launch (attempts=1, so the NEXT launch's delay is BASE).
    now = _T0
    rec = commit_launch(
        now=now,
        head_msg_id="msg-1",
        verdict=Verdict(
            decision=Decision.LAUNCH,
            reason="no-prior-record",
            token="bohr",
            token_raw="Bohr",
            progressed=False,
            attempts_before=0,
            attempts_after=1,
            delay=timedelta(0),
            eligible_at=now,
        ),
        control_state="run",
    )

    # Attempt 2: delay = BASE (15 min). Head msg-id changes; nomination + control do not.
    now2 = now + BASE
    v2 = decide(
        now=now2,
        head_msg_id="msg-2",  # head moved
        head_body=_body("Bohr"),  # nomination unchanged
        control_state="run",  # control unchanged
        record=rec,
    )
    assert v2.decision is Decision.LAUNCH
    assert v2.reason == "backoff-elapsed"
    assert v2.delay == BASE
    rec = commit_launch(now=now2, head_msg_id="msg-2", verdict=v2, control_state="run")
    assert rec.launch_attempts == 2

    # Attempt 3: delay = 2*BASE = 30 min. DEFER just before, LAUNCH exactly at.
    now_defer = now2 + BASE * 2 - timedelta(seconds=1)
    v_defer = decide(
        now=now_defer,
        head_msg_id="msg-3",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v_defer.decision is Decision.DEFER
    assert v_defer.delay == BASE * 2

    now3 = now2 + BASE * 2
    v3 = decide(
        now=now3,
        head_msg_id="msg-3",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v3.decision is Decision.LAUNCH
    assert v3.delay == BASE * 2
    rec = commit_launch(now=now3, head_msg_id="msg-3", verdict=v3, control_state="run")
    assert rec.launch_attempts == 3

    # Attempt 4: delay = 4*BASE = 60 min = CAP (min() clamps).
    now4 = now3 + BASE * 4
    v4 = decide(
        now=now4,
        head_msg_id="msg-4",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v4.decision is Decision.LAUNCH
    assert v4.delay == CAP
    rec = commit_launch(now=now4, head_msg_id="msg-4", verdict=v4, control_state="run")

    # Attempt 5+: capped at CAP (steady-state 1 launch/hour).
    now5 = now4 + CAP
    v5 = decide(
        now=now5,
        head_msg_id="msg-5",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v5.decision is Decision.LAUNCH
    assert v5.delay == CAP


# --- 6. progress path has no floor --------------------------------------------------------------


def test_progressed_launches_have_zero_delay() -> None:
    """A thread whose nomination changes every tick launches with delay=0 on every tick.

    This is the explicit-fix side of removing ``MIN_GAP``: the progress path has no design-time
    floor. The bound on this path is the ENVIRONMENT (Windows scheduler IgnoreNew + 1-candidate-
    per-run + 15-25 min measured session length -> 2-4 launches/hour/thread), not the predicate.
    Pinning this test is what forces a future change that reintroduces a delay on the progress
    path to be a deliberate, visible change — the human-approved invariant "a progressing thread
    never backs off" is right here.
    """
    now = _T0
    rec = commit_launch(
        now=now,
        head_msg_id="msg-1",
        verdict=Verdict(
            decision=Decision.LAUNCH,
            reason="no-prior-record",
            token="bohr",
            token_raw="Bohr",
            progressed=False,
            attempts_before=0,
            attempts_after=1,
            delay=timedelta(0),
            eligible_at=now,
        ),
        control_state="run",
    )
    # Even ONE second after the launch, a progressed nomination should launch immediately.
    for i in range(1, 6):
        now = now + timedelta(seconds=1)
        next_token = ["einstein", "heisenberg", "bohr", "einstein", "heisenberg"][i - 1]
        v = decide(
            now=now,
            head_msg_id=f"msg-{i + 1}",
            head_body=_body(next_token),
            control_state="run",
            record=rec,
        )
        assert v.decision is Decision.LAUNCH, f"tick {i}: expected LAUNCH, got {v.decision}"
        assert v.reason == "progressed"
        assert v.delay == timedelta(0)
        assert v.progressed is True
        rec = commit_launch(now=now, head_msg_id=f"msg-{i + 1}", verdict=v, control_state="run")
        # Reset check: attempts should stay at 1 (each launch is the first of a new series).
        assert rec.launch_attempts == 1


# --- 7. nomination change resets attempts -------------------------------------------------------


def test_nomination_change_resets_attempts_and_launches() -> None:
    """A shift from ``bohr`` to ``einstein`` at attempts=5 must reset attempts to 0 and LAUNCH.

    The reset is what breaks the degeneracy: a stuck thread's counter climbs, but the moment the
    handoff actually moves to a different participant, the backoff is unwound and the sweep can
    proceed at full cadence again.
    """
    now = _T0
    rec = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=5,
        head_observed_at=now,
    )
    # Immediately after the previous launch, but with a changed nomination.
    v = decide(
        now=now + timedelta(seconds=30),
        head_msg_id="msg-2",
        head_body=_body("Einstein"),
        control_state="run",
        record=rec,
    )
    assert v.decision is Decision.LAUNCH
    assert v.progressed is True
    assert v.reason == "progressed"
    assert v.delay == timedelta(0)
    # attempts_after is the fresh series' first launch: 1.
    assert v.attempts_after == 1


# --- 8. control change resets attempts ----------------------------------------------------------


@pytest.mark.parametrize(
    ("old_control", "new_control"),
    [
        ("supervised", "run"),
        ("run", "supervised"),
        ("hold", "run"),
    ],
)
def test_control_change_resets_attempts_and_launches(old_control: str, new_control: str) -> None:
    """A change in control state — with UNCHANGED nomination — resets attempts to 0 and LAUNCHes.

    This is the second half of the ``progressed`` disjunction. The measured production failure it
    prevents: a thread at unchanged head+nomination sitting under a HOLD, then released to RUN,
    would (under a nomination-only progress test) still be treated as no-progress and delayed.
    Control changes carry routing intent — a naysayer→implementer handoff under RUN dispatches
    where under HOLD it did not — so the same NEXT under a new control state must LAUNCH.
    """
    now = _T0
    rec = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch=old_control,
        head_msg_id_at_launch="msg-1",
        launch_attempts=5,
        head_observed_at=now,
    )
    v = decide(
        now=now + timedelta(seconds=30),
        head_msg_id="msg-1",  # head UNCHANGED
        head_body=_body("Bohr"),  # nomination UNCHANGED
        control_state=new_control,
        record=rec,
    )
    assert v.decision is Decision.LAUNCH
    assert v.progressed is True
    assert v.attempts_after == 1


# --- 9. DEFER does not consume an attempt -------------------------------------------------------


def test_defer_does_not_consume_an_attempt() -> None:
    """A DEFER emits attempts_after == attempts_before.

    Contract: DEFERs are the "not yet ready" state and must not count toward the exponent, else
    the backoff would climb faster than the wall clock does — a thread checked twice per tick
    would DOUBLE its perceived attempt count. Only a LAUNCH commits an attempt.
    """
    now = _T0
    rec = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=3,
        head_observed_at=now,
    )
    # 1 second later: nowhere near the backoff (4*BASE = 60 min). DEFER expected.
    v = decide(
        now=now + timedelta(seconds=1),
        head_msg_id="msg-2",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v.decision is Decision.DEFER
    assert v.attempts_before == 3
    assert v.attempts_after == 3  # NOT incremented


# --- 10. commit_launch produces a session-start-before record ------------------------------------


def test_commit_launch_produces_full_record_for_pre_session_write() -> None:
    """LAUNCH commits a fully-populated Record; the write is the sweep's "before-session" step.

    The 4H forced-kill scenario: the SDK session is capped at ``ExecTimeLimit=PT4H`` by the
    Windows Task Scheduler. If the sweep wrote the record AFTER the session returned, a kill
    would leave ``last_launch_at`` unchanged and the next tick would compute the same "not yet
    backed off" verdict — a tight loop instead of a backoff. Writing before the session start
    means an interrupted launch still counts as an attempt.

    Corollary: the record fields set here match one-for-one against the schema declared in
    :class:`Record`, so a schema drift is a test failure.
    """
    now = _T0
    v = Verdict(
        decision=Decision.LAUNCH,
        reason="progressed",
        token="einstein",
        token_raw="Einstein",
        progressed=True,
        attempts_before=0,
        attempts_after=1,
        delay=timedelta(0),
        eligible_at=now,
    )
    rec = commit_launch(now=now, head_msg_id="msg-42", verdict=v, control_state="supervised")
    assert rec.last_launch_at == now
    assert rec.nomination_at_launch == "einstein"
    assert rec.control_at_launch == "supervised"
    assert rec.head_msg_id_at_launch == "msg-42"
    assert rec.launch_attempts == 1
    assert rec.head_observed_at == now  # TTL clock is refreshed on every launch

    # And the round trip through JSON preserves the whole shape exactly (this is the persistence
    # contract with the sweep wrapper's state file).
    assert record_from_json(record_to_json(rec)) == rec

    # Committing a non-LAUNCH verdict is a programmer error — must raise, never silently succeed.
    non_launch = Verdict(
        decision=Decision.DEFER,
        reason="backoff",
        token="bohr",
        token_raw="Bohr",
        progressed=False,
        attempts_before=1,
        attempts_after=1,
        delay=BASE,
        eligible_at=now + BASE,
    )
    with pytest.raises(ValueError, match="non-LAUNCH"):
        commit_launch(now=now, head_msg_id="msg-42", verdict=non_launch, control_state="run")


# --- 11. backoff never terminates in SKIP -------------------------------------------------------


@pytest.mark.parametrize("attempts", [1, 5, 10, 50, 100, 1000])
def test_backoff_caps_at_cap_and_never_skips(attempts: int) -> None:
    """No matter how large ``launch_attempts`` grows, the verdict is LAUNCH or DEFER, never SKIP.

    This is the second core invariant: **backoff is a floor on launch rate, not a permanent
    silence.** A thread that spins for a month accumulates attempts into the hundreds; the delay
    saturates at CAP and the sweep keeps launching once per CAP forever. The only path to SKIP
    is a stop token in the current head — an operator (or the thread itself) can always close a
    spin by handing to ``NEXT: human``, but the head-skip cache cannot decide to on its own.
    """
    now = _T0
    rec = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=attempts,
        head_observed_at=now,
    )
    # Well beyond CAP: should be a LAUNCH.
    v_ready = decide(
        now=now + CAP * 2,
        head_msg_id="msg-2",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    # `is LAUNCH` already excludes SKIP (Decision is a 3-valued enum), which IS the property
    # this test is here to guarantee — asserting `is not SKIP` as well would be redundant AND
    # mypy would flag it as unreachable. The primary assertion is the load-bearing one.
    assert v_ready.decision is Decision.LAUNCH
    # Delay is capped: whatever the attempts value, the required delay never exceeds CAP.
    assert v_ready.delay <= CAP
    # For any attempts >= 3 (2^(3-1)*BASE = 60 min = CAP), the delay saturates exactly at CAP.
    if attempts >= 3:
        assert v_ready.delay == CAP

    # Just before the delay elapses: should be a DEFER (still not SKIP). Pick a check-time that
    # is strictly less than the required delay so DEFER is guaranteed for every attempts value.
    # (attempts=1 uses BASE, larger attempts saturate at CAP; ``last_launch_at + 1 second`` is
    # always < ``last_launch_at + BASE``.)
    v_early = decide(
        now=now + timedelta(seconds=1),
        head_msg_id="msg-2",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    # `is DEFER` likewise already excludes SKIP; the redundant `is not SKIP` would be dead code.
    assert v_early.decision is Decision.DEFER


# --- 12. verdict_to_json emits all required audit fields (report mode) --------------------------


def test_verdict_to_json_contains_all_report_mode_fields() -> None:
    """report-mode output MUST carry every field the spec calls out for the audit log.

    Fields per Bohr's spec: decision, reason, token_normalised, progressed, delay, eligible_at
    (display value — computed here, NOT persisted to the record), attempts_before, attempts_after.
    Plus token_raw so a report reader can see the exact bytes ``parse_next_token`` returned,
    against which the normalisation is a hint.
    """
    now = _T0
    v = decide(
        now=now,
        head_msg_id="msg-42",
        head_body=_body("Bohr", gloss="（意識しないで）"),  # noqa: RUF001
        control_state="run",
        record=None,
    )
    j = verdict_to_json(v)
    required = {
        "decision",
        "reason",
        "token",
        "token_raw",
        "progressed",
        "attempts_before",
        "attempts_after",
        "delay_seconds",
        "eligible_at",
    }
    assert required <= set(j), f"missing: {required - set(j)}"
    assert j["decision"] == "launch"
    assert j["token"] == "bohr"
    assert j["token_raw"] == "Bohr"
    assert j["progressed"] is False
    assert j["delay_seconds"] == 0.0
    # eligible_at is emitted (as ISO 8601) even though the record never stores it — that IS the
    # Einstein Objection 2 integration point (a display value, not a persisted derivation).
    assert isinstance(j["eligible_at"], str) and j["eligible_at"].startswith("2026-08-11T12:00")


# --- 13. Stage-1 stop-token judgment ignores the timing record ----------------------------------


def test_stage_1_skip_does_not_read_the_record() -> None:
    """A ``NEXT: human`` head returns SKIP regardless of what the record contains.

    Passed both a ``None`` record and a fully-populated, "should-be-launching-any-moment" record.
    Neither state can override the stop token — Stage 1 is by construction a pure function of the
    token alone. This is the Einstein Q4 pin: a future refactor that adds "if record is very old,
    force LAUNCH even on human" would be caught by this test breaking, which is what "closed
    set" means concretely.
    """
    body = "content...\n\nNEXT: human"
    # Case A: null record. Nothing to read even in principle.
    v_null = decide(
        now=_T0,
        head_msg_id="msg-1",
        head_body=body,
        control_state="run",
        record=None,
    )
    assert v_null.decision is Decision.SKIP
    assert v_null.reason == "stop-token"

    # Case B: a record that WOULD launch under Stage 2 (attempts=0, everything progressed) —
    # the SKIP must still fire, because Stage 1 short-circuits before Stage 2 ever sees the
    # record.
    rec = Record(
        last_launch_at=_T0 - timedelta(days=7),
        nomination_at_launch="einstein",  # different from current
        control_at_launch="run",
        head_msg_id_at_launch="msg-old",
        launch_attempts=0,
        head_observed_at=_T0 - timedelta(days=7),
    )
    v_stale = decide(
        now=_T0,
        head_msg_id="msg-1",
        head_body=body,
        control_state="run",
        record=rec,
    )
    assert v_stale.decision is Decision.SKIP
    assert v_stale.reason == "stop-token"


# --- 14. HEAD_CACHE_TTL forces a re-parse -------------------------------------------------------


def test_head_cache_ttl_forces_a_reparse_after_60_min() -> None:
    """After :attr:`HEAD_CACHE_TTL` (60 min), the sweep MUST re-fetch the head body.

    This is the *only* automatic recovery path when the operational premise "operators post new
    messages rather than edit an existing head in place" is broken. An edit to an existing head
    leaves the msg-id unchanged — the head probe returns the same id, the sweep's cache appears
    valid, and without a TTL the change would sit indefinitely invisible. 60 min bounds that
    invisibility.
    """
    now = _T0
    # Fresh launch: TTL just refreshed.
    rec = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=1,
        head_observed_at=now,
    )
    # Nothing has aged out — the cached parse is still fresh.
    assert needs_head_reparse(rec, now + timedelta(minutes=1)) is False
    assert needs_head_reparse(rec, now + HEAD_CACHE_TTL - timedelta(seconds=1)) is False

    # At exactly TTL, a re-parse is required.
    assert needs_head_reparse(rec, now + HEAD_CACHE_TTL) is True
    assert needs_head_reparse(rec, now + HEAD_CACHE_TTL + timedelta(minutes=1)) is True

    # None record and record with no head_observed_at both require a re-parse (no cache to reuse).
    assert needs_head_reparse(None, now) is True
    stale = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=1,
        head_observed_at=None,
    )
    assert needs_head_reparse(stale, now) is True
