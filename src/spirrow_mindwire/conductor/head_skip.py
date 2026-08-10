"""Head-skip nomination predicate — the sweep's per-thread launch decision (2026-08-11).

The scheduled sweep (``deploy/run-conductor-scheduled.ps1``) walks its candidate list every tick
and, for each candidate, needs to decide: **launch the conductor, defer, or skip?** The previous
predicate (``Test-CanSkip`` in the wrapper) answered by comparing ``(head_msg_id, control_state)``
equality — SAME head AND SAME control → SKIP. That kept the sweep cheap on idle threads but had a
silent failure mode measured on 2026-08-11: a thread whose head *did* move but whose nomination
target and control state were unchanged (an in-thread self-nomination such as "NEXT: Bohr — but
not yet") produced a fresh head every turn, defeated the equality check, and got launched every
tick — burning inferences on 12 launches/hour with nothing to show for it. Worse, because the sweep
processes at most one candidate per tick (``thread did work — sweep done``), the spinning candidate
occupied the sweep's whole throughput and *starved* the other 13 candidates behind it.

**This module replaces that predicate.** Instead of head-msg-id equality, the predicate:

1. **Parses the last ``NEXT: <token>`` line of the head message body** (module :mod:`.handoff`
   already does this — we reuse ``parse_next_token`` for the parse and only add token normalisation
   here per ADR-2026-05-29-11 lowercase + separator-normalisation). The parser does NOT look up the
   token against a persona roster — an unknown persona name is NOT allowed to fall into "not a
   loop participant, skip"; it fails **open** into a launch (fail-open = fail-safe here).

2. **Runs a two-stage judgment** (:func:`decide`):

   - **Stage 1 (stop tokens).** ``token in {none, human}`` → :attr:`Decision.SKIP` with reason
     ``stop-token``. Stage 1 reads *nothing else* — no timing state, no record, no clock. This is
     what fixes the failure this module was written for: the stop-token skip set is fixed at
     ``{none, human}`` and cannot shrink or expand no matter what happens in Stage 2, so the head-
     skip cache can never silently swallow a live thread.
   - **Stage 2 (time judgment).** For everything else — including ``UNRESOLVED`` (unparseable /
     missing / weird ``NEXT:``) — the output is either :attr:`Decision.LAUNCH` or
     :attr:`Decision.DEFER`, never SKIP. ``UNRESOLVED`` fails **open** into LAUNCH (which is why
     Stage 2 is not written as an ``if token == unresolved: launch`` branch — the fail-open is a
     structural consequence of "Stage 2 never returns SKIP" plus "Stage 1 stops only ``{none,
     human}``", not a special case a future refactor might drop).

3. **Backoff for the degenerate path** (head moves but nomination and control do not — the
   spec-degeneracy case above). When ``progressed == False``, ``launch_attempts`` accumulates and
   the required delay grows :math:`\\text{BASE} \\cdot 2^{n-1}` capped at :attr:`CAP`. The delay
   **never** terminates in a SKIP — the sweep DEFERs while below the delay and LAUNCHes when the
   delay elapses. This is the design's core invariant: **backoff is a floor on launch rate, not a
   permanent silence.**

4. **Progress unwinds the backoff.** ``progressed`` is ``True`` when ANY of three moves is
   detected: (a) the nomination differs from the last LAUNCH baseline
   (``nomination_at_launch``), (b) the control state differs from the last LAUNCH baseline, or
   (c) the nomination differs from the last OBSERVATION (``last_observed_nomination`` — which
   captures SKIPs / DEFERs, not only LAUNCHes). Disjunct (c) is what makes a **park→resume** a
   progress event: a thread parked with ``NEXT: human`` (SKIP) and then resumed to the
   pre-park nomination has the SAME launch baseline, but the intermediate ``human`` observation
   is visible in ``last_observed_nomination``, so backoff is correctly reset. Head-msg-id
   changes ALONE do NOT count as progress: two different msg-ids that both say ``NEXT: Bohr``
   under the same control state are, for scheduling purposes, the same input. The head msg-id
   is still recorded (``head_msg_id_at_launch``) for **audit only**; it is never consulted by
   :func:`decide`.

5. **Head cache TTL** (:attr:`HEAD_CACHE_TTL`). Head-msg-id equality does not catch an *edit* to
   the existing head message (the msg id is unchanged, only its body is). To bound how long a
   stealth edit stays invisible, every record carries ``head_observed_at`` and the caller
   re-parses when it has aged out. The caller is expected to look at
   :attr:`Record.head_observed_at` before deciding to reuse a cached head body; :func:`decide`
   itself does not fetch anything.

Cost invariants worth stating outright, because the spec depends on them:

- **The stop-token skip set is fixed and closed** at ``{none, human}``. This is the "the head-skip
  cache can never starve a live thread" property. Property-tested at test #2.
- **Backoff has an upper bound** (:attr:`CAP`) and never terminates. Even a degenerate spin
  eventually launches every :attr:`CAP` (defaults: 1 launch/hour). No design-time upper bound
  applies to the *progress* path (a thread whose nomination changes every turn is dispatched at
  the sweep's full cadence) — that is intentional (the human-approved invariant "a progressing
  thread never backs off") and is instead bounded by the environment: Windows scheduler's
  ``MultipleInstancesPolicy=IgnoreNew`` keeps the conductor to one live process, the conductor
  processes one candidate per run, and measured session length is 15-25 min -> effective 2-4
  launches/hour/thread. This is a *load-bearing operational premise*, not a design guarantee.
- **``eligible_at`` is a display value only**. It is emitted on every verdict (for report-mode
  audit and for the log) but never persisted to the record — the record only stores observations
  (``last_launch_at`` etc.), and :func:`decide` recomputes the eligibility each call. Storing a
  derived schedule value in the record would double-book the source of truth and drift the moment
  ``BASE`` / ``CAP`` change (Einstein Objection 2, Bohr msg-878).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .handoff import HUMAN_TOKEN, NONE_TOKEN, parse_next_token

# --- Constants (policy calls, not derived values) ------------------------------------------------
#
# NONE of these three are derived. They are collected here so a future observation-driven tune
# (e.g. "sessions started running to 30 min, so BASE should move up") can find them without
# grep-hunting the file. Change ONLY when a concrete observation says the current value is wrong,
# and record the observation in the git commit message.
#
# BASE
#   Rationale: must be >= session_timeout + 1 tick so a single conductor launch cannot overlap
#   its own next scheduled tick under the ``MultipleInstancesPolicy=IgnoreNew`` scheduler
#   contract. Session timeout is ``PT4H`` (14400 s) worst case (kill) but ~15-25 min typical
#   (measured 2026-08-11 on this project's threads); the tick cadence is 5 min. Choosing 15 min
#   matches the typical session floor with one tick of headroom without imposing untuned delay.
#
# CAP
#   The steady-state ceiling for the degenerate path. At 60 min a "spin" thread produces 1
#   launch/hour: enough that a human eventually sees it in the log, few enough that it does not
#   crowd the sweep out.
#
# HEAD_CACHE_TTL
#   How long a cached head body may be reused before a re-parse is required. Bounds the delay
#   between an edit to an existing head message (msg-id unchanged) and its being picked up. 60 min
#   matches the CAP so the two policy horizons align — a spin thread's next launch and the TTL
#   re-parse cycle are on the same scale.
BASE: timedelta = timedelta(minutes=15)
CAP: timedelta = timedelta(minutes=60)
HEAD_CACHE_TTL: timedelta = timedelta(minutes=60)


# Stop tokens that terminate the head-skip in Stage 1 with SKIP. This is a **closed** set: adding
# to it silently expands the skip surface (the exact failure this module was written to prevent),
# and removing from it silently shrinks the launch-open surface (a stop signal would become a spin
# instead of a quiet park). Any change here needs a test change and an ADR reference.
STOP_TOKENS: frozenset[str] = frozenset({NONE_TOKEN, HUMAN_TOKEN})


class Decision(StrEnum):
    """The three possible verdicts for a single candidate on a single tick."""

    LAUNCH = "launch"  # start the conductor on this thread now
    SKIP = "skip"  # do nothing (stop-token — will not launch until the head moves off the stop)
    DEFER = "defer"  # backoff — do not launch yet; try again once the delay has elapsed


@dataclass(frozen=True)
class Verdict:
    """A single decision + its supporting observations, for logging and report-mode audit.

    All fields are populated on every verdict (Stage 1 SKIP included) so a log reader can read the
    reason from one line without conditional formatting.
    """

    decision: Decision
    reason: str
    token: str  # normalised (casefolded / separator-normalised); empty string on UNRESOLVED
    token_raw: str | None  # exactly what parse_next_token returned (None on missing / unparseable)
    progressed: bool  # True iff nomination OR control changed vs the record; False otherwise
    attempts_before: int  # launch_attempts value on the record BEFORE this decision
    attempts_after: int  # value the record WOULD carry after committing this LAUNCH (else same)
    delay: timedelta  # the backoff delay that applied (0 on progressed / LAUNCH-no-record)
    eligible_at: datetime | None  # WHEN this thread will next be eligible (LAUNCH: now; else t+d)


@dataclass
class Record:
    """The per-``(project, thread_id)`` state persisted between ticks.

    Deliberately all-observation: every field is "what did we see at time T?", never a derived
    schedule value. Storing ``next_eligible_at`` here would be the wrong choice because it makes
    the record a second source of truth for the backoff schedule — a tuning change to ``BASE`` /
    ``CAP`` would then have to migrate the file too. Recomputing on every :func:`decide` call is
    cheaper than the migration risk (Einstein Objection 2, msg-878).

    Two families of fields, deliberately separated (PR #140 Tier B naysayer round 1):

    - **_at_launch** — the observation as of the last LAUNCH decision. Used by :func:`decide` for
      the ``progressed`` check (a moving nomination or control state relative to the launch
      baseline resets the backoff counter). Recorded for audit + progression semantics.
    - **last_observed_** — the observation as of the last EVALUATION (LAUNCH or not — SKIP,
      DEFER, or a passive re-parse all count). Used by the caller (the CLI) as the cache-hit
      predicate: if the current tick's head_msg_id equals ``last_observed_head_msg_id``, we
      already know what the body says (its nomination is in ``last_observed_nomination``) and no
      re-fetch is needed.

    Splitting the two families is the fix for the round-1 naysayer flaw: without it, a parked
    (``NEXT: human``) or deferring thread's ``head_msg_id_at_launch`` still points at the LAUNCH
    baseline and the cache condition ``rec.head_msg_id_at_launch == current_head`` fails on
    every subsequent tick — the CLI would then re-fetch the body forever, defeating the whole
    caching purpose. Updating ``head_msg_id_at_launch`` on non-LAUNCH observations would in turn
    break the progression semantics (we would lose the launch baseline). The two must be
    separate observations of the same file.

    Fields:
      last_launch_at
          Wall-clock UTC of the last LAUNCH decision this module committed for the thread.
      nomination_at_launch
          The **normalised** NEXT token seen at that launch. Used for the ``progressed`` check.
          Empty string when the last launch had no readable NEXT (fail-open case).
      control_at_launch
          The control state seen at that launch (``run`` / ``supervised`` / ``hold`` / other).
      head_msg_id_at_launch
          The head msg id seen at that launch. Recorded for AUDIT only — never consulted by
          :func:`decide` and never used as the cache-hit predicate (see ``last_observed_...``).
      launch_attempts
          The number of consecutive "no-progress" LAUNCHes ending at ``last_launch_at``. Reset to
          0 on any LAUNCH where ``progressed`` was True. Used to compute the backoff delay.
      head_observed_at
          Wall-clock UTC when the sweep last re-parsed the head body. Independent of
          ``last_launch_at`` because the head can be re-observed (edit detection, HEAD_CACHE_TTL
          re-parse) without a launch. Empty when no re-parse has happened yet.
      last_observed_head_msg_id
          The head msg id we last **observed** for this thread — refreshed on every evaluation,
          LAUNCH or not. This is the cache-hit key. Empty when we have never observed one (a
          record freshly forward-migrated from the pre-observation schema will read empty for
          one tick, harmlessly forcing a re-fetch that self-heals the field).
      last_observed_nomination
          The **normalised** NEXT token we last observed for this thread — refreshed on every
          evaluation. Used by the caller to synthesise the head body on a cache hit
          (``NEXT: {last_observed_nomination}`` re-parses to the same token, so :func:`decide`
          reaches the same verdict without a network fetch).
    """

    last_launch_at: datetime | None = None
    nomination_at_launch: str = ""
    control_at_launch: str = ""
    head_msg_id_at_launch: str = ""
    launch_attempts: int = 0
    head_observed_at: datetime | None = None
    last_observed_head_msg_id: str = ""
    last_observed_nomination: str = ""


# --- Parser --------------------------------------------------------------------------------------
#
# The parser MUST NOT consult a roster — that is the whole "fail-open on unknown persona" contract.
# ``parse_next_token`` (from :mod:`.handoff`) already handles: last-line wins, parenthetical gloss
# stripped, trailing punctuation stripped. This module adds the ADR-2026-05-29-11 normalisation
# (lowercase + separator normalisation) so equality comparisons across launches survive cosmetic
# reformatting (``Bohr`` vs ``bohr`` vs ``Bohr`` with a trailing space).

_SEP_RE = re.compile(r"[\s_-]+")


def _normalize_token(name: str) -> str:
    """Normalise a persona / stop-token name for equality comparison (ADR-2026-05-29-11).

    The normalisation is deliberately **weak**: casefold, collapse any run of whitespace /
    underscore / hyphen to a single ``-``, and strip. Two personas that differ only in casing or
    separator style are treated as the same target (which is the right thing: they are the same
    intended handoff written slightly differently). This does not merge distinct personas — the
    normalisation is injective on the alphabet of names ADR-2026-05-29-11 allows.
    """
    if not name:
        return ""
    return _SEP_RE.sub("-", name.strip()).casefold()


def parse_head_token(body: str) -> str:
    """Return the normalised NEXT token for a head message body, or ``""`` when UNRESOLVED.

    UNRESOLVED covers three cases treated identically by :func:`decide`: (a) no ``NEXT:`` line at
    all, (b) a ``NEXT:`` line whose token is empty or unparseable, (c) any other reason
    ``parse_next_token`` returned ``None``. All three fall through Stage 1 (they are not in
    :data:`STOP_TOKENS`) and Stage 2 fails open into LAUNCH — the sweep sees a live-looking thread
    it cannot interpret and tries the conductor rather than silently parking it (Obj3 spirit).
    """
    raw = parse_next_token(body)
    if raw is None:
        return ""
    return _normalize_token(raw)


# --- Judgment -----------------------------------------------------------------------------------


def _backoff_delay(attempts: int) -> timedelta:
    """The required delay for the ``attempts``th consecutive no-progress launch.

    ``attempts == 0`` (a first launch, or a launch after progress) has delay 0. ``attempts == 1``
    is BASE, ``attempts == 2`` is 2·BASE, ..., all capped at CAP. Never overflows — Python's
    ``timedelta`` accepts any int, and ``min(..., CAP)`` guarantees a bounded output even for
    absurdly large attempts (test #11 exercises attempts == 1000).
    """
    if attempts <= 0:
        return timedelta(0)
    # Cap the exponent to avoid needless multiplication on absurd inputs (still functionally
    # equivalent because the result is min()-capped at CAP anyway; this just avoids feeding a
    # 2**1000 multiplier into timedelta arithmetic).
    exponent = min(attempts - 1, 32)
    delay = BASE * (2**exponent)
    return delay if delay < CAP else CAP


def decide(
    *,
    now: datetime,
    head_msg_id: str,
    head_body: str,
    control_state: str,
    record: Record | None,
) -> Verdict:
    """The two-stage predicate.

    ``now`` is the wall-clock UTC of this evaluation, injected so the caller can time-travel in
    tests. ``head_msg_id`` / ``head_body`` come from the sweep's probe. ``control_state`` is the
    project's current control state as the sweep observed it this tick (or ``""`` when the probe
    could not read it — the fail-open case). ``record`` is the persisted state for this thread, or
    ``None`` when the thread has never launched.

    :func:`decide` is pure — it never touches the record. The caller is responsible for calling
    :func:`commit_launch` on a LAUNCH before actually starting the conductor process (the
    "session-start-before write" contract that survives a 4H forced kill: even if the launched
    session dies, the record has already been written with attempts+1, so the backoff floor
    applies to the retry).
    """
    token = parse_head_token(head_body)
    raw = parse_next_token(head_body)

    # --- Stage 1: stop-token judgment. Reads NOTHING but the token. --------------------------
    #
    # Written as a set-membership test (not an ``if token == 'none' or token == 'human'`` branch)
    # so the closed-set invariant is visible: this is the ONLY place the head-skip cache can
    # return SKIP, and expanding it requires editing STOP_TOKENS. Any downstream code that
    # short-circuits SKIP on other conditions is a bug (see test #13).
    if token in STOP_TOKENS:
        return Verdict(
            decision=Decision.SKIP,
            reason="stop-token",
            token=token,
            token_raw=raw,
            progressed=False,
            attempts_before=record.launch_attempts if record else 0,
            attempts_after=record.launch_attempts if record else 0,
            delay=timedelta(0),
            eligible_at=None,
        )

    # --- Stage 2: time judgment. Reads the record; output is LAUNCH or DEFER only. ---------
    #
    # First-launch case: no record → LAUNCH immediately. The sweep has never tried this thread
    # under the new predicate and there is no data to defer against.
    if record is None or record.last_launch_at is None:
        return Verdict(
            decision=Decision.LAUNCH,
            reason="no-prior-record",
            token=token,
            token_raw=raw,
            progressed=False,
            attempts_before=0,
            attempts_after=1,
            delay=timedelta(0),
            eligible_at=now,
        )

    # Progress = the environment moved since we last observed it. Three disjuncts, all needed:
    #
    #   1. ``token != nomination_at_launch``: the nomination has changed since the last LAUNCH
    #      baseline. This is the primary "spin has ended" signal — and it also catches a
    #      fail-open recovery (a prior LAUNCH with head_fetched=False committed an empty
    #      baseline; a subsequent successful fetch with any real token trips this).
    #   2. ``control_state != control_at_launch``: control routing has changed since the last
    #      LAUNCH — a hold-then-run transition, for example, routes a naysayer→implementer
    #      handoff differently.
    #   3. ``last_observed_nomination`` is populated AND ``token`` differs from it: the
    #      nomination has changed since the last OBSERVATION (which includes SKIPs and DEFERs,
    #      not only LAUNCHes). This is the park→resume detector (Tier B naysayer round 3, PR
    #      #140): if a spinning thread on ``NEXT: bohr`` is parked by an operator posting
    #      ``NEXT: human`` and later resumed to the same ``NEXT: bohr``, disjuncts 1 & 2 are
    #      both False (baseline is still bohr / unchanged control), but the intermediate
    #      ``human`` observation lives in ``last_observed_nomination`` — disjunct 3 fires,
    #      backoff resets, LAUNCH is immediate. Without this disjunct, the operator's
    #      intervention would be invisible and the resumed thread would eat the remainder of
    #      the pre-park backoff (up to CAP = 60 min).
    #
    #      The ``last_observed_nomination != ""`` guard keeps disjunct 3 quiet when there is no
    #      observation to compare against — either a hand-constructed record in a test or a
    #      never-actually-observed thread (a fresh-record fail-open LAUNCH commits an empty
    #      observation; a later successful fetch tripping this would double-count with disjunct
    #      1, which already fires from the empty baseline). Empty-vs-empty is not progress.
    #
    # Head msg-id is deliberately NOT read here — the spec-degeneracy failure was precisely a
    # moving head with unchanged nomination+control, and trusting head msg-id would reintroduce
    # it. Head msg-id is stored in the record for AUDIT only.
    progressed = (
        token != record.nomination_at_launch
        or control_state != record.control_at_launch
        or (record.last_observed_nomination != "" and token != record.last_observed_nomination)
    )

    attempts_before = record.launch_attempts
    # Progressed launches reset the backoff counter to 0 BEFORE they add themselves — so the next
    # committed launch's attempts value is 1 (the current one, at the fresh series).
    attempts_next_series = 0 if progressed else attempts_before
    delay = _backoff_delay(attempts_next_series)
    ready_at = record.last_launch_at + delay

    if now < ready_at:
        return Verdict(
            decision=Decision.DEFER,
            reason="backoff",
            token=token,
            token_raw=raw,
            progressed=progressed,
            attempts_before=attempts_before,
            # A DEFER does NOT consume an attempt — the eventual LAUNCH will (see commit_launch).
            attempts_after=attempts_before,
            delay=delay,
            eligible_at=ready_at,
        )

    # LAUNCH. attempts_after is what the caller should write on commit — the "next series" plus
    # one (this launch is the (attempts_next_series+1)th).
    return Verdict(
        decision=Decision.LAUNCH,
        reason="progressed" if progressed else "backoff-elapsed",
        token=token,
        token_raw=raw,
        progressed=progressed,
        attempts_before=attempts_before,
        attempts_after=attempts_next_series + 1,
        delay=delay,
        eligible_at=now,
    )


def commit_launch(
    *,
    now: datetime,
    head_msg_id: str,
    verdict: Verdict,
    control_state: str,
    head_fetched: bool = True,
    prior_record: Record | None = None,
) -> Record:
    """Build the Record to persist BEFORE actually starting the conductor session.

    "Before starting" is the whole discipline that makes the wrapper survive a forced-kill: if we
    wrote the record AFTER the session returned, a session killed at PT4H (or by any OS-level
    stop) would leave ``last_launch_at`` unchanged and the next tick would compute the same "not
    yet backed off" verdict — a tight loop instead of a backoff. Writing before the session start
    means the launch is *reported* even if the session dies mid-flight (test #10).

    ``head_fetched`` distinguishes the two LAUNCH paths (Tier B naysayer round 2, PR #140):

    - **``head_fetched=True``** — the body was successfully re-fetched this tick. The
      observation fields (``last_observed_head_msg_id`` / ``last_observed_nomination``) are
      populated with the fresh values, so the very next tick can cache-hit without another fetch.
    - **``head_fetched=False``** — the LAUNCH is fail-open on a **failed** body fetch. The
      observation fields must NOT be populated with the unreliable current values (writing
      ``last_observed_nomination=""`` from an empty synthesised body would poison the cache: the
      next 60 min of ticks would synthesise ``NEXT: `` and either bypass a real ``NEXT: human``
      SKIP or ignore progress under the same head msg id). Prior observation values are carried
      forward from ``prior_record`` if present (or empty on a never-observed thread).

    Only meant to be called when ``verdict.decision is Decision.LAUNCH``. The caller decides
    whether to actually persist (report-mode does not); this helper only constructs the value.
    """
    if verdict.decision is not Decision.LAUNCH:
        raise ValueError(
            f"commit_launch called with non-LAUNCH verdict: {verdict.decision.value!r} "
            "(only LAUNCH commits a new record — DEFER and SKIP leave the record unchanged)"
        )
    if head_fetched:
        # The observation this tick is reliable; adopt it as the cache-hit key.
        obs_head_msg_id = head_msg_id
        obs_nomination = verdict.token
        obs_at: datetime | None = now
    else:
        # Fail-open LAUNCH on a failed fetch — the observation-side of the record must NOT move
        # (empty observation would poison the cache for HEAD_CACHE_TTL). Carry forward whatever
        # the prior record had; if there is no prior record, the observation fields stay empty
        # and the next tick will cache-miss (correctly) and re-fetch.
        obs_head_msg_id = prior_record.last_observed_head_msg_id if prior_record else ""
        obs_nomination = prior_record.last_observed_nomination if prior_record else ""
        obs_at = prior_record.head_observed_at if prior_record else None
    return Record(
        last_launch_at=now,
        nomination_at_launch=verdict.token,
        control_at_launch=control_state,
        head_msg_id_at_launch=head_msg_id,
        launch_attempts=verdict.attempts_after,
        head_observed_at=obs_at,
        last_observed_head_msg_id=obs_head_msg_id,
        last_observed_nomination=obs_nomination,
    )


def commit_observation(
    *,
    now: datetime,
    head_msg_id: str,
    token: str,
    record: Record | None,
) -> Record:
    """Update the observation fields for a NON-LAUNCH evaluation (SKIP, DEFER, or passive re-parse).

    Refreshes exactly the observation-side fields — ``last_observed_head_msg_id``,
    ``last_observed_nomination``, ``head_observed_at`` — and leaves the launch-baseline fields
    (``_at_launch`` + ``launch_attempts`` + ``last_launch_at``) untouched.

    When ``record`` is ``None``, an initial record is minted with empty launch-baseline fields
    (never-launched thread) and populated observation fields. This handles the parked-and-
    never-launched case: a thread whose very first evaluation is a SKIP on ``NEXT: human``
    would otherwise have no persisted state, cache-miss on every subsequent tick, and re-fetch
    forever — the exact failure that started this fix (Tier B naysayer round 2, PR #140).

    Preserving the launch baseline (when it exists) is what keeps the backoff / progressed
    semantics correct — a DEFER that overwrote ``nomination_at_launch`` would make the next
    tick's ``progressed`` check read against the DEFER'd observation instead of the last actual
    LAUNCH, and the counter would stop climbing at attempts=1 forever. Fixed in round 1.
    """
    if record is None:
        # Never-launched thread whose first evaluation is a SKIP or a DEFER. There is no launch
        # baseline to preserve; write an initial record with only observation fields set. The
        # next evaluation can cache-hit against these — and if it becomes a LAUNCH, commit_launch
        # will fill in the launch baseline while the observation stays fresh.
        return Record(
            head_observed_at=now,
            last_observed_head_msg_id=head_msg_id,
            last_observed_nomination=token,
        )
    return Record(
        last_launch_at=record.last_launch_at,
        nomination_at_launch=record.nomination_at_launch,
        control_at_launch=record.control_at_launch,
        head_msg_id_at_launch=record.head_msg_id_at_launch,
        launch_attempts=record.launch_attempts,
        head_observed_at=now,
        last_observed_head_msg_id=head_msg_id,
        last_observed_nomination=token,
    )


def can_reuse_cached_parse(record: Record | None, head_msg_id: str, now: datetime) -> bool:
    """Is the cached parse in ``record`` still valid for the current ``head_msg_id`` and ``now``?

    Two conditions, both required:

    1. **Cache-hit predicate**: the head msg id we last *observed* (not last *launched*) equals
       the current head msg id. Any change (a new msg posted since we last looked) invalidates
       the cache: we must fetch and re-parse.
    2. **TTL predicate**: the observation is not stale. If :attr:`HEAD_CACHE_TTL` has elapsed
       since the last observation, an edit-in-place could have replaced the body under the same
       msg id; the sweep re-fetches to catch it.

    A ``None`` record or an unset ``last_observed_head_msg_id`` fails both: nothing cached.
    An empty ``head_msg_id`` is treated as unknown — we cannot compare against nothing, so we
    fetch (fail-open on the observability side).
    """
    if record is None or not head_msg_id or not record.last_observed_head_msg_id:
        return False
    if record.last_observed_head_msg_id != head_msg_id:
        return False
    return not needs_head_reparse(record, now)


def needs_head_reparse(record: Record | None, now: datetime) -> bool:
    """Should the sweep re-fetch the head body even if the head msg-id is unchanged?

    A ``None`` record is the never-launched case: the sweep has to fetch the body anyway (there is
    nothing cached to reuse). An unset ``head_observed_at`` (record present but no re-parse
    stamped) is the same — no cached parse, no TTL to compare. Otherwise, re-parse when the
    :attr:`HEAD_CACHE_TTL` has elapsed since the last observation.

    This is the ONLY automatic recovery path when the operational premise "head msg-id changes on
    every intervention" is broken. It is 60 min, not 5 min, because it is the safety-net, not the
    fast path: it must be tight enough that a real edit cannot sit indefinitely, but loose enough
    that a well-behaved (edit-free) intervention pattern does not pay per-tick fetch cost.
    """
    if record is None or record.head_observed_at is None:
        return True
    return (now - record.head_observed_at) >= HEAD_CACHE_TTL


# --- Persistence -------------------------------------------------------------------------------
#
# The state file is JSON-serialisable so the PowerShell wrapper can co-inhabit the ``state/``
# directory alongside heads.json / quarantine.json / evaluated.json (all of which are JSON of
# the same shape family). Two helpers convert Record ↔ dict so callers (a CLI wrapper, tests) can
# work with plain hashtable shapes without importing the dataclass.


def record_to_json(record: Record) -> dict[str, Any]:
    """Serialise a :class:`Record` to a JSON-safe dict.

    Datetimes serialise as ISO 8601 strings (UTC ``+00:00``), matching the convention used by the
    sweep wrapper's other state files. Missing timestamps stay ``None`` (JSON ``null``) rather
    than empty strings — the JSON reader distinguishes them so the round trip is exact.
    """
    return {
        "last_launch_at": _iso(record.last_launch_at),
        "nomination_at_launch": record.nomination_at_launch,
        "control_at_launch": record.control_at_launch,
        "head_msg_id_at_launch": record.head_msg_id_at_launch,
        "launch_attempts": int(record.launch_attempts),
        "head_observed_at": _iso(record.head_observed_at),
        "last_observed_head_msg_id": record.last_observed_head_msg_id,
        "last_observed_nomination": record.last_observed_nomination,
    }


def record_from_json(data: dict[str, Any] | None) -> Record | None:
    """Deserialise a JSON dict back to a :class:`Record`.

    A ``None`` input yields ``None`` (the "no record" case). A dict with missing fields yields a
    Record with defaults for the missing fields — this is the forward-compatibility path for a
    file written by an older version of this module (e.g. an old file with no
    ``last_observed_head_msg_id`` reads empty for that field, and the CLI's cache-hit predicate
    fails once, forcing one fetch that then self-heals the field). ISO 8601 timestamps parse via
    :func:`datetime.fromisoformat` (Python 3.11 handles the trailing ``Z`` and ``+00:00``).
    """
    if data is None:
        return None
    return Record(
        last_launch_at=_parse_iso(data.get("last_launch_at")),
        nomination_at_launch=str(data.get("nomination_at_launch") or ""),
        control_at_launch=str(data.get("control_at_launch") or ""),
        head_msg_id_at_launch=str(data.get("head_msg_id_at_launch") or ""),
        launch_attempts=int(data.get("launch_attempts") or 0),
        head_observed_at=_parse_iso(data.get("head_observed_at")),
        last_observed_head_msg_id=str(data.get("last_observed_head_msg_id") or ""),
        last_observed_nomination=str(data.get("last_observed_nomination") or ""),
    )


def verdict_to_json(verdict: Verdict) -> dict[str, Any]:
    """Serialise a :class:`Verdict` to a JSON-safe dict for report-mode output.

    ``delay`` and ``eligible_at`` are the "display / audit" fields Bohr's spec calls out: they are
    included in every verdict so a report-mode reader can see what backoff was in force, without
    the record having to persist them. See the module docstring on why derivation-only values do
    not go in the record.
    """
    return {
        "decision": verdict.decision.value,
        "reason": verdict.reason,
        "token": verdict.token,
        "token_raw": verdict.token_raw,
        "progressed": bool(verdict.progressed),
        "attempts_before": int(verdict.attempts_before),
        "attempts_after": int(verdict.attempts_after),
        "delay_seconds": verdict.delay.total_seconds(),
        "eligible_at": _iso(verdict.eligible_at),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        # Ensure UTC — an unaware datetime is treated as UTC (that is the wrapper's convention
        # for its state files; see ConvertTo-UtcInstant in the sweep wrapper).
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# Report-mode toggle. The sweep wrapper reads this env var and passes the mode through to the
# decision CLI; the CLI in turn skips persistence (never writes state) when the mode is "report".
# Named here so it appears in `git grep` alongside the predicate that its state file backs.
REPORT_MODE_ENV: str = "MINDWIRE_HEADSKIP_MODE"
REPORT_MODE_VALUE: str = "report"


__all__ = [
    "BASE",
    "CAP",
    "HEAD_CACHE_TTL",
    "REPORT_MODE_ENV",
    "REPORT_MODE_VALUE",
    "STOP_TOKENS",
    "Decision",
    "Record",
    "Verdict",
    "_normalize_token",
    "can_reuse_cached_parse",
    "commit_launch",
    "commit_observation",
    "decide",
    "needs_head_reparse",
    "parse_head_token",
    "record_from_json",
    "record_to_json",
    "verdict_to_json",
]
