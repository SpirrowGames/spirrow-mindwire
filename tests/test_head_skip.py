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
15. Cache-hit predicate uses ``last_observed_*`` (observation fields), NOT ``*_at_launch``:
    a thread that goes SKIP (``NEXT: human``) or DEFER can be re-evaluated on the next tick
    from cached state alone, without an MCP re-fetch. This is the Tier B naysayer round-1 fix on
    PR #140 — the previous cache-hit condition compared against the LAUNCH baseline, which for
    a parked or deferring thread never matched, defeating the caching purpose.
16. ``commit_observation`` refreshes only the observation fields and leaves the launch baseline
    (``_at_launch`` + ``launch_attempts`` + ``last_launch_at``) untouched. Overwriting the
    launch baseline on a non-LAUNCH observation would break the ``progressed`` check on the
    subsequent LAUNCH — the counter would stop climbing at attempts=1 (also naysayer round-1).
17. ``commit_observation`` accepts ``record=None`` and mints an initial record. A parked thread
    that has never launched (``NEXT: human`` on the very first evaluation) must still write
    observation state; without this, ``rec`` stays ``None`` forever and the CLI re-fetches on
    every tick — the exact "MCP spammer" failure the caching is meant to prevent (Tier B
    naysayer round 2 on PR #140).
18. ``commit_launch`` with ``head_fetched=False`` (fail-open LAUNCH on a failed body fetch) MUST
    NOT populate the cache-hit key with the synthesised empty body. Doing so would poison the
    cache for HEAD_CACHE_TTL (60 min): subsequent ticks would synthesise ``NEXT: `` and either
    bypass a real ``NEXT: human`` SKIP or ignore progress under the same head msg id. The
    observation fields must be preserved from ``prior_record`` (also naysayer round 2).
19. Park→resume: a spinning thread parked with ``NEXT: human`` (SKIP) and then resumed to the
    SAME pre-park nomination must LAUNCH immediately, not eat the residual pre-park backoff.
    The ``progressed`` check has three disjuncts, and the third (``token !=
    last_observed_nomination``) is the operator-intervention detector (Tier B naysayer round 3
    on PR #140). Without it, an operator's SKIP + resume looks like a continuation of the
    original spin, and the resumed thread waits up to CAP (60 min) before actually starting.
20. Spin continues climbing across ``commit_observation`` DEFERs when the token stays the same:
    naysayer round 3's fix must NOT accidentally trip progressed for a plain spin. This test
    is a regression guard against reverting the round-1 "commit_observation preserves the
    launch baseline" fix.
21. Persistent network outage: successive fail-open LAUNCHes must NOT reset backoff via the
    round-3 disjunct 3. The synthetic ``token=""`` from a failed fetch compared against the
    ROUND-2-preserved observation would trip disjunct 3 forever without the ``token != ""``
    guard, spinning at 5-min cadence indefinitely. This is the round-4 fix: the guard makes
    the fail-open path climb the backoff to CAP like any other spin (Tier B naysayer round 4).
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
    can_reuse_cached_parse,
    commit_launch,
    commit_observation,
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
    # LAUNCH is also an observation — the cache-hit fields must be populated so the very next
    # tick can reuse this parse without a fetch (Tier B naysayer round 1, PR #140).
    assert rec.last_observed_head_msg_id == "msg-42"
    assert rec.last_observed_nomination == "einstein"

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


# --- 15. cache-hit uses OBSERVATION fields, not LAUNCH baseline ---------------------------------


def test_cache_hit_uses_observation_fields_after_a_skip() -> None:
    """A thread that goes SKIP on ``NEXT: human`` must be re-evaluable from cache next tick.

    Tier B naysayer round 1 (PR #140): the previous cache-hit predicate compared
    ``rec.head_msg_id_at_launch == current_head``. For a parked or deferring thread,
    ``head_msg_id_at_launch`` still points at the LAUNCH baseline (an older msg id), so the
    cache condition failed on every subsequent tick and the CLI re-fetched the body via MCP on
    every tick forever. Splitting observation from launch state (``last_observed_head_msg_id``
    tracked separately) fixes this: an operator posting ``NEXT: human`` is observed once, and
    every subsequent tick with the same head msg id is a cache hit.
    """
    now = _T0
    # Prior state: a launch happened on msg-1 with nomination `bohr`, then an operator posted
    # msg-2 with `NEXT: human`. commit_observation records the observation without touching the
    # launch baseline.
    rec_at_launch = Record(
        last_launch_at=now - timedelta(minutes=30),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=1,
        head_observed_at=now - timedelta(minutes=30),
        last_observed_head_msg_id="msg-1",
        last_observed_nomination="bohr",
    )
    # Tick T1: fetch msg-2's body, decide() returns SKIP, CLI calls commit_observation.
    rec_after_skip = commit_observation(
        now=now,
        head_msg_id="msg-2",
        token="human",
        record=rec_at_launch,
    )
    # The launch baseline must be preserved (progression / backoff semantics rely on it).
    assert rec_after_skip.last_launch_at == rec_at_launch.last_launch_at
    assert rec_after_skip.nomination_at_launch == "bohr"
    assert rec_after_skip.head_msg_id_at_launch == "msg-1"
    assert rec_after_skip.launch_attempts == 1
    # The observation fields must now point at msg-2 / human.
    assert rec_after_skip.last_observed_head_msg_id == "msg-2"
    assert rec_after_skip.last_observed_nomination == "human"
    assert rec_after_skip.head_observed_at == now

    # Tick T2: same head msg id, no fetch needed. can_reuse_cached_parse returns True.
    assert can_reuse_cached_parse(rec_after_skip, "msg-2", now + timedelta(minutes=5)) is True

    # And a different head msg id (msg-3 appeared) invalidates the cache — a fetch IS needed.
    assert can_reuse_cached_parse(rec_after_skip, "msg-3", now + timedelta(minutes=5)) is False

    # A None record can never hit the cache.
    assert can_reuse_cached_parse(None, "msg-2", now) is False

    # An empty current head_msg_id can never hit the cache (nothing to compare).
    assert can_reuse_cached_parse(rec_after_skip, "", now) is False

    # And crucially: even at a cache hit, the TTL still applies. 60+ min after the last
    # observation, we re-parse regardless (the edit-in-place recovery path).
    assert can_reuse_cached_parse(rec_after_skip, "msg-2", now + HEAD_CACHE_TTL) is False


# --- 16. commit_observation preserves the launch baseline ---------------------------------------


def test_commit_observation_preserves_launch_baseline_across_defer_ticks() -> None:
    """Repeated DEFERs must not corrupt the launch baseline that the backoff counter climbs against.

    Regression: a naive fix to the cache-hit problem might be to update
    ``head_msg_id_at_launch`` on every observation. That would break the ``progressed`` check —
    a stuck thread's launch baseline would move to the freshest observation, so the next LAUNCH
    would see ``progressed == True`` (against a moved baseline it thinks progressed) and reset
    attempts to 0, halting the backoff climb at attempts=1 forever. That is the second half of
    the Tier B naysayer round-1 finding: observation and launch state must be independent
    fields, and non-LAUNCH observations must touch only the observation family.
    """
    now = _T0
    # Prior LAUNCH at t=-30min on msg-1, nomination `bohr`, attempts=3 (mid-staircase).
    rec = Record(
        last_launch_at=now - timedelta(minutes=30),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=3,
        head_observed_at=now - timedelta(minutes=30),
        last_observed_head_msg_id="msg-1",
        last_observed_nomination="bohr",
    )

    # Ten DEFER ticks in a row, each with a fresh head msg id (the "spin" scenario). Each tick
    # sees the head has moved but nomination + control have not; the CLI would call
    # commit_observation.
    for i in range(2, 12):
        rec = commit_observation(
            now=now + timedelta(minutes=i),
            head_msg_id=f"msg-{i}",
            token="bohr",  # unchanged nomination on every tick
            record=rec,
        )
        # Launch baseline UNCHANGED on every observation — this is what preserves the counter.
        assert rec.head_msg_id_at_launch == "msg-1"
        assert rec.nomination_at_launch == "bohr"
        assert rec.launch_attempts == 3
        assert rec.last_launch_at == _T0 - timedelta(minutes=30)
        # Observation fields track the freshest head msg id.
        assert rec.last_observed_head_msg_id == f"msg-{i}"
        assert rec.last_observed_nomination == "bohr"

    # Now the backoff delay (4 * BASE = 60 min from a fresh series would be CAP-capped, but with
    # attempts=3 the required delay is 4*BASE = 60min = CAP). After CAP elapses since the last
    # LAUNCH, the next tick LAUNCHes with progressed=False (nomination is still `bohr`) and the
    # attempts counter must correctly climb to 4 — NOT reset to 1. That only works because the
    # launch baseline was preserved across all 10 DEFERs.
    ready = _T0 - timedelta(minutes=30) + CAP  # last_launch_at + CAP
    v = decide(
        now=ready,
        head_msg_id="msg-99",
        head_body="content\n\nNEXT: bohr",
        control_state="run",
        record=rec,
    )
    assert v.decision is Decision.LAUNCH
    assert v.reason == "backoff-elapsed"  # NOT "progressed" — the baseline is still `bohr`
    assert v.progressed is False
    assert v.attempts_after == 4  # climbed to 4, not reset to 1


# --- 17. commit_observation on record=None mints an initial record ------------------------------


def test_commit_observation_mints_initial_record_for_never_launched_thread() -> None:
    """A parked (``NEXT: human``) thread evaluated for the FIRST time must persist observations.

    Tier B naysayer round 2 (PR #140): the earlier CLI required ``rec is not None`` before
    calling commit_observation, so a thread whose very first evaluation was a SKIP (never
    launched, then parked) never had ANY record written. Every subsequent tick found rec=None,
    cache-miss, re-fetch — the "MCP spammer" failure repeated for never-launched threads.

    Fix: commit_observation(record=None) mints an initial record with only the observation
    fields populated (no launch baseline, because nothing has launched). Subsequent ticks can
    cache-hit against that record. If the thread eventually LAUNCHes, commit_launch fills in
    the launch baseline while the observation stays fresh.
    """
    now = _T0
    rec = commit_observation(now=now, head_msg_id="msg-42", token="human", record=None)

    # Launch baseline: never set — this thread has never actually launched.
    assert rec.last_launch_at is None
    assert rec.nomination_at_launch == ""
    assert rec.control_at_launch == ""
    assert rec.head_msg_id_at_launch == ""
    assert rec.launch_attempts == 0

    # Observation fields: populated with the SKIP-time observation, so the cache-hit key works.
    assert rec.last_observed_head_msg_id == "msg-42"
    assert rec.last_observed_nomination == "human"
    assert rec.head_observed_at == now

    # The cache-hit predicate hits on the next tick with the same head msg id.
    assert can_reuse_cached_parse(rec, "msg-42", now + timedelta(minutes=5)) is True

    # And the JSON round-trip preserves the shape (so the wrapper can persist and re-load it).
    assert record_from_json(record_to_json(rec)) == rec


# --- 18. commit_launch on a failed body fetch must NOT poison the cache -------------------------


def test_commit_launch_head_fetched_false_preserves_prior_observation() -> None:
    """A fail-open LAUNCH (body fetch failed) must not overwrite the cache-hit key.

    Tier B naysayer round 2 (PR #140): the earlier commit_launch unconditionally populated
    ``last_observed_head_msg_id`` / ``last_observed_nomination`` from the current verdict. On
    a failed fetch, ``verdict.token`` is empty (synthesised body), so the cache-hit key would
    get poisoned with an empty token for HEAD_CACHE_TTL (60 min): every subsequent cached tick
    would synthesise ``NEXT: `` and either (a) bypass a real ``NEXT: human`` SKIP that a
    successful fetch would have returned, or (b) ignore actual progress hidden behind the
    same probe-reported msg id.

    Fix: ``commit_launch(..., head_fetched=False, prior_record=...)`` preserves the prior
    observation fields (or empty if no prior). The next tick therefore cache-misses (if the
    prior observation was empty) or hits correctly against a REAL previous observation.
    """
    now = _T0

    # Case A: no prior record. Failed fetch on a brand-new thread → LAUNCH, but observation
    # fields must stay empty (no reliable data to cache).
    v = Verdict(
        decision=Decision.LAUNCH,
        reason="no-prior-record",
        token="",  # synthesised empty body → empty token
        token_raw=None,
        progressed=False,
        attempts_before=0,
        attempts_after=1,
        delay=timedelta(0),
        eligible_at=now,
    )
    rec = commit_launch(
        now=now,
        head_msg_id="msg-99",
        verdict=v,
        control_state="run",
        head_fetched=False,
        prior_record=None,
    )
    # Launch baseline: correctly reflects the attempt.
    assert rec.last_launch_at == now
    assert rec.launch_attempts == 1
    assert rec.head_msg_id_at_launch == "msg-99"
    assert rec.nomination_at_launch == ""  # this side records "what we thought at launch"
    # OBSERVATION fields: NOT populated — the fetch failed, so we do not have a reliable parse.
    assert rec.last_observed_head_msg_id == ""
    assert rec.last_observed_nomination == ""
    assert rec.head_observed_at is None
    # Cache-miss on the next tick — correctly forces a re-fetch attempt.
    assert can_reuse_cached_parse(rec, "msg-99", now + timedelta(minutes=5)) is False

    # Case B: prior record with a good observation. Failed fetch → LAUNCH, but observation
    # fields must be CARRIED FORWARD from the prior, not overwritten with the empty verdict.
    prior = Record(
        last_launch_at=now - timedelta(minutes=30),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=1,
        head_observed_at=now - timedelta(minutes=30),
        last_observed_head_msg_id="msg-1",
        last_observed_nomination="bohr",
    )
    rec_b = commit_launch(
        now=now,
        head_msg_id="msg-1",
        verdict=v,
        control_state="run",
        head_fetched=False,
        prior_record=prior,
    )
    # Launch baseline moves forward — this is still a real launch attempt.
    assert rec_b.last_launch_at == now
    assert rec_b.launch_attempts == 1
    # OBSERVATION fields: PRESERVED from prior — the fetch failed, we trust the previous parse.
    assert rec_b.last_observed_head_msg_id == "msg-1"
    assert rec_b.last_observed_nomination == "bohr"
    assert rec_b.head_observed_at == prior.head_observed_at
    # The prior observation is still valid, so the next tick cache-hits against msg-1.
    assert can_reuse_cached_parse(rec_b, "msg-1", now + timedelta(minutes=5)) is True

    # Case C: head_fetched=True keeps the pre-existing behaviour — observation is populated
    # from the current verdict (regression guard against reverting the fix "too far").
    v_ok = Verdict(
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
    rec_c = commit_launch(
        now=now,
        head_msg_id="msg-2",
        verdict=v_ok,
        control_state="run",
        head_fetched=True,
        prior_record=None,
    )
    assert rec_c.last_observed_head_msg_id == "msg-2"
    assert rec_c.last_observed_nomination == "einstein"
    assert rec_c.head_observed_at == now


# --- 19. park -> resume: intermediate SKIP + resume-to-same-token launches immediately ----------


def test_park_and_resume_to_same_token_is_progress_and_launches_immediately() -> None:
    """A parked thread resumed to the SAME pre-park nomination must LAUNCH now, not wait backoff.

    Tier B naysayer round 3 (PR #140). Scenario:
      T1..Tn: thread spins on ``NEXT: bohr``, backoff accumulates to CAP.
      Tp:     operator parks it by posting ``NEXT: human``. SKIP. Launch baseline unchanged
              (still ``bohr``), observation now ``human``.
      Tr:     operator resumes to the same ``NEXT: bohr``.

    Under the old two-disjunct progressed check (``token != baseline_nomination`` OR
    ``control changed``), disjunct 1 is False (``bohr != bohr``) and disjunct 2 is False
    (control unchanged): the intervention is invisible and the resumed thread eats the
    remainder of the pre-park backoff (up to CAP = 60 min).

    The fix: a third disjunct compares against ``last_observed_nomination``, which the SKIP
    updated to ``human``. On resume, ``bohr != human`` -> progressed=True -> LAUNCH now, not
    DEFER.
    """
    now = _T0
    # State after spinning to attempts=4 (delay CAP = 60 min) and then being parked:
    #   - launch baseline still points at the spin (bohr / attempts=4)
    #   - observation reflects the current parked state (human)
    rec_parked = Record(
        last_launch_at=now - timedelta(minutes=30),  # 30 min into the 60 min CAP delay
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-100",
        launch_attempts=4,
        head_observed_at=now - timedelta(minutes=1),
        last_observed_head_msg_id="msg-101",  # the operator's `NEXT: human` msg
        last_observed_nomination="human",  # SKIP updated this
    )

    # Resume: operator posts msg-102 with NEXT: bohr — the pre-park nomination.
    v = decide(
        now=now,
        head_msg_id="msg-102",
        head_body=_body("Bohr"),
        control_state="run",  # control unchanged
        record=rec_parked,
    )

    # Progressed detects the resume even though baseline and control did not change.
    assert v.progressed is True
    assert v.decision is Decision.LAUNCH
    assert v.reason == "progressed"
    # attempts reset: the operator intervention starts a fresh series.
    assert v.attempts_after == 1
    # Zero delay: no residual pre-park backoff applies.
    assert v.delay == timedelta(0)


def test_park_and_resume_to_different_token_launches_immediately_too() -> None:
    """A parked thread resumed to a DIFFERENT nomination also launches now (disjunct 1 fires).

    Belt-and-suspenders: this case would launch even without the round-3 fix (disjunct 1 fires
    on ``einstein != bohr``), but pinning it here makes explicit that the two paths converge to
    the same "immediate launch" verdict, so a future refactor cannot silently favour one
    disjunct over the other.
    """
    now = _T0
    rec_parked = Record(
        last_launch_at=now - timedelta(minutes=30),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-100",
        launch_attempts=4,
        head_observed_at=now - timedelta(minutes=1),
        last_observed_head_msg_id="msg-101",
        last_observed_nomination="human",
    )
    v = decide(
        now=now,
        head_msg_id="msg-102",
        head_body=_body("Einstein"),  # different from pre-park bohr
        control_state="run",
        record=rec_parked,
    )
    assert v.progressed is True
    assert v.decision is Decision.LAUNCH
    assert v.attempts_after == 1


# --- 20. round-3 fix does NOT break the pure spin case (regression guard) -----------------------


def test_spin_still_climbs_backoff_when_only_head_msg_id_changes() -> None:
    """Every commit_observation DEFER between two LAUNCHes must NOT trip progressed=True.

    Regression guard against naysayer round 3's fix over-firing: if the third disjunct
    (``token != last_observed_nomination``) fired on a plain spin, every intermediate DEFER
    (which sets ``last_observed_nomination = token``) would still leave the fields equal, so
    it should NOT trip. But a subtle bug could compare against a STALE observation. Pin this
    explicitly.

    Sequence: LAUNCH on `bohr`; 3 DEFER ticks (head msg-id moves each time, token stays `bohr`);
    then backoff-elapsed. attempts must climb to 2 on the second LAUNCH, not reset to 1.
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
        head_fetched=True,
    )
    assert rec.launch_attempts == 1
    assert rec.nomination_at_launch == "bohr"
    assert rec.last_observed_nomination == "bohr"

    # 3 DEFER ticks (each with a fresh head msg id but the same nomination token).
    for i in range(1, 4):
        rec = commit_observation(
            now=now + timedelta(minutes=i),
            head_msg_id=f"msg-{1 + i}",
            token="bohr",  # unchanged
            record=rec,
        )
        # Neither the launch baseline nor the counter moved.
        assert rec.nomination_at_launch == "bohr"
        assert rec.launch_attempts == 1
        # Observation stays `bohr` on every DEFER (the disjunct-3 guard against park→resume
        # false positives depends on this — an unchanged spin must NOT look like a resume).
        assert rec.last_observed_nomination == "bohr"

    # Backoff elapsed (attempts=1 -> delay=BASE=15min). LAUNCH now, climbing to attempts=2.
    v = decide(
        now=now + BASE,
        head_msg_id="msg-99",  # head moved again, still same token
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v.decision is Decision.LAUNCH
    assert v.reason == "backoff-elapsed"  # NOT "progressed"
    assert v.progressed is False  # spin did NOT progress
    assert v.attempts_after == 2  # climbed to 2, NOT reset to 1


def test_disjunct_3_is_gated_against_empty_last_observed_nomination() -> None:
    """The third disjunct MUST NOT fire when ``last_observed_nomination == ""``.

    Empty observation means "we have not observed via a non-launch, or fail-open erased it".
    Comparing against it would trip disjunct 3 for every non-empty token, which:
      - would break the ``test_defer_does_not_consume_an_attempt`` case (record with empty
        observation, token=`bohr`, DEFER expected but progressed would fire and LAUNCH);
      - would double-count with disjunct 1 on fail-open recovery (baseline also empty →
        disjunct 1 fires; no need to redundantly fire disjunct 3);
      - is not the naysayer's scenario anyway (their scenario has an intermediate SKIP
        producing a NON-empty observation).

    Pin the gate so a "simplification" that drops it is caught.
    """
    now = _T0
    rec = Record(
        last_launch_at=now,
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=1,
        head_observed_at=now,
        last_observed_head_msg_id="",  # hand-constructed / never observed
        last_observed_nomination="",
    )
    # Same token as baseline, one second later. Would DEFER under the correct predicate.
    v = decide(
        now=now + timedelta(seconds=1),
        head_msg_id="msg-2",
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v.decision is Decision.DEFER
    assert v.progressed is False  # empty observation must NOT trip disjunct 3


# --- 21. persistent network outage: fail-open backoff still climbs (round 4 fix) ---------------


def test_persistent_fetch_failure_still_backs_off_and_does_not_spin_forever() -> None:
    """A persistent network outage MUST NOT reset backoff via disjunct 3 on every failed tick.

    Tier B naysayer round 4 (PR #140). The interaction between the round-2 fix (commit_launch
    with head_fetched=False preserves the prior observation to avoid poisoning the cache) and
    the round-3 fix (disjunct 3 compares against last_observed_nomination to detect
    park→resume) created a fail-open loop:

      T0: LAUNCH on `bohr`. baseline=`bohr`, observation=`bohr`, attempts=1.
      T1: fetch FAILS. synth token=``""``. decide → disjunct 1 (``"" != "bohr"``) fires →
          LAUNCH. commit_launch(head_fetched=False) sets baseline=``""`` but preserves
          observation=`bohr` (round-2 anti-poison).
      T2: fetch FAILS. synth token=``""``. decide → disjunct 1 (``"" != ""``) False.
          Disjunct 3 (WITHOUT round-4 guard): ``"" != "bohr"`` = **True** → progressed →
          LAUNCH with attempts reset. Same loop at T3, T4, ..., forever.

    Round-4 fix: gate disjunct 3 on ``token != ""`` too. A synthesised empty from a failed
    fetch is not a real observation; it must not be eligible to trip the operator-intervention
    disjunct. Disjunct 1 still fires on the FIRST failed tick (T1: real baseline `bohr` vs
    empty), so attempts_after=1 lands there — but every subsequent failed tick sees baseline
    already empty (disjunct 1 quiet), and disjunct 3 is now gated. Result: normal backoff.
    """
    now = _T0
    # Prior good state: launched on `bohr` at T-30min, observation caught up.
    rec = commit_launch(
        now=now - timedelta(minutes=30),
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
            eligible_at=now - timedelta(minutes=30),
        ),
        control_state="run",
        head_fetched=True,
    )
    assert rec.nomination_at_launch == "bohr"
    assert rec.last_observed_nomination == "bohr"
    assert rec.launch_attempts == 1

    # Tick T1: fetch fails. Disjunct 1 fires (baseline `bohr` vs empty). LAUNCH.
    v1 = decide(
        now=now,
        head_msg_id="msg-2",
        head_body="",  # synthetic empty (fetch failed)
        control_state="run",
        record=rec,
    )
    assert v1.decision is Decision.LAUNCH
    assert v1.progressed is True  # via disjunct 1: `` "" != "bohr" ``
    assert v1.reason == "progressed"
    rec = commit_launch(
        now=now,
        head_msg_id="msg-2",
        verdict=v1,
        control_state="run",
        head_fetched=False,
        prior_record=rec,
    )
    # Baseline moved to empty; observation preserved as `bohr` (round-2 anti-poison).
    assert rec.nomination_at_launch == ""
    assert rec.last_observed_nomination == "bohr"
    assert rec.launch_attempts == 1  # progressed reset the series

    # Tick T2: 1 sec later. fetch still fails. WITHOUT the round-4 guard, this would LAUNCH
    # forever via disjunct 3. WITH the guard, it DEFERs.
    v2 = decide(
        now=now + timedelta(seconds=1),
        head_msg_id="msg-3",  # head keeps moving on the chatroom side, we just can't fetch
        head_body="",
        control_state="run",
        record=rec,
    )
    assert v2.decision is Decision.DEFER
    assert v2.progressed is False  # neither disjunct fires
    assert v2.reason == "backoff"
    assert v2.delay == BASE  # first backoff step

    # A CAP-length of failed-fetch DEFER/LAUNCH ticks: attempts climbs to steady-state at CAP.
    for i in range(1, 8):
        # Advance by BASE * 2^(i-1), capped at CAP; the next LAUNCH is exactly at that boundary.
        cur_delay = min(BASE * (2 ** (rec.launch_attempts - 1)), CAP)
        t = rec.last_launch_at + cur_delay if rec.last_launch_at else now
        v = decide(
            now=t,
            head_msg_id=f"msg-{100 + i}",
            head_body="",
            control_state="run",
            record=rec,
        )
        # Every fail-open tick MUST reach LAUNCH via backoff-elapsed, NOT via progressed.
        assert v.decision is Decision.LAUNCH
        assert v.reason == "backoff-elapsed"
        assert v.progressed is False  # NOT progressed — disjunct 3 is gated
        rec = commit_launch(
            now=t,
            head_msg_id=f"msg-{100 + i}",
            verdict=v,
            control_state="run",
            head_fetched=False,
            prior_record=rec,
        )
        # attempts increments by 1 per tick, capped in effect by CAP producing steady-state 1/hr.
        # Observation is still preserved as the pre-outage `bohr`.
        assert rec.last_observed_nomination == "bohr"

    # After 7 climbing ticks, attempts is at least 3 (delay saturated at CAP long ago).
    assert rec.launch_attempts >= 3
    # And the observed effective spin rate is bounded: the last two LAUNCHes are >= CAP apart.
    # (Trivially true by construction of the loop, but pinned to keep the intent visible.)
    assert rec.last_launch_at is not None


def test_fail_open_recovery_to_real_token_launches_via_disjunct_1() -> None:
    """After a fail-open outage, a successful fetch of ANY real token LAUNCHes via disjunct 1.

    The round-4 guard on disjunct 3 must not close the recovery path. Recovery goes through
    disjunct 1: baseline is empty (last successful action was a fail-open commit_launch that
    set it to ``""``), and the newly-fetched token is anything real — ``bohr`` or ``einstein``
    or ``human`` or any other. Disjunct 1 fires, LAUNCH is immediate, backoff resets.
    """
    now = _T0
    # State after a fail-open LAUNCH: baseline empty, observation preserved `bohr`.
    rec = Record(
        last_launch_at=now - timedelta(minutes=10),
        nomination_at_launch="",  # baseline was overwritten by the failed-fetch verdict
        control_at_launch="run",
        head_msg_id_at_launch="msg-99",
        launch_attempts=3,
        head_observed_at=now - timedelta(hours=1),  # observation not refreshed during outage
        last_observed_head_msg_id="msg-1",  # the last successfully-fetched msg id
        last_observed_nomination="bohr",  # preserved through the outage
    )

    # Case A: same pre-outage token. Should LAUNCH via disjunct 1 (baseline "" != "bohr").
    v_a = decide(
        now=now,
        head_msg_id="msg-100",  # a fresh msg id (chatroom moved during outage)
        head_body=_body("Bohr"),
        control_state="run",
        record=rec,
    )
    assert v_a.decision is Decision.LAUNCH
    assert v_a.progressed is True
    assert v_a.attempts_after == 1  # reset — recovery is fresh series

    # Case B: different token. Also LAUNCH via disjunct 1.
    v_b = decide(
        now=now,
        head_msg_id="msg-100",
        head_body=_body("Einstein"),
        control_state="run",
        record=rec,
    )
    assert v_b.decision is Decision.LAUNCH
    assert v_b.progressed is True
    assert v_b.attempts_after == 1

    # Case C: stop token (`human`) — Stage 1 catches it, no time judgment reached.
    v_c = decide(
        now=now,
        head_msg_id="msg-100",
        head_body=_body("human"),
        control_state="run",
        record=rec,
    )
    assert v_c.decision is Decision.SKIP
    assert v_c.reason == "stop-token"


def test_park_resume_still_works_after_round_4_guard() -> None:
    """The round-4 ``token != ""`` guard on disjunct 3 must NOT break the round-3 fix.

    Regression guard: a REAL non-empty token (park→resume scenario) must still trip disjunct 3
    against a real non-empty preserved observation. This test replicates round 3's #19 with
    the round-4 change in place, so a future refactor that "simplifies" the guard by dropping
    one side is caught by breaking both tests.
    """
    now = _T0
    rec_parked = Record(
        last_launch_at=now - timedelta(minutes=30),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-100",
        launch_attempts=4,
        head_observed_at=now - timedelta(minutes=1),
        last_observed_head_msg_id="msg-101",
        last_observed_nomination="human",  # non-empty (SKIP observation)
    )
    v = decide(
        now=now,
        head_msg_id="msg-102",
        head_body=_body("Bohr"),  # non-empty (real fetch)
        control_state="run",
        record=rec_parked,
    )
    # Disjunct 3 still fires: token=`bohr` (non-empty) != observation=`human` (non-empty).
    assert v.progressed is True
    assert v.decision is Decision.LAUNCH
    assert v.reason == "progressed"
