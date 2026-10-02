"""P-2 preflight + turn attestation — read the server's own accounting row back.

**The one idea under test: do not ask the model, read the server's own
accounting record back.** msg-953 §1.3 measured why — the same Gemini backend
answered "I am part of the Claude model family" under one system prompt and "I
am a member of the Gemini model family" under another, while the cost row said
``backend: gemini`` both times. Self-report is steerable (by *our* system
prompt); the accounting row is the one thing in the loop the model cannot
write. **The row wins.**

**Which rows are ours** is decided by an exact join since
T-per-turn-backend-attestation: every request carries a fresh trace id
(``X-Mindwire-Trace``) and the gateway records it in the row's ``trace_id``
column. Selection is ``row["trace_id"] == ours``; judgement is ``backend``. The
earlier selector (``tier`` above an id baseline) could not tell our row from a
concurrent naysayer request's (Einstein msg-5391), and the tests that pinned it
are replaced here by tests that pin the join.

Two entry points:

- :func:`attest_backend` — the two-token preflight probe before a turn
  (``scope=probe``);
- :func:`attest_turn` — the turn's own rows, read back after its stream drains
  (``scope=turn``) — the record a naysayer post is stamped with.

Everything here is exercised against fakes. One ``@pytest.mark.manual`` smoke
test at the bottom runs the real thing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.lexora.client import (
    TRACE_HEADER,
    ChatCompletion,
    ChatMessage,
    LexoraHTTPError,
    LexoraTimeoutError,
)
from spirrow_mindwire.naysayer.preflight import (
    PREFLIGHT_ATTEMPTS,
    TRACE_READ_LIMIT,
    TURN_ROW_REREAD_INTERVAL_SECONDS,
    TURN_ROW_REREADS,
    PreflightError,
    attest_backend,
    attest_turn,
    custom_headers_env_value,
)
from spirrow_mindwire.naysayer.principles import (
    NAYSAYER_EXPECTED_BACKEND,
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
)
from spirrow_mindwire.value_objects import AttestationRecord

_ROUTE = "http://{{IP_SERVICES}}:8110"
_TIER = "naysayer"
_EXPECTED = "gemini"
_NOW = datetime(2026, 8, 13, 4, 29, 54, tzinfo=UTC)

# MEASURED off the live gateway, never imported from the code under test.
# Writing ``NAYSAYER_UPSTREAM_MODEL`` here would make the fixture agree with the
# implementation by construction, which is precisely how the pre-2026-09-01
# fixture went on passing while production had stopped working.
_LIVE_MODEL_ID = "gemini-3.1-pro-preview"
_LIVE_LIGHT_MODEL_ID = "Qwen3.8-27B"

# Sentinel for "the trace id of the request that caused this row". The fake
# substitutes the real (freshly minted) id at append time; any other string is a
# foreign request's trace.
_OWN = object()


def _row(
    row_id: int,
    *,
    trace: object = _OWN,
    tier: str | None = _TIER,
    model: str = _LIVE_MODEL_ID,
    backend: str = _EXPECTED,
    endpoint: str = "/v1/chat/completions",
) -> dict[str, Any]:
    """One ``/stats/costs/recent`` row, shaped like the live gateway's.

    The 15 columns other than ``trace_id`` are copied from a live read
    (2026-10-02, row 15371: ``id, timestamp, model, backend, endpoint, user_id,
    tokens_input, tokens_output, cost_usd, duration_seconds, success, tier,
    pricing_known, tokens_thinking, tokens_cached_input``). ``trace_id`` is the
    column spirrow-lexora adds for this thread (msg-5392); it is the one column
    here that was **not** measured, because it does not exist yet — the manual
    live test at the bottom is what checks it against the real gateway once it
    does.
    """
    return {
        "id": row_id,
        "timestamp": "2026-10-02T01:05:56.798669+00:00",
        "model": model,
        "backend": backend,
        "endpoint": endpoint,
        "user_id": None,
        "tokens_input": 2,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "duration_seconds": 1.96,
        "success": 1,
        "tier": tier,
        "pricing_known": 1,
        "tokens_thinking": None,
        "tokens_cached_input": None,
        "trace_id": trace,
    }


def _light_row(row_id: int, *, trace: object = None) -> dict[str, Any]:
    """Untraced traffic on a different tier, in the live shape (measured alongside 15371)."""
    return _row(row_id, trace=trace, tier="light", model=_LIVE_LIGHT_MODEL_ID, backend="light")


class _FakeGateway:
    """Fake Lexora: a growing ledger that ``chat_completion`` appends to.

    ``append_on_call[i]`` is what the i-th completion adds; rows whose
    ``trace_id`` is the :data:`_OWN` sentinel get the trace id that completion
    was sent with. ``honours_trace_filter=False`` models a gateway that predates
    the ``trace_id`` filter: the parameter is ignored and the newest rows come
    back unfiltered.
    """

    def __init__(
        self,
        *,
        ledger: list[dict[str, Any]] | None = None,
        append_on_call: list[list[dict[str, Any]]] | None = None,
        raise_on_call: list[Exception | None] | None = None,
        completion_content: str = "pong",
        honours_trace_filter: bool = True,
    ) -> None:
        self.ledger: list[dict[str, Any]] = list(
            ledger if ledger is not None else [_light_row(6031)]
        )
        self._append_on_call = list(append_on_call or [[_row(6032)]])
        self._raise_on_call = list(raise_on_call or [])
        self._completion_content = completion_content
        self._honours = honours_trace_filter
        self.calls: list[dict[str, Any]] = []
        self.stats_reads: list[dict[str, Any]] = []

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        trace_id: str | None = None,
    ) -> ChatCompletion:
        index = len(self.calls)
        self.calls.append(
            {"model": model, "messages": messages, "max_tokens": max_tokens, "trace_id": trace_id}
        )
        if index < len(self._append_on_call):
            for row in self._append_on_call[index]:
                stamped = dict(row)
                if stamped.get("trace_id") is _OWN:
                    stamped["trace_id"] = trace_id
                self.ledger.append(stamped)
        if index < len(self._raise_on_call) and self._raise_on_call[index] is not None:
            raise self._raise_on_call[index]  # type: ignore[misc]
        return ChatCompletion(
            content=self._completion_content,
            reasoning_content=None,
            finish_reason="length",
            model=_LIVE_MODEL_ID,
            usage={},
            raw={},
        )

    async def stats_costs_recent(
        self, *, limit: int, trace_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.stats_reads.append({"limit": limit, "trace_id": trace_id})
        rows = list(reversed(self.ledger))
        if self._honours and trace_id is not None:
            rows = [row for row in rows if row.get("trace_id") == trace_id]
        return rows[:limit]


async def _attest(gateway: _FakeGateway, **kwargs: Any) -> AttestationRecord:
    return await attest_backend(
        base_url=_ROUTE,
        tier=_TIER,
        expected=_EXPECTED,
        client=gateway,
        now=lambda: _NOW,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The happy path — and what the record is allowed to say
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_attestation_reads_the_backend_from_the_accounting_row() -> None:
    gateway = _FakeGateway()
    record = await _attest(gateway)

    assert record.backend == "gemini"
    assert record.expected == "gemini"
    assert record.tier == "naysayer"
    assert record.probe == "cost-row#6032"
    assert record.scope == "probe"
    assert record.at == _NOW
    assert gateway.calls[0]["model"] == "naysayer"


@pytest.mark.anyio
async def test_the_probe_sends_a_trace_id_and_reads_back_by_the_same_one() -> None:
    """★ The join, from the probe's side: one id, sent and then asked for."""
    gateway = _FakeGateway()
    await _attest(gateway)
    sent = gateway.calls[0]["trace_id"]
    assert isinstance(sent, str) and len(sent) == 26  # a ULID
    assert gateway.stats_reads == [{"limit": TRACE_READ_LIMIT, "trace_id": sent}]


@pytest.mark.anyio
async def test_route_is_the_credential_guarded_authority_not_the_raw_url() -> None:
    """The record's ``route`` runs through the SAME reducer as the source line.

    PR #142 landed a credential guard because a base URL can carry an inline
    secret and these lines land in a durable chatroom post. A second, hand-rolled
    reducer here would be a second place for that leak to reappear.
    """
    gateway = _FakeGateway()
    record = await attest_backend(
        base_url="https://user:t0ken@gw.example:8443/v1",
        tier=_TIER,
        expected=_EXPECTED,
        client=gateway,
        now=lambda: _NOW,
    )
    assert record.route == "gw.example:8443"
    assert "t0ken" not in record.route


@pytest.mark.anyio
async def test_route_redacts_when_the_userinfo_boundary_is_unresolvable() -> None:
    gateway = _FakeGateway()
    record = await attest_backend(
        base_url="https://admin:p/assword@api.internal:8443/",
        tier=_TIER,
        expected=_EXPECTED,
        client=gateway,
        now=lambda: _NOW,
    )
    assert record.route == "redacted"
    assert "assword" not in record.route


# --------------------------------------------------------------------------- #
# Fail-closed. Every one of these must refuse to produce a record.
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_backend_mismatch_fails_closed() -> None:
    gateway = _FakeGateway(append_on_call=[[_row(6032, backend="vllm")]])
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    assert "vllm" in str(excinfo.value)
    assert "gemini" in str(excinfo.value)


@pytest.mark.anyio
async def test_mismatch_is_not_retried() -> None:
    """★ A mismatch is a verdict, not a transient.

    Retrying it would be sampling the gateway until it gives the answer we
    want — and each retry is another billed request.
    """
    gateway = _FakeGateway(
        append_on_call=[[_row(6032, backend="vllm")], [_row(6033)], [_row(6034)]]
    )
    with pytest.raises(PreflightError):
        await _attest(gateway)
    assert len(gateway.calls) == 1


@pytest.mark.anyio
async def test_no_row_for_our_trace_fails_closed() -> None:
    """The probe returned 200 but the gateway recorded nothing under our trace.

    Attesting on the strength of a successful HTTP response alone would be
    attesting the response body — exactly what this design refuses to do.
    """
    gateway = _FakeGateway(append_on_call=[[], [], []])
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    assert "no accounting row carries trace" in str(excinfo.value)
    assert "a 2xx response is not evidence of routing" in str(excinfo.value)


@pytest.mark.anyio
async def test_split_rows_under_our_trace_fail_closed() -> None:
    """Several rows under our own trace (a fallback, a retry upstream): ALL must match.

    msg-953 §3: "候補が複数なら全候補の backend が expected であることを要求".
    One matching row does not license ignoring a non-matching sibling — that is
    how a fallback backend would hide.
    """
    gateway = _FakeGateway(append_on_call=[[_row(6032), _row(6033, backend="vllm")]])
    with pytest.raises(PreflightError):
        await _attest(gateway)


@pytest.mark.anyio
async def test_all_rows_under_our_trace_are_recorded_in_the_probe_id() -> None:
    gateway = _FakeGateway(append_on_call=[[_row(6033), _row(6032)]])
    record = await _attest(gateway)
    assert record.probe == "cost-row#6032+6033"  # id order, whatever the read order


@pytest.mark.anyio
async def test_a_failed_row_still_counts() -> None:
    """``success=0`` rows are NOT filtered out — and that is deliberate.

    Residual hole (a) (msg-954 §6): the live ``fallback_backends`` config has
    never been read. A fallback would show up as an extra row attributed to
    another backend; dropping unsuccessful rows would drop that evidence.
    """
    failed = _row(6032, backend="vllm")
    failed["success"] = 0
    gateway = _FakeGateway(append_on_call=[[failed, _row(6033)]])
    with pytest.raises(PreflightError):
        await _attest(gateway)


@pytest.mark.anyio
async def test_malformed_row_without_an_id_fails_closed() -> None:
    bad = _row(6032)
    del bad["id"]
    gateway = _FakeGateway(append_on_call=[[bad]] * 3)
    with pytest.raises(PreflightError):
        await _attest(gateway)


# --------------------------------------------------------------------------- #
# ★ Concurrency — the two defects of the id-window selector (Einstein msg-5391)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_concurrent_requests_good_row_cannot_stand_in_for_ours() -> None:
    """★ msg-5391 §1, the false pass. Our row is absent; another naysayer's is present.

    Same tier, same endpoint, ``backend=gemini`` — under the old ``tier``-above-
    baseline selector this row WAS our candidate and the attempt passed. Under the
    trace join it is somebody else's row and the attempt cannot pass.
    """
    gateway = _FakeGateway(
        append_on_call=[[_row(8753, trace="01FOREIGNTRACE000000000000")]] * 3,
        honours_trace_filter=False,
    )
    with pytest.raises(PreflightError):
        await _attest(gateway)


@pytest.mark.anyio
async def test_a_concurrent_requests_bad_row_cannot_fail_ours() -> None:
    """★ msg-5391 §2, the false refusal. A neighbour was misrouted; we were not.

    Even with a gateway that ignores the filter, the neighbour's row is not
    selected, so its ``backend`` is never judged against our verdict.
    """
    gateway = _FakeGateway(
        append_on_call=[
            [_row(8753), _row(8754, trace="01FOREIGNTRACE000000000000", backend="anthropic")]
        ],
        honours_trace_filter=False,
    )
    record = await _attest(gateway)
    assert record.probe == "cost-row#8753"


@pytest.mark.anyio
async def test_selection_does_not_read_tier_or_model() -> None:
    """``tier`` and ``model`` are addresses, not identities — neither selects.

    A row under our trace whose ``tier`` is null and ``model`` is the upstream id
    is still ours (and judged on ``backend``); a row with ``tier=naysayer`` and no
    trace is not ours. Point the selector back at either column and one of the
    two halves reds.
    """
    ours = _FakeGateway(append_on_call=[[_row(9001, tier=None, model=_LIVE_MODEL_ID)]])
    assert (await _attest(ours)).probe == "cost-row#9001"

    not_ours = _FakeGateway(
        append_on_call=[[_row(9002, trace=None, tier="naysayer")]] * 3,
        honours_trace_filter=False,
    )
    with pytest.raises(PreflightError):
        await _attest(not_ours)


# --------------------------------------------------------------------------- #
# A gateway that does not (yet) apply the filter, and a read that may be cut off
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_gateway_that_ignores_the_filter_is_reported_with_the_row_shape() -> None:
    """Rows came back for a trace-filtered read and none is ours.

    This is exactly what a pre-``trace_id`` Lexora does (the parameter is
    ignored), so it is the failure every naysayer turn hits if the mindwire side
    is deployed first. The message says so in observation only — count, the
    column set (which shows ``trace_id`` missing) and a bounded first row.
    """
    pre_trace = _light_row(8753)
    del pre_trace["trace_id"]
    gateway = _FakeGateway(
        ledger=[],
        append_on_call=[[pre_trace], [], []],
        honours_trace_filter=False,
    )
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    message = str(excinfo.value)
    assert "1 accounting row(s) came back for a trace-filtered read" in message
    assert "the gateway did not apply the filter" in message
    assert "observed columns" in message
    assert "'trace_id'" not in message.split("observed columns")[1].split(";")[0]
    assert "id=8753" in message
    assert "backend='light'" in message


@pytest.mark.anyio
async def test_the_two_empty_selection_failures_do_not_share_a_message() -> None:
    """One failure operationally, two facts about the world — same type, different text."""
    nothing_written = _FakeGateway(append_on_call=[[], [], []])
    with pytest.raises(PreflightError) as empty:
        await _attest(nothing_written)

    unfiltered = _FakeGateway(append_on_call=[[], [], []], honours_trace_filter=False)
    with pytest.raises(PreflightError) as ignored:
        await _attest(unfiltered)

    assert type(empty.value) is type(ignored.value) is PreflightError
    assert str(empty.value) != str(ignored.value)
    assert "no accounting row carries trace" in str(empty.value)
    assert "did not apply the filter" in str(ignored.value)


@pytest.mark.anyio
async def test_the_failure_message_does_not_dump_whole_rows() -> None:
    """The projection is bounded: these strings land in durable chatroom posts."""
    row = _light_row(6032)
    row["user_id"] = "{{USER_SERVICES}}@example.internal"
    gateway = _FakeGateway(append_on_call=[[row]] * 3, honours_trace_filter=False)
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    message = str(excinfo.value)
    assert "'user_id'" in message  # the column is named...
    assert "{{USER_SERVICES}}@example.internal" not in message  # ...its value is not.


@pytest.mark.anyio
async def test_a_full_read_cannot_pass() -> None:
    """A read at the limit may have been cut off, hiding a mismatching sibling."""
    gateway = _FakeGateway(append_on_call=[[_row(10_000 + i) for i in range(TRACE_READ_LIMIT)]] * 3)
    with pytest.raises(PreflightError, match="came back full"):
        await _attest(gateway)


@pytest.mark.anyio
async def test_a_mismatch_in_hand_beats_a_full_read() -> None:
    """★ Einstein msg-5389 §1: judge the rows you hold BEFORE declaring the read incomplete.

    A full read that already contains a mismatching row of ours is a verdict
    (not retried), not a "could not determine" (retried).
    """
    rows = [_row(10_000 + i) for i in range(TRACE_READ_LIMIT - 1)]
    rows.append(_row(20_000, backend="anthropic"))
    gateway = _FakeGateway(append_on_call=[rows, [_row(1)], [_row(2)]])
    with pytest.raises(PreflightError, match="did not resolve to the expected backend"):
        await _attest(gateway)
    assert len(gateway.calls) == 1


# --------------------------------------------------------------------------- #
# Retry — required, and bounded (T36: Lexora Gemini 502s are frequent)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_transient_http_failure_is_retried_and_can_succeed() -> None:
    gateway = _FakeGateway(
        raise_on_call=[LexoraHTTPError("502 bad gateway", status_code=502), None],
        append_on_call=[[], [_row(6033)]],
    )
    record = await _attest(gateway)
    assert record.probe == "cost-row#6033"
    assert len(gateway.calls) == 2


@pytest.mark.anyio
async def test_attempts_are_bounded_at_three() -> None:
    assert PREFLIGHT_ATTEMPTS == 3
    gateway = _FakeGateway(
        raise_on_call=[LexoraTimeoutError("timed out")] * 5,
        append_on_call=[[]] * 5,
    )
    with pytest.raises(PreflightError):
        await _attest(gateway)
    assert len(gateway.calls) == PREFLIGHT_ATTEMPTS


@pytest.mark.anyio
async def test_each_attempt_mints_a_fresh_trace_id() -> None:
    """Attempt N must not inherit attempt N-1's rows.

    A 502'd attempt can still have left a row — here a mismatching one. Were the
    trace id shared across attempts, that corpse would be judged in every later
    attempt and the bounded retry would be decorative.
    """
    gateway = _FakeGateway(
        raise_on_call=[LexoraHTTPError("502"), None],
        append_on_call=[[_row(6032, backend="vllm")], [_row(6033)]],
    )
    record = await _attest(gateway)
    assert record.probe == "cost-row#6033"
    assert gateway.calls[0]["trace_id"] != gateway.calls[1]["trace_id"]


@pytest.mark.anyio
async def test_empty_reply_body_does_not_fail_the_attestation() -> None:
    """★ Immunity to the ``max_tokens`` trap: the body is never read."""
    gateway = _FakeGateway(completion_content="")
    record = await _attest(gateway)
    assert record.backend == "gemini"


# --------------------------------------------------------------------------- #
# ★ M5 (Tier-C msg-970 §3) — the PRODUCTION constants, exercised in CI
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_production_constants_attest_against_a_live_shaped_row() -> None:
    """The values the daemon spawns with must attest a row shaped like the real one.

    Mutate ``NAYSAYER_EXPECTED_BACKEND`` to the upstream model id and the row
    saying ``backend: "gemini"`` stops matching, and this reds.
    """
    gateway = _FakeGateway(
        append_on_call=[[_row(6032, tier="naysayer", model=_LIVE_MODEL_ID, backend="gemini")]],
    )
    record = await attest_backend(
        base_url=_ROUTE,
        tier=NAYSAYER_MODEL_TIER,
        expected=NAYSAYER_EXPECTED_BACKEND,
        client=gateway,
        now=lambda: _NOW,
    )
    assert record.backend == "gemini"
    assert record.tier == "naysayer"


@pytest.mark.anyio
async def test_comparing_against_the_upstream_model_id_fails_every_attestation() -> None:
    """The row names the backend family; asking it for the model id can never succeed."""
    gateway = _FakeGateway(
        append_on_call=[[_row(6032, tier="naysayer", model=_LIVE_MODEL_ID, backend="gemini")]],
    )
    with pytest.raises(PreflightError, match="did not resolve to the expected backend"):
        await attest_backend(
            base_url=_ROUTE,
            tier=NAYSAYER_MODEL_TIER,
            expected=NAYSAYER_UPSTREAM_MODEL,
            client=gateway,
            now=lambda: _NOW,
            attempts=1,
        )


# --------------------------------------------------------------------------- #
# attest_turn — the turn's own rows (T-per-turn-backend-attestation)
# --------------------------------------------------------------------------- #

_TRACE = "01K6FMT1TURNTRACE000000001"


class _Ledger:
    """Scripted ``read_rows``: the i-th read returns ``reads[i]`` (or raises it)."""

    def __init__(self, reads: list[list[dict[str, Any]] | Exception]) -> None:
        self._reads = reads
        self.asked: list[str] = []

    async def __call__(self, trace_id: str) -> list[dict[str, Any]]:
        self.asked.append(trace_id)
        item = self._reads[min(len(self.asked) - 1, len(self._reads) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _turn_row(row_id: int, *, trace: str = _TRACE, backend: str = _EXPECTED) -> dict[str, Any]:
    return _row(row_id, trace=trace, backend=backend, endpoint="/v1/messages")


async def _attest_turn(ledger: _Ledger, sleeps: _Sleeps, **kwargs: Any) -> AttestationRecord:
    return await attest_turn(
        route="lexora.local:8110",
        tier=_TIER,
        expected=_EXPECTED,
        trace_id=_TRACE,
        read_rows=ledger,
        sleep=sleeps,
        now=lambda: _NOW,
        **kwargs,
    )


@pytest.mark.anyio
async def test_the_turn_is_attested_from_its_own_rows_at_turn_scope() -> None:
    ledger, sleeps = _Ledger([[_turn_row(14814)]]), _Sleeps()
    record = await _attest_turn(ledger, sleeps)
    assert record.scope == "turn"
    assert record.probe == "cost-row#14814"
    assert record.backend == record.expected == "gemini"
    assert record.route == "lexora.local:8110"
    assert record.at == _NOW
    assert ledger.asked == [_TRACE]
    assert sleeps.calls == []


@pytest.mark.anyio
async def test_a_tool_using_turn_with_several_rows_needs_every_one_to_match() -> None:
    ok = await _attest_turn(_Ledger([[_turn_row(2), _turn_row(1)]]), _Sleeps())
    assert ok.probe == "cost-row#1+2"
    with pytest.raises(PreflightError, match="did not resolve"):
        await _attest_turn(_Ledger([[_turn_row(1), _turn_row(2, backend="anthropic")]]), _Sleeps())


@pytest.mark.anyio
async def test_a_turn_mismatch_is_a_verdict_and_is_never_re_read() -> None:
    ledger, sleeps = _Ledger([[_turn_row(1, backend="anthropic")], [_turn_row(2)]]), _Sleeps()
    with pytest.raises(PreflightError, match="anthropic"):
        await _attest_turn(ledger, sleeps)
    assert len(ledger.asked) == 1
    assert sleeps.calls == []


@pytest.mark.anyio
async def test_a_late_row_is_found_by_a_re_read() -> None:
    """Rows can land a moment after the stream closes; an empty first read re-reads."""
    ledger, sleeps = _Ledger([[], [], [_turn_row(7)]]), _Sleeps()
    record = await _attest_turn(ledger, sleeps)
    assert record.probe == "cost-row#7"
    assert sleeps.calls == [TURN_ROW_REREAD_INTERVAL_SECONDS] * 2


@pytest.mark.anyio
async def test_no_row_after_every_re_read_fails_closed_and_does_not_fall_back() -> None:
    """★ msg-5390 §2: "could not determine" fails closed. There is no probe-scope fallback."""
    assert TURN_ROW_REREADS == 3
    assert TURN_ROW_REREAD_INTERVAL_SECONDS == 2.0
    ledger, sleeps = _Ledger([[]]), _Sleeps()
    with pytest.raises(PreflightError) as excinfo:
        await _attest_turn(ledger, sleeps)
    assert len(ledger.asked) == TURN_ROW_REREADS + 1
    assert sleeps.calls == [TURN_ROW_REREAD_INTERVAL_SECONDS] * TURN_ROW_REREADS
    assert f"after {TURN_ROW_REREADS + 1} read(s)" in str(excinfo.value)
    assert "no accounting row carries trace" in str(excinfo.value)


@pytest.mark.anyio
async def test_an_unreadable_ledger_is_re_read_then_fails_closed() -> None:
    ledger = _Ledger([LexoraHTTPError("502", status_code=502)])
    sleeps = _Sleeps()
    with pytest.raises(PreflightError, match="502"):
        await _attest_turn(ledger, sleeps)
    assert len(ledger.asked) == TURN_ROW_REREADS + 1

    recovering = _Ledger([LexoraTimeoutError("timed out"), [_turn_row(3)]])
    assert (await _attest_turn(recovering, _Sleeps())).probe == "cost-row#3"


@pytest.mark.anyio
async def test_a_full_turn_read_fails_at_once_and_a_mismatch_in_it_wins() -> None:
    full = [_turn_row(10_000 + i) for i in range(TRACE_READ_LIMIT)]
    ledger, sleeps = _Ledger([full]), _Sleeps()
    with pytest.raises(PreflightError, match="came back full"):
        await _attest_turn(ledger, sleeps)
    assert len(ledger.asked) == 1  # re-reading a full set returns the same full set

    poisoned = [*full[:-1], _turn_row(20_000, backend="anthropic")]
    with pytest.raises(PreflightError, match="did not resolve"):
        await _attest_turn(_Ledger([poisoned]), _Sleeps())


@pytest.mark.anyio
async def test_other_turns_rows_neither_pass_nor_fail_this_turn() -> None:
    """★ Both msg-5391 defects, at turn scope, against an unfiltering gateway."""
    foreign_good = _turn_row(1, trace="01FOREIGNTRACE000000000000")
    foreign_bad = _turn_row(2, trace="01FOREIGNTRACE000000000000", backend="anthropic")

    with pytest.raises(PreflightError, match="did not apply the filter"):
        await _attest_turn(_Ledger([[foreign_good]]), _Sleeps())

    record = await _attest_turn(_Ledger([[foreign_bad, _turn_row(3)]]), _Sleeps())
    assert record.probe == "cost-row#3"


def test_the_custom_headers_value_is_the_measured_name_colon_value_form() -> None:
    """D-0a (2026-10-02): CLI 2.1.133 sends ``Name: Value``; it rejects the JSON form."""
    assert TRACE_HEADER == "X-Mindwire-Trace"
    assert custom_headers_env_value("01ABC") == "X-Mindwire-Trace: 01ABC"


# --------------------------------------------------------------------------- #
# Live smoke (skipped in CI: addopts -m "not manual")
# --------------------------------------------------------------------------- #


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_live_preflight_against_the_real_gateway() -> None:
    """Run with ``uv run pytest -m manual -k live_preflight``.

    Costs one real naysayer completion and appends one row to the live ledger.
    Passes only against a gateway that records ``X-Mindwire-Trace`` into
    ``trace_id`` and applies ``?trace_id=`` — i.e. after the spirrow-lexora
    change for T-per-turn-backend-attestation is deployed. Before that it fails
    with "did not apply the filter", which is itself the check that the mindwire
    side must not be merged first.
    """
    import os

    base_url = os.environ.get("MINDWIRE_NAYSAYER_BASE_URL") or "http://localhost:8110"
    record = await attest_backend(
        base_url=base_url,
        tier=NAYSAYER_MODEL_TIER,
        expected=NAYSAYER_EXPECTED_BACKEND,
    )
    assert record.backend == NAYSAYER_EXPECTED_BACKEND
    assert record.probe.startswith("cost-row#")
