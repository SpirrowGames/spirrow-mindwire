"""Ledger heartbeat — the emitter-side record that lets the digest render an
operational state (``healthy`` / ``idle`` / ``ingest_failure``) instead of a raw
domain conclusion (``0 stalls``).

The thread that specifies this module is ``T-stalled-pr-has-no-detector``. The
concrete definitions this module implements come from Bohr msg-2692 §1 (the
state table, the accounting rule, and the digest-side rendering discipline)
with two amendments landed in this slice:

    * Einstein msg-2691 (blocking #1): ``examined == 0`` is NOT an ingestion
      failure by itself. Failure is decided by ``fetch_outcome`` — a healthy
      idle state (`fetch_outcome == "ok"` and `examined == 0`) advances the
      heartbeat exactly like a healthy non-empty state, so a repository with no
      open PRs does not drive perpetual false alarms into the daily digest.

    * Einstein msg-2687 → msg-2692 (blocking #2 → resolution): the live-canary
      / historical replay probe (R-2c) is dropped, because a probe against
      historical data does not exercise the live retrieval path anyway. The
      drift the probe was meant to catch first appears on the accounting rule
      of the next real ingest: ``recognized + unrecognized == examined`` is
      asserted, and ``unrecognized > 0`` is one of the ingestion-failure
      predicates below.

    * Einstein msg-2693 (advisory): the digest is domain-agnostic. It compares
      ``now`` against ``expires_at`` (an absolute timestamp emitted by THIS
      module as ``evaluated_at + T_HEARTBEAT``). That keeps the heartbeat
      interval — a mindwire-specific policy — encapsulated in the emitter.

The regress this module closes at (msg-2692 §1):

    - domain logic lives HERE (accounting rule + state derivation + expires_at)
    - the digest emits STATE NAMES as the verdict, with ``examined`` breakdown
      as SUBORDINATE evidence
    - the outer freshness predicate is a single wall-clock comparison
      (``now > expires_at``) that carries no domain knowledge and needs no
      second detector to interpret its silence

Not covered here (documented residuals, msg-2692 §3):

    - Residual A — well-formed but mis-classified inputs (parser accepts,
      predicate returns wrong verdict). Recovered by the monotonic-fixture
      obligation (see spec/process/obligations.yaml → OBL-STALL-DETECTOR-
      MONOTONIC-FIXTURES) and the incident-backtest suite
      (tests/test_stall_ledger_incident_backtest.py).

    - Residual B — upstream filter-semantics drift ("200 OK + empty" that
      SHOULD have been non-empty). Not observable from our repo alone. Enters
      the fixture obligation the same way Residual A does when observed by
      operator lane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

# ─── Heartbeat interval (msg-2693 advisory) ────────────────────────────────────────────
#
# The digest gets an absolute ``expires_at`` timestamp per record. The interval that
# derives it lives HERE, not in the digest renderer, so that a change to the sweep
# cadence (or to what the operator considers "too old") is one edit, not two. The
# outer freshness comparison the digest performs is `now > expires_at`, which needs
# no knowledge of the sweep schedule at all.
#
# The value is a policy pick, not a measurement. Two things constrain it: the sweep
# cadence (5 min) sets the smallest interval that can advance the heartbeat, and the
# operator's tolerance for "detector may have died" sets the largest one before the
# digest should turn red. 4h is comfortably above both a routine transient network
# hiccup (self-clearing within one 5-min tick) and a scheduled maintenance window
# (the two we have observed lasted 20 min and 40 min); it stays well under the 24h
# starvation threshold the same digest already uses so a stale heartbeat surfaces
# BEFORE the operator's own attention interval elapses.

T_HEARTBEAT: timedelta = timedelta(hours=4)


class FetchOutcome(StrEnum):
    """Result of talking to one upstream source.

    Named enum-side so the accounting rule and the state derivation both read
    the same identifiers — msg-2691 (Einstein blocking #1) called the earlier
    "0 items means failure" collapse a boundary error precisely because the
    two concepts (transport outcome vs. domain cardinality) were left as one
    field.

    * ``ok``               — talked, got a well-formed answer (possibly empty).
    * ``http_error``       — got an HTTP-layer answer but it was ≠ 2xx.
    * ``timeout``          — waited past the source's deadline.
    * ``auth_failure``     — 401/403 or credential rejection specifically
                             (kept separate from ``http_error`` because a
                             human fixes it differently — rotate a token, not
                             wait it out).
    * ``file_missing``     — a filesystem-backed source (e.g. quarantine.json)
                             was not there or unreadable.
    * ``parse_error``      — the transport succeeded but the payload did not
                             conform to the schema the parser expects.
    """

    OK = "ok"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    AUTH_FAILURE = "auth_failure"
    FILE_MISSING = "file_missing"
    PARSE_ERROR = "parse_error"


class HealthState(StrEnum):
    """The three verdicts the digest renders (msg-2692 §1 table).

    ``stale`` is deliberately absent from this enum — it is derived by the
    digest side from ``expires_at`` and ``now``, not stored on the record.
    Keeping ``stale`` off the record preserves the property Einstein
    endorsed (msg-2691, msg-2693): the record's declared state is what the
    ingest itself observed, and the freshness verdict is a separate
    wall-clock comparison the digest performs with no domain knowledge.
    """

    HEALTHY = "healthy"
    IDLE = "idle"
    INGEST_FAILURE = "ingest_failure"


@dataclass(frozen=True)
class SourceReport:
    """One upstream source's contribution to a single heartbeat evaluation.

    Every ledger evaluation walks 1..N sources (GitHub REST for PRs, chatroom
    API for threads, filesystem for quarantine.json). Each source reports its
    outcome and its counts INDEPENDENTLY so an outage in one source does not
    silently mask an idle-but-healthy signal from another.

    Accounting invariant: ``recognized + unrecognized == examined``. The
    ``__post_init__`` validator asserts it here so a producer cannot silently
    drop a row it could not parse — the record must show the drop as
    ``unrecognized > 0`` and inherit the ``ingest_failure`` state. This is the
    load-bearing defence against schema drift: graceful-empty is structurally
    impossible when the accounting is enforced.
    """

    name: str
    fetch_outcome: FetchOutcome
    examined: int
    recognized: int
    unrecognized: int

    def __post_init__(self) -> None:
        # Non-negative — a negative count would already be a bug on the emitter
        # side, but the assertion here catches the case before the record ever
        # touches the state table (a negative would silently make some sums
        # look "correct" by cancellation).
        if self.examined < 0 or self.recognized < 0 or self.unrecognized < 0:
            raise ValueError(
                f"SourceReport({self.name!r}): counts must be non-negative "
                f"(examined={self.examined}, recognized={self.recognized}, "
                f"unrecognized={self.unrecognized})"
            )
        # The accounting rule (msg-2692 §1). This is what makes graceful-empty
        # structurally impossible: a parser that silently drops the rows it
        # cannot understand leaves ``recognized + unrecognized < examined``,
        # and the check below prevents such a record from ever being built.
        if self.recognized + self.unrecognized != self.examined:
            raise ValueError(
                f"SourceReport({self.name!r}): accounting violation "
                f"recognized({self.recognized}) + unrecognized({self.unrecognized}) "
                f"!= examined({self.examined}). No silent-discard: every examined "
                "row must land in exactly one of the two buckets."
            )

    def is_failure(self, expected_format_version: str, observed_version: str) -> bool:
        """Would this source flip the record's state to ``ingest_failure``?

        ``fetch_outcome != ok`` — transport did not succeed.
        ``unrecognized > 0``  — parser saw rows it could not understand.
        ``expected != observed`` version — schema stamp says the source
                                           payload is a version we do not
                                           know how to read.

        ``examined == 0`` is DELIBERATELY not a failure here (msg-2692 §1,
        msg-2691 blocking #1): a healthy source with no items to report is
        the ``idle`` state, not a failure.
        """

        if self.fetch_outcome != FetchOutcome.OK:
            return True
        if self.unrecognized > 0:
            return True
        return expected_format_version != observed_version


@dataclass(frozen=True)
class HeartbeatRecord:
    """One evaluation's record — emitted every tick (msg-2692 §4-1).

    Fields the digest reads:

        * ``evaluated_at``          — this evaluation ran to completion.
        * ``last_valid_ingest_at``  — the most recent time a HEALTHY-OR-IDLE
                                      ingest was recorded (advances on state
                                      != INGEST_FAILURE, stops otherwise).
        * ``expires_at``            — ``evaluated_at + T_HEARTBEAT``. The
                                      digest tests ``now > expires_at`` to
                                      derive ``stale`` (msg-2693 advisory).
        * ``sources``               — per-source SourceReport list; the digest
                                      surfaces this as subordinate evidence
                                      under the state name.
        * ``stalls``                — the actual level-triggered stall list.
        * ``input_format_version``  — this evaluation's expected version.
        * ``observed_format_versions`` — the version stamp EACH source sent;
                                      a mismatch flips the record to
                                      ``ingest_failure`` on that source.
    """

    evaluated_at: datetime
    input_format_version: str
    sources: tuple[SourceReport, ...]
    observed_format_versions: Mapping[str, str]
    last_valid_ingest_at: datetime | None = None
    stalls: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Every source that reports must have a matching observed_format_versions
        # entry — the version stamp is a REQUIRED part of the source report,
        # not an optional add-on. Making the two fields separate keeps the
        # per-source counts frozen even when the version stamp API changes
        # shape, but the two MUST stay in sync per evaluation.
        for src in self.sources:
            if src.name not in self.observed_format_versions:
                raise ValueError(
                    f"HeartbeatRecord: source {src.name!r} has no version stamp in "
                    "observed_format_versions — every source must declare which "
                    "schema its payload was produced against."
                )

    @property
    def expires_at(self) -> datetime:
        """The absolute time after which the digest should render ``stale``
        (msg-2693 advisory): the digest just does ``now > record.expires_at``
        and needs no other knowledge to interpret it.

        Derived rather than stored: keeping the interval encapsulated in this
        module means changing ``T_HEARTBEAT`` is a one-line edit here, and no
        digest-side change is needed to pick it up.
        """

        return self.evaluated_at + T_HEARTBEAT

    def failing_sources(self) -> tuple[SourceReport, ...]:
        """Return the sources that pushed this record to INGEST_FAILURE state."""

        return tuple(
            src
            for src in self.sources
            if src.is_failure(
                expected_format_version=self.input_format_version,
                observed_version=self.observed_format_versions.get(src.name, ""),
            )
        )


def derive_state(record: HeartbeatRecord) -> HealthState:
    """Map a heartbeat record to its state (msg-2692 §1 table).

    Evaluation order matters:

        1. ANY source is failing → ``ingest_failure``.
        2. All ok, all examined == 0 → ``idle``.
        3. Otherwise → ``healthy``.

    The failure-first order is what prevents an idle-looking record from
    masking a partial outage: two sources returned empty, one returned an
    HTTP 500. Falling through to ``idle`` on the first two would hide the
    500 completely; enforcing failure-first surfaces the 500 EVEN when the
    other sources look healthy.
    """

    if record.failing_sources():
        return HealthState.INGEST_FAILURE

    # msg-2692 §1: "all_sources have fetch_outcome == ok and examined == 0"
    if all(src.examined == 0 for src in record.sources):
        return HealthState.IDLE

    return HealthState.HEALTHY


def advance_last_valid_ingest_at(
    record: HeartbeatRecord,
    previous_last_valid: datetime | None,
) -> datetime | None:
    """The one bit of authority the emitter has over the outer predicate: does
    ``last_valid_ingest_at`` advance this tick? (msg-2692 §2.)

    * HEALTHY or IDLE → advances to ``evaluated_at``.
    * INGEST_FAILURE  → holds at the previous value.

    Passing ``previous_last_valid`` in (rather than reading it off the record)
    keeps this function pure: the record is a per-evaluation snapshot, the
    state file is the caller's business.
    """

    if derive_state(record) == HealthState.INGEST_FAILURE:
        return previous_last_valid
    return record.evaluated_at


def is_stale(
    now: datetime,
    record: HeartbeatRecord,
    last_valid_ingest_at: datetime | None,
) -> bool:
    """Digest-side freshness predicate (msg-2693 advisory).

    Two comparisons, no domain logic:

        * ``last_valid_ingest_at`` is None (nothing has ever succeeded) → stale.
        * ``now > last_valid_ingest_at + T_HEARTBEAT`` → stale.

    The interval lives in this module (via ``record.expires_at`` for the case
    where the caller passes ``last_valid_ingest_at == record.evaluated_at``);
    the digest's contract is only "compare a wall-clock now against the
    expiry we hand you".
    """

    if last_valid_ingest_at is None:
        return True
    return now > last_valid_ingest_at + T_HEARTBEAT


# ─── Digest rendering (msg-2692 §4-3) ──────────────────────────────────────────────────
#
# The digest surface owns the layout. This helper produces the FIXED CANONICAL LINE
# for one heartbeat record — the "verdict" the digest prints. The state name is the
# first token; the per-source counts follow as subordinate evidence UNDER the state,
# never as the verdict itself.
#
# The rule the shape enforces: a reader who reads only the state token gets the
# correct answer. A reader who reads the evidence gets the same answer with more
# detail. There is NO reading of this line that lets "0 stalls" claim health while
# the ingest was broken — that boundary is what msg-2691 blocking-#1 taught this
# module.


def render_digest_lines(
    record: HeartbeatRecord,
    now: datetime,
    last_valid_ingest_at: datetime | None,
) -> tuple[str, ...]:
    """Return the digest lines for this heartbeat record.

    Line 0: state name + freshness suffix (`healthy`, `idle`, `ingest_failure`,
            `stale`).  ``stale`` is derived from ``now`` and appears in place
            of the state name when ``is_stale`` fires — the record's DECLARED
            state (which is what the ingest itself observed) is preserved in
            the subordinate line below rather than dropped.

    Line 1+: subordinate evidence — one line per source with fetch_outcome
             and counts. This is the "件数は verdict の位置から降格して従属
             証拠へ" contract from msg-2692 §1.
    """

    declared = derive_state(record)
    stale = is_stale(now=now, record=record, last_valid_ingest_at=last_valid_ingest_at)

    if stale:
        # Preserve the declared state in the subordinate footer so an operator
        # can tell "stale because ingest_failure" from "stale because the
        # detector process itself died" (declared=healthy but wall-clock says
        # stale — the process is not running).
        header = f"detector: stale (declared={declared.value})"
    else:
        header = f"detector: {declared.value}"

    if last_valid_ingest_at is not None:
        header += f" · last_valid_ingest_at={last_valid_ingest_at.isoformat()}"
    else:
        header += " · last_valid_ingest_at=never"

    lines: list[str] = [header]
    for src in record.sources:
        observed = record.observed_format_versions.get(src.name, "?")
        version_note = (
            f" version_drift(expected={record.input_format_version!r} observed={observed!r})"
            if observed != record.input_format_version
            else ""
        )
        lines.append(
            f"  · {src.name}: fetch={src.fetch_outcome.value} "
            f"examined={src.examined} recognized={src.recognized} "
            f"unrecognized={src.unrecognized}{version_note}"
        )
    if record.stalls:
        lines.append(f"  · stalls: {len(record.stalls)} — {', '.join(record.stalls)}")
    return tuple(lines)


# ─── Query pin (msg-2692 §4-4) ─────────────────────────────────────────────────────────
#
# THE FIRST DEFENCE against "自分側のクエリ drift": we build our own GitHub PR
# retrieval query in ONE function whose exact output is snapshot-tested. The pin
# is not "these bytes are correct" — it is "these bytes changed", which is the
# minimum guarantee that lets a reviewer notice the query moved.
#
# Kept in this module (rather than in a generic github helper) on purpose: the
# heartbeat's ingest path IS this query, so a snapshot elsewhere would either
# duplicate the string (drift risk) or leave the heartbeat's actual retrieval
# unpinned.


def build_open_pr_query(owner: str, repo: str, per_page: int = 100) -> Mapping[str, Any]:
    """Return the (path, params) pair the heartbeat's PR-listing call uses.

    Kept as a MAPPING so a snapshot test can diff structured input rather
    than a formatted URL — a params order swap on the caller side would not
    change the effective request, and the snapshot must not red on it.

    The pin is what makes msg-2692 §4-4 mechanical rather than aspirational:
    ADV-1's own rule from msg-2688 (弁済価格 < 強制機構価格 なら払う) is
    applied backwards here — the fix for query drift IS the query being
    rebuilt correctly, and the trip-wire (a snapshot test) is cheap enough
    to be worth it because a silent query change directly enables the
    residual B failure mode (msg-2692 §3).
    """

    return {
        "path": f"/repos/{owner}/{repo}/pulls",
        "params": {
            "state": "open",  # msg-2692 §1: what the live sweep MUST look at
            "per_page": per_page,
        },
    }
