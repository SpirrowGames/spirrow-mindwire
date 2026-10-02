"""P-2 — spawn-time preflight attestation of the naysayer's backend.

**Do not ask the model. Read the server's own accounting record back.**

That is the whole design, and it was arrived at by measurement rather than
taste. msg-953 §1.3 put the same Gemini backend behind two different system
prompts: under one it answered *"I am a member of the Gemini model family"*,
under the other *"I am part of the Claude model family"* — and the gateway's
cost row recorded ``backend: gemini`` for **both**. Self-report is not noisy,
it is **steerable**, and the thing steering it is our own ``system_prompt``. No
amount of sampling fixes that. The accounting row is the one artefact in the
loop the model cannot write. **The row wins.**

So the preflight is two steps:

1. one small **non-streaming** completion to the configured route at the
   configured tier, carrying a fresh trace id
2. ``GET /stats/costs/recent?trace_id=…``, read that request's own row back,
   compare its ``backend``

and it produces an :class:`~spirrow_mindwire.value_objects.AttestationRecord`
or it raises. There is no third outcome: a spawn that cannot attest does not
happen, so the turn is never posted.

What this establishes, and what it does not (T-per-turn-backend-attestation).
The probe proves *"immediately before this turn, from this host, this tier
resolved to this backend"* — and nothing about the streaming turn that follows,
because it is a separate, non-streaming request. That last gap is closed by
:func:`attest_turn`, not here: every naysayer turn's own SDK subprocess carries a
per-turn trace id (``X-Mindwire-Trace``, injected through
``ANTHROPIC_CUSTOM_HEADERS``), the gateway records it on every cost row the turn
causes — streaming ones included — and the turn's rows are read back by that id
and judged on ``backend`` before the verdict may be posted. The posted
``attest:`` line therefore carries ``scope=turn``: the evidence is the turn
itself, not the probe. The probe stays as a cheap (two-token) gate that refuses
to spend a full turn on a route that is already wrong.

(The 2026-08-13 text here said streaming requests leave no row at all —
msg-953 §1.4. That stopped being true on the Lexora side: measured 2026-10-02,
all 266 attested naysayer posts since 2026-09-28 have a ``/v1/messages`` row
for their turn right after their probe.)

Residual hole (a) (msg-954 §6) is handled by *not depending on it*: the live
``fallback_backends`` configuration has never been read, so nothing here claims
fallback is impossible. The preflight reads no configuration at all — it
observes the backend afresh every time, which is why a fallback that served the
probe would surface as a mismatching row and fail the spawn.

**Two columns, two jobs — do not conflate them.** Selection picks *our* rows out
of concurrent traffic; judgement decides them. **Selection reads ``trace_id``**
— a fresh ULID minted for exactly one request (the probe) or one turn, sent as
a header and echoed by the gateway into the row. It is an exact join, not a
heuristic: no id window, no tier filter, no endpoint filter, so a concurrent
turn's rows can neither stand in for ours (the false pass) nor contaminate our
verdict (the false refusal) — Einstein msg-5391. **Judgement reads
``backend``**, which names the route the gateway chose and is the column the
model cannot write. Selecting on an echo of our own request is sound precisely
because it decides nothing: someone replaying our trace id can only *add* rows
to the judged set, and every one of them must still say ``expected``.

Before 2026-10 selection read ``tier`` above an id baseline (and before
2026-09-01, ``model``). Both were addresses, not identities: ``tier`` names what
was asked for, not who asked, so any concurrent naysayer request was
indistinguishable from ours (T-per-turn-backend-attestation msg-5391 §1). The
history below is kept because the measurement it records still explains why
``model`` must never be read as a requester key.

This used to read ``model``, and was correct when written (#143, 2026-08-13):
the gateway then echoed the requested alias into ``model`` and had no ``tier``
column at all. On 2026-09-01T00:23Z Lexora added ``tier`` and changed ``model``
to mean the resolved upstream model id (``"gemini-3.1-pro-preview"``). The cut
is exact — ledger ids ≤8282 carry the old shape, ≥8283 the new, with no
interleaving — so **no row of the old shape has been written since**, and no
backward-compatible branch is carried here: a branch whose condition can never
again be true is a dead limb that still has to be maintained and reasoned about.
The consequence of that deletion is pinned by a test, so re-adding one reds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from ..lexora.client import (
    TRACE_HEADER,
    ChatCompletion,
    ChatMessage,
    LexoraClient,
    LexoraError,
)
from ..route_authority import ROUTE_REDACTED, route_authority
from ..ulid_util import new_ulid
from ..value_objects import AttestationRecord

# msg-954 §3, from the T36 learning ("Lexora Gemini 502 頻発 → retry-until-
# success (最大 3 attempt)"). A 1-shot fail-closed preflight would park the loop
# on any transient 502; an unbounded one would hammer a gateway that is down.
PREFLIGHT_ATTEMPTS = 3

# Short on purpose. The review driver's 900s timeout is sized for a full
# adversarial critique; this is a two-token ping. Live measurements: 6.7-8.2s
# (msg-953 §3) and 1.97s (2026-08-13). 60s is generous for all of them, and
# keeps a hung gateway from stalling a spawn for a quarter of an hour.
PREFLIGHT_TIMEOUT_SECONDS = 60.0

# Deliberately tiny — the reply is never read (see the module docstring), so
# every token bought here would be waste. Gemini is a thinking model and at this
# budget the deliberation consumes it all: the live probe came back
# ``finish_reason="length"`` with ``completion_tokens=0`` and an EMPTY body,
# while the gateway still recorded ``backend: gemini``. An oracle that read the
# body would be broken by that; this one is immune to it by construction.
PREFLIGHT_MAX_TOKENS = 16

PREFLIGHT_PROMPT = "ping"

# How many rows one trace-filtered read may return. A trace id names one
# request (the probe) or one turn; even a turn that makes several upstream calls
# writes a handful of rows, not hundreds. A read that comes back FULL may have
# been cut off, and a cut-off set is exactly how a mismatching row would go
# unjudged — so a full read is "could not determine", never a pass (see
# :func:`_judge`). Sized far above any real turn so that branch is a tripwire.
TRACE_READ_LIMIT = 200

# The gateway row column the trace id (``TRACE_HEADER``, sent by the Lexora
# client) lands in. Contract with spirrow-lexora (T-per-turn-backend-attestation
# msg-5392).
TRACE_ROW_COLUMN = "trace_id"

# The env var Claude Code reads extra request headers from, and the ONE value
# format it accepts: ``Name: Value``, not JSON. Measured, not recalled (D-0a,
# 2026-10-02): the CLI the daemon actually runs (claude_agent_sdk 0.1.77's
# bundled ``claude.exe``, 2.1.133), pointed at a local echo server, sent
# ``X-Mindwire-Trace`` on the ``POST /v1/messages`` ``stream=true`` request when
# given ``X-Mindwire-Trace: <id>`` — both as a bare CLI and through
# ``ClaudeAgentOptions.env``. Given the JSON form ``{"X-Mindwire-Trace": "<id>"}``
# it refused the request outright (``Invalid header name: '{"X-Mindwire-Trace"'``).
# Exactly one form is produced; nothing here tries both.
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"

# A turn's rows can land a moment after its stream closes. Re-read on an empty
# (or unreadable) result only — a mismatch is a verdict and is never re-read
# (msg-5390 §1). Bounded, so a gateway that never records the trace fails the
# turn in seconds rather than hanging it.
TURN_ROW_REREADS = 3
TURN_ROW_REREAD_INTERVAL_SECONDS = 2.0


def custom_headers_env_value(trace_id: str) -> str:
    """The ``ANTHROPIC_CUSTOM_HEADERS`` value that makes a turn carry ``trace_id``."""
    return f"{TRACE_HEADER}: {trace_id}"


class PreflightError(RuntimeError):
    """The preflight could not attest which backend served the configured tier.

    Raised for every failure mode without distinction — unreachable gateway,
    non-2xx, no accounting row, a mismatching backend, candidates that disagree.
    They are one thing operationally: **the route was not proven**, and the
    caller's response to all of them is identical (refuse to spawn). Splitting
    them into a taxonomy would invite a caller to treat some as recoverable,
    which is the failure this class exists to prevent. The distinguishing detail
    goes in the message.

    That last sentence is load-bearing and is why the 2026-09-01 outage is
    answered here with *wording* rather than a new exception class: "the gateway
    wrote no row" and "rows were written but our selector matched none of them"
    are different facts about the world and must be told apart by a reader, but
    they are the same fact operationally (the route was not proven) and must not
    be told apart by a caller.
    """


class _BackendMismatchError(PreflightError):
    """Internal: the row was read and it says the wrong backend.

    Private, and a *subclass*, so the public surface stays the single
    :class:`PreflightError` a caller handles — but the retry loop can tell a
    verdict from a transient by type rather than by matching on message text.
    A string match there would silently start retrying mismatches the day
    someone reworded the message.
    """


class LexoraPreflightClient(Protocol):
    """The two gateway methods the preflight drives.

    A separate, narrower Protocol than
    :class:`~spirrow_mindwire.lexora.client.LexoraChatClient` on purpose:
    ``stats_costs_recent`` is meaningless to the naysayer adapter, and widening
    the existing Protocol would force every fake in the suite to grow a method
    it never calls.
    """

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        trace_id: str | None = None,
    ) -> ChatCompletion: ...

    async def stats_costs_recent(
        self, *, limit: int, trace_id: str | None = None
    ) -> list[dict[str, Any]]: ...


def _row_id(row: dict[str, Any]) -> int:
    """Return a row's ``id``, or raise :class:`PreflightError` if it has none.

    The id is what the ``probe=cost-row#…`` field names so a human can re-open
    the same evidence. A selected row we cannot name is a row we cannot point
    at, so it fails the attempt rather than being skipped — skipping it would
    silently shrink the judged set, and a shrunk judged set is exactly how a
    mismatching row would go unnoticed.
    """
    value = row.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreflightError(f"accounting row has no usable integer id: {row!r}")
    return value


def _observed_shape(rows: list[dict[str, Any]]) -> str:
    """Describe rows we could not select from, for the failure message.

    A **bounded projection**, never ``{row!r}``. The column set is the whole
    diagnostic — it names a schema change (a gateway with no ``trace_id`` column
    yet, say) in one line — and the first row's identifying fields show what the
    selector was handed. Dumping whole rows would put every present and future
    column (``user_id`` today, anything at all tomorrow) into an exception that
    is quoted verbatim into durable chatroom posts, which is the same leak the
    route reducer exists to prevent.
    """
    keys = sorted({key for row in rows for key in row})
    first = rows[0]
    return (
        f"observed columns {keys!r}; first row returned: "
        f"id={first.get('id')!r} {TRACE_ROW_COLUMN}={first.get(TRACE_ROW_COLUMN)!r} "
        f"tier={first.get('tier')!r} backend={first.get('backend')!r}"
    )


def _judge(
    rows: list[dict[str, Any]],
    *,
    trace_id: str,
    expected: str,
    limit: int,
    what: str,
) -> str:
    """Select ``trace_id``'s rows out of ``rows`` and judge them. Returns the probe id.

    The order of the checks is the design (Einstein msg-5389 §1):

    1. **Selection** is client-side and exact: ``row["trace_id"] == trace_id``.
       The gateway is *asked* to filter (``?trace_id=``) but its filter is not
       trusted to exist — a gateway that predates the column ignores the
       parameter and returns the newest rows unfiltered. None of those carry our
       id, so the answer is "no row of ours", never a pass.
    2. **Mismatch first.** Any selected row whose ``backend`` is not
       ``expected`` is a verdict (:class:`_BackendMismatchError`), and it wins
       over every "could not determine" state below: a mismatching row in hand
       is never set aside because the read *also* looked incomplete.
    3. **No selected row** cannot pass: a 2xx response is not evidence of
       routing. This is checked BEFORE the full-read check (PR-gate msg-5852):
       a gateway that ignores ``?trace_id=`` returns the newest ``limit`` rows of
       a populated ledger — a full read with none of ours — and the operator
       must see "did not apply the filter", the deploy-ordering diagnostic, not
       a generic truncation message. The caller still sees ``len(rows) >=
       limit`` and does not re-read (:func:`attest_turn`).
    4. A **full read** (``len(rows) >= limit``) may have been cut off and may be
       hiding a mismatching sibling, so it cannot pass.
    5. Otherwise every selected row said ``expected``. One matching row never
       licenses ignoring a non-matching sibling, which is why (2) checks all.

    ``success`` is NOT filtered on. A failed row still records which backend the
    gateway handed the request to, and if a fallback ever served the request the
    extra row is precisely the evidence that would reveal it.
    """
    selected = sorted((row for row in rows if row.get(TRACE_ROW_COLUMN) == trace_id), key=_row_id)
    backends = {str(row.get("backend")) for row in selected}
    if selected and backends != {expected}:
        raise _BackendMismatchError(
            f"{what} did not resolve to the expected backend: rows for trace "
            f"{trace_id} report {sorted(backends)!r}, expected {expected!r}"
        )
    if not selected:
        if not rows:
            raise PreflightError(
                f"{what}: no accounting row carries trace {trace_id}; "
                f"a 2xx response is not evidence of routing"
            )
        # Rows came back for a trace-filtered read and none is ours: the gateway
        # did not apply the filter. Stated as observed — not why it did not.
        raise PreflightError(
            f"{what}: {len(rows)} accounting row(s) came back for a trace-filtered "
            f"read but none carries {TRACE_ROW_COLUMN} {trace_id!r}; the gateway did "
            f"not apply the filter, so the route is unproven. {_observed_shape(rows)}"
        )
    if len(rows) >= limit:
        raise PreflightError(
            f"{what}: the trace-filtered read for {trace_id} came back full "
            f"({len(rows)} row(s) at limit {limit}), so rows may have been cut off; "
            f"a set that may be incomplete cannot attest the route"
        )
    return "cost-row#" + "+".join(str(_row_id(row)) for row in selected)


async def _attempt(
    client: LexoraPreflightClient,
    *,
    tier: str,
    expected: str,
) -> tuple[str, str]:
    """Run one probe/read-back cycle. Returns ``(backend, probe_id)``.

    Each attempt mints its **own** trace id. A previous attempt that failed at
    the transport layer may still have left a row behind; a trace id shared
    across attempts would make that corpse part of every later attempt's judged
    set, so one bad row would poison all the retries and the retry budget would
    be decorative.
    """
    trace_id = new_ulid()
    await client.chat_completion(
        model=tier,
        messages=[ChatMessage(role="user", content=PREFLIGHT_PROMPT)],
        max_tokens=PREFLIGHT_MAX_TOKENS,
        trace_id=trace_id,
    )
    rows = await client.stats_costs_recent(limit=TRACE_READ_LIMIT, trace_id=trace_id)
    probe = _judge(
        rows,
        trace_id=trace_id,
        expected=expected,
        limit=TRACE_READ_LIMIT,
        what=f"preflight probe for tier {tier!r}",
    )
    return expected, probe


async def attest_backend(
    *,
    base_url: str,
    tier: str,
    expected: str,
    client: LexoraPreflightClient | None = None,
    attempts: int = PREFLIGHT_ATTEMPTS,
    now: Callable[[], datetime] | None = None,
) -> AttestationRecord:
    """Attest which backend ``tier`` resolves to at ``base_url``, or raise.

    ``base_url`` must be the **same** endpoint the session being attested will
    use for inference. It is passed in rather than resolved from
    ``MINDWIRE_LEXORA_URL`` because those are two different variables that can
    hold two different hosts: attesting one endpoint while the session talks to
    another would be an attestation of the wrong thing, phrased so confidently
    that nobody would check.

    ``client`` is injectable for tests; left unset, one is built against
    ``base_url`` and closed before returning.

    Retry policy — the asymmetry is the point:

    - **transport failures are retried** (up to ``attempts``): a 502 from the
      Gemini upstream is frequent and transient (T36), and failing closed on the
      first one would park the loop for a reason that resolves itself.
    - **a backend mismatch is not retried, ever.** It is a verdict, not a
      transient. Retrying it would be re-rolling until the gateway returns the
      answer we wanted, which is indistinguishable from having no check at all —
      and each roll is another billed request.
    """
    owned: LexoraClient | None = None
    if client is None:
        owned = LexoraClient(base_url, timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS)
        client = owned
    clock = now or (lambda: datetime.now(UTC))
    # ``None`` (unresolvable userinfo boundary) and ``""`` (reduced to no
    # authority) both render ``redacted`` here. Unlike the ``source:`` line
    # there is no ``empty`` case to distinguish: spawn already refused an empty
    # base URL, so a value that reduces to nothing is a malformed one.
    route = route_authority(base_url) or ROUTE_REDACTED
    last: Exception | None = None
    try:
        for _ in range(max(1, attempts)):
            try:
                backend, probe = await _attempt(client, tier=tier, expected=expected)
            except _BackendMismatchError:
                raise  # verdict, not transient — see the docstring
            except (PreflightError, LexoraError) as exc:
                last = exc
                continue
            return AttestationRecord(
                tier=tier,
                backend=backend,
                expected=expected,
                route=route,
                probe=probe,
                scope="probe",
                at=clock(),
            )
    finally:
        if owned is not None:
            await owned.aclose()
    raise PreflightError(
        f"preflight could not attest tier {tier!r} at {route} after {attempts} attempt(s): {last}"
    )


TurnRowReader = Callable[[str], Awaitable[list[dict[str, Any]]]]
"""Reads the accounting rows recorded for one trace id (``/stats/costs/recent?trace_id=``)."""


async def attest_turn(
    *,
    route: str,
    tier: str,
    expected: str,
    trace_id: str,
    read_rows: TurnRowReader,
    limit: int = TRACE_READ_LIMIT,
    rereads: int = TURN_ROW_REREADS,
    interval_seconds: float = TURN_ROW_REREAD_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] | None = None,
) -> AttestationRecord:
    """Attest the backend that served the turn tagged ``trace_id``, or raise.

    Called **after** the turn's stream has drained and **before** its verdict is
    posted. The turn's SDK subprocess sent ``trace_id`` on every request it made
    (:func:`custom_headers_env_value`) and the gateway wrote it onto every row
    those requests caused; this reads them back and judges them with
    :func:`_judge`. ``read_rows`` is expected to ask for at most ``limit`` rows.

    Fail-closed in every branch that does not prove the route (msg-5390 §2). In
    particular there is **no fallback to the probe-scope attestation**: falling
    back would reopen the very gap this closes, exactly when resolution fails.

    - a mismatching ``backend`` → :class:`PreflightError` at once, **never
      re-read** (a verdict, not a transient);
    - a full read (possibly truncated) → :class:`PreflightError` at once: the
      same trace re-read returns the same full set;
    - no row of ours, or the read itself failing → re-read up to ``rereads``
      times, ``interval_seconds`` apart (rows can land just after the stream
      closes), then :class:`PreflightError`.

    ``route`` is the already-reduced authority — the same one the probe used.
    The record is ``scope="turn"``: its ``probe`` field names the turn's own
    rows, not the preflight's.
    """
    clock = now or (lambda: datetime.now(UTC))
    what = f"naysayer turn for tier {tier!r}"
    reads = max(0, rereads) + 1
    last: Exception | None = None
    for index in range(reads):
        if index:
            await sleep(interval_seconds)
        try:
            rows = await read_rows(trace_id)
        except LexoraError as exc:
            last = exc
            continue
        try:
            probe = _judge(rows, trace_id=trace_id, expected=expected, limit=limit, what=what)
        except _BackendMismatchError:
            raise  # verdict — see the docstring
        except PreflightError as exc:
            if len(rows) >= limit:
                raise  # a full read does not get better by re-reading it
            last = exc
            continue
        return AttestationRecord(
            tier=tier,
            backend=expected,
            expected=expected,
            route=route,
            probe=probe,
            scope="turn",
            at=clock(),
        )
    raise PreflightError(f"could not attest the {what} at {route} after {reads} read(s): {last}")


__all__ = [
    "CUSTOM_HEADERS_ENV",
    "PREFLIGHT_ATTEMPTS",
    "PREFLIGHT_MAX_TOKENS",
    "PREFLIGHT_PROMPT",
    "PREFLIGHT_TIMEOUT_SECONDS",
    "TRACE_HEADER",
    "TRACE_READ_LIMIT",
    "TRACE_ROW_COLUMN",
    "TURN_ROW_REREADS",
    "TURN_ROW_REREAD_INTERVAL_SECONDS",
    "LexoraPreflightClient",
    "PreflightError",
    "TurnRowReader",
    "attest_backend",
    "attest_turn",
    "custom_headers_env_value",
]
