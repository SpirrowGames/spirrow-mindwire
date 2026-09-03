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

1. one **non-streaming** completion to the configured route at the configured
   tier — non-streaming because that is the only shape the gateway writes a
   cost row for (msg-953 §1.4: 42 streaming naysayer turns left *zero* rows, so
   "no row recorded an alternate route" was trivially true for them)
2. ``GET /stats/costs/recent``, read the row back, compare its ``backend``

and it produces an :class:`~spirrow_mindwire.value_objects.AttestationRecord`
or it raises. There is no third outcome: a spawn that cannot attest does not
happen, so the turn is never posted.

What this does **not** establish (stated here so it is not discovered later —
msg-953 §3 L1). The attestation covers *"at spawn time, from this host, this
tier resolved to this backend"*. It does not cover the individual streaming
turns that follow. If routing were ever request-shape dependent, a
non-streaming preflight would not speak for a streaming turn. Closing that gap
needs per-request records for streaming on the Lexora side (P-5), which is a
different repo and is not done. The record is also taken **once per session**,
so every post in a session carries the observation made at its spawn — not one
made at that post.

Residual hole (a) (msg-954 §6) is handled by *not depending on it*: the live
``fallback_backends`` configuration has never been read, so nothing here claims
fallback is impossible. The preflight reads no configuration at all — it
observes the backend afresh every time, which is why a fallback that served the
probe would surface as a mismatching row and fail the spawn.

**Two columns, two jobs — do not conflate them** (T-preflight-row-selector-
reads-wrong-column). Step 1 *selects* our probe's row out of concurrent traffic;
step 2 *judges* it. Selection reads ``tier``, which carries the alias we asked
for (``"naysayer"``) and is therefore the column that answers "is this row
mine?". Judgement reads ``backend``, which names the route the gateway chose and
is the column the model cannot write. Selecting on an echo of our own request is
sound precisely because it decides nothing: were ``tier`` selection to go wrong
the attempt fails closed, and no amount of a right ``tier`` can make a wrong
``backend`` pass. Measured 2026-09-02 on the live ledger: a ``tier="heavy"``
request was served by ``backend="deep"`` — the two columns genuinely disagree,
which is what makes ``backend`` evidence and ``tier`` merely an address.

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

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from ..lexora.client import ChatCompletion, ChatMessage, LexoraClient, LexoraError
from ..route_authority import ROUTE_REDACTED, route_authority
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

# How many rows to pull when looking for the probe's own row. The endpoint takes
# no filter, so the window has to be wide enough that concurrent traffic cannot
# push a row we just caused off the end of it, and the baseline is re-read
# immediately before each probe so only same-instant traffic competes.
_RECENT_LIMIT = 50


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
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion: ...

    async def stats_costs_recent(self, *, limit: int) -> list[dict[str, Any]]: ...


def _row_id(row: dict[str, Any]) -> int:
    """Return a row's ``id``, or raise :class:`PreflightError` if it has none.

    The ordering key is the only thing separating "the row our probe caused"
    from "a row that was already there". A row we cannot order is a row we
    cannot reason about, so it fails the attempt rather than being skipped —
    skipping it would silently shrink the candidate set, and a shrunk candidate
    set is exactly how a mismatching row would go unnoticed.
    """
    value = row.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreflightError(f"accounting row has no usable integer id: {row!r}")
    return value


def _max_row_id(rows: list[dict[str, Any]]) -> int:
    return max((_row_id(row) for row in rows), default=0)


def _observed_shape(rows: list[dict[str, Any]]) -> str:
    """Describe rows we could not select from, for the failure message.

    A **bounded projection**, never ``{row!r}``. The column set is the whole
    diagnostic — it is what names a schema change in one line — and the first
    row's ``tier``/``model``/``backend`` triple shows what the selector was
    handed. Dumping whole rows would put every present and future column
    (``user_id`` today, anything at all tomorrow) into an exception that is
    quoted verbatim into durable chatroom posts, which is the same leak the
    route reducer exists to prevent.
    """
    keys = sorted({key for row in rows for key in row})
    first = rows[0]
    return (
        f"observed columns {keys!r}; first row above baseline: "
        f"id={first.get('id')!r} tier={first.get('tier')!r} "
        f"model={first.get('model')!r} backend={first.get('backend')!r}"
    )


async def _attempt(
    client: LexoraPreflightClient,
    *,
    tier: str,
    expected: str,
) -> tuple[str, str]:
    """Run one probe/read-back cycle. Returns ``(backend, probe_id)``.

    The baseline is taken **inside** the attempt, not once for the whole
    preflight. A previous attempt that failed at the transport layer may still
    have left a row behind; a baseline carried forward would make that corpse a
    candidate for every subsequent attempt, so one bad row would poison all the
    retries and the retry budget would be decorative.
    """
    baseline = _max_row_id(await client.stats_costs_recent(limit=_RECENT_LIMIT))
    await client.chat_completion(
        model=tier,
        messages=[ChatMessage(role="user", content=PREFLIGHT_PROMPT)],
        max_tokens=PREFLIGHT_MAX_TOKENS,
    )
    rows = await client.stats_costs_recent(limit=_RECENT_LIMIT)
    # Sorted once here so ``candidates`` inherits id order: the probe id joins
    # them in that order, and ``_observed_shape`` reports the *earliest* row
    # above the baseline rather than whichever end the endpoint happened to
    # return first.
    above = sorted((row for row in rows if _row_id(row) > baseline), key=_row_id)
    # ``tier``, not ``model`` — see "Two columns, two jobs" in the module
    # docstring. ``model`` names the resolved upstream model id since
    # 2026-09-01T00:23Z and no longer identifies the requester.
    candidates = [row for row in above if row.get("tier") == tier]
    if not candidates:
        if not above:
            raise PreflightError(
                f"no accounting row at all appeared after the preflight probe "
                f"(baseline id {baseline}); a 2xx response is not evidence of routing"
            )
        # Two states reach this line and the observation does not separate
        # them: our probe's row is one of ``above`` and this selector cannot
        # identify it, or our probe's row is not in this window at all and
        # ``above`` is somebody else's traffic. The split is exhaustive — the
        # row either is or is not in the set we just read — and nothing here
        # decides it: the ``ChatCompletion`` carries no key that joins it to a
        # row, and the row carries no field naming its requester (``user_id``
        # is ``None`` on every row this gateway writes). So the message names
        # both and asserts neither.
        #
        # It used to assert the first as fact ("the gateway's row shape
        # disagree — the probe was served and billed"). Under the ``light``
        # traffic that runs here continuously that is simply false, and
        # "billed" was circular besides: the billing record *is* the row we
        # are reporting we could not find, and this module's own docstring
        # records 42 served turns that left zero rows. Naming a cause the
        # observation does not determine is the same error the pre-#213
        # wording made — only with the accusation pointed the other way.
        # Which party is blamed was never the bug. Asserting past the evidence
        # was.
        raise PreflightError(
            f"{len(above)} accounting row(s) appeared after the preflight probe "
            f"(baseline id {baseline}) but none carries tier {tier!r}. The probe "
            f"was served — the completion returned — but a 2xx response is not "
            f"evidence of routing. This observation does not say which of two "
            f"states produced it: our row is among these and this host's "
            f"selector cannot identify it, or our row is not in this window at "
            f"all and these belong to other traffic. Either way the route is "
            f"unproven. {_observed_shape(above)}"
        )
    # ``success`` is NOT filtered on. A failed row still records which backend
    # the gateway handed the request to, and if a fallback ever served the probe
    # the extra row is precisely the evidence that would reveal it. Dropping
    # unsuccessful rows would drop the evidence.
    backends = {str(row.get("backend")) for row in candidates}
    if backends != {expected}:
        # Includes the single-mismatch case and the "candidates disagree" case
        # (msg-953 §3: 候補が複数なら全候補の backend が expected であること).
        # One matching row does not license ignoring a non-matching sibling.
        raise _BackendMismatchError(
            f"tier {tier!r} did not resolve to the expected backend: rows report "
            f"{sorted(backends)!r}, expected {expected!r}"
        )
    ids = "+".join(str(_row_id(row)) for row in candidates)
    return expected, f"cost-row#{ids}"


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
                at=clock(),
            )
    finally:
        if owned is not None:
            await owned.aclose()
    raise PreflightError(
        f"preflight could not attest tier {tier!r} at {route} after {attempts} attempt(s): {last}"
    )


__all__ = [
    "PREFLIGHT_ATTEMPTS",
    "PREFLIGHT_MAX_TOKENS",
    "PREFLIGHT_PROMPT",
    "PREFLIGHT_TIMEOUT_SECONDS",
    "LexoraPreflightClient",
    "PreflightError",
    "attest_backend",
]
