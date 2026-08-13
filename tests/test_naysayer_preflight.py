"""P-2 — spawn-time preflight attestation (msg-953 §3 / Tier-C msg-954 §3 / msg-965).

**The one idea under test: do not ask the model, read the server's own
accounting record back.** msg-953 §1.3 measured why — the same Gemini backend
answered "I am part of the Claude model family" under one system prompt and "I
am a member of the Gemini model family" under another, while the cost row said
``backend: gemini`` both times. Self-report is steerable (by *our* system
prompt); the accounting row is the one thing in the loop the model cannot
write. **The row wins.**

So the preflight is two steps, and the second one is the whole point:

1. one **non-streaming** request to the configured route, at the configured tier
2. ``GET /stats/costs/recent`` — read back the row the gateway wrote for it

Everything here is exercised against a fake gateway. One
``@pytest.mark.manual`` smoke test at the bottom runs the real thing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.lexora.client import (
    ChatCompletion,
    ChatMessage,
    LexoraHTTPError,
    LexoraTimeoutError,
)
from spirrow_mindwire.naysayer.preflight import (
    PREFLIGHT_ATTEMPTS,
    PreflightError,
    attest_backend,
)
from spirrow_mindwire.value_objects import AttestationRecord

_ROUTE = "http://100.79.84.62:8110"
_TIER = "naysayer"
_EXPECTED = "gemini"
_NOW = datetime(2026, 8, 13, 4, 29, 54, tzinfo=UTC)


def _row(row_id: int, *, model: str = _TIER, backend: str = _EXPECTED) -> dict[str, Any]:
    """One ``/stats/costs/recent`` row, shaped like the live gateway's.

    Field names and types copied from a live read (2026-08-13, row 6032):
    ``{"id": 6032, "timestamp": ..., "model": "naysayer", "backend": "gemini",
    "endpoint": "/v1/chat/completions", ..., "success": 1}``.
    """
    return {
        "id": row_id,
        "timestamp": "2026-08-13T04:29:54.899164+00:00",
        "model": model,
        "backend": backend,
        "endpoint": "/v1/chat/completions",
        "user_id": None,
        "tokens_input": 2,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "duration_seconds": 1.96,
        "success": 1,
    }


class _FakeGateway:
    """Fake Lexora: a growing ledger that ``chat_completion`` appends to.

    ``append_on_call`` is what each completion adds to the ledger, so a test can
    make the probe land a mismatching row, several rows, or none at all.
    """

    def __init__(
        self,
        *,
        ledger: list[dict[str, Any]] | None = None,
        append_on_call: list[list[dict[str, Any]]] | None = None,
        raise_on_call: list[Exception | None] | None = None,
        completion_content: str = "pong",
    ) -> None:
        self.ledger: list[dict[str, Any]] = list(
            ledger or [_row(6031, model="light", backend="light")]
        )
        self._append_on_call = list(append_on_call or [[_row(6032)]])
        self._raise_on_call = list(raise_on_call or [])
        self._completion_content = completion_content
        self.calls: list[dict[str, Any]] = []
        self.stats_reads = 0

    async def chat_completion(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion:
        index = len(self.calls)
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        if index < len(self._append_on_call):
            self.ledger.extend(self._append_on_call[index])
        if index < len(self._raise_on_call) and self._raise_on_call[index] is not None:
            raise self._raise_on_call[index]  # type: ignore[misc]
        return ChatCompletion(
            content=self._completion_content,
            reasoning_content=None,
            finish_reason="length",
            model="gemini-3.1-pro-preview",
            usage={},
            raw={},
        )

    async def stats_costs_recent(self, *, limit: int) -> list[dict[str, Any]]:
        self.stats_reads += 1
        return list(reversed(self.ledger))[:limit]


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
    assert record.at == _NOW
    # The probe is non-streaming and goes to the configured tier: that is what
    # makes the gateway write a row at all (msg-953 §1.4 — streaming leaves no
    # accounting trace, so a streaming probe could attest nothing).
    assert gateway.calls[0]["model"] == "naysayer"


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
    want — and each retry is another billed request. Only transport-level
    failures and a missing row are retried.
    """
    gateway = _FakeGateway(
        append_on_call=[[_row(6032, backend="vllm")], [_row(6033)], [_row(6034)]]
    )
    with pytest.raises(PreflightError):
        await _attest(gateway)
    assert len(gateway.calls) == 1


@pytest.mark.anyio
async def test_no_matching_row_fails_closed() -> None:
    """The probe returned 200 but the gateway recorded nothing for our tier.

    Attesting on the strength of a successful HTTP response alone would be
    attesting the response body — exactly what this design refuses to do.
    """
    gateway = _FakeGateway(append_on_call=[[], [], []])
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    assert "no accounting row" in str(excinfo.value)


@pytest.mark.anyio
async def test_row_for_another_tier_is_not_a_match() -> None:
    gateway = _FakeGateway(append_on_call=[[_row(6032, model="light", backend="light")]] * 3)
    with pytest.raises(PreflightError):
        await _attest(gateway)


@pytest.mark.anyio
async def test_split_candidates_fail_closed() -> None:
    """Concurrent naysayer traffic can put more than one row above the baseline.

    msg-953 §3: "候補が複数なら全候補の backend が expected であることを要求".
    One matching row does not license ignoring a non-matching sibling — that is
    how a fallback backend would hide.
    """
    gateway = _FakeGateway(append_on_call=[[_row(6032), _row(6033, backend="vllm")]])
    with pytest.raises(PreflightError):
        await _attest(gateway)


@pytest.mark.anyio
async def test_all_matching_candidates_are_recorded_in_the_probe_id() -> None:
    gateway = _FakeGateway(append_on_call=[[_row(6032), _row(6033)]])
    record = await _attest(gateway)
    assert record.probe == "cost-row#6032+6033"


@pytest.mark.anyio
async def test_a_failed_row_still_counts_as_a_candidate() -> None:
    """``success=0`` rows are NOT filtered out — and that is deliberate.

    Residual hole (a) (msg-954 §6): the live ``fallback_backends`` config has
    never been read. The design does not assume fallback is impossible, it
    *observes the backend every time*. A fallback would show up as an extra row
    attributed to another backend; dropping unsuccessful rows would be dropping
    the very evidence the hole is watched with.
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
    """A 1-shot fail-closed preflight would park the loop on any transient 502
    (T36). An unbounded one would hammer a down gateway. Three (msg-954 §3).
    """
    assert PREFLIGHT_ATTEMPTS == 3
    gateway = _FakeGateway(
        raise_on_call=[LexoraTimeoutError("timed out")] * 5,
        append_on_call=[[]] * 5,
    )
    with pytest.raises(PreflightError):
        await _attest(gateway)
    assert len(gateway.calls) == PREFLIGHT_ATTEMPTS


@pytest.mark.anyio
async def test_each_attempt_takes_a_fresh_baseline() -> None:
    """Attempt N must not inherit attempt N-1's rows as candidates.

    A 502'd attempt can still have left a row. Carrying the first baseline
    forward would make that corpse a candidate for every later attempt, so a
    single bad row would poison every retry and the bounded retry would be
    decorative.
    """
    gateway = _FakeGateway(
        raise_on_call=[LexoraHTTPError("502"), None],
        append_on_call=[[_row(6032, backend="vllm")], [_row(6033)]],
    )
    record = await _attest(gateway)
    assert record.probe == "cost-row#6033"


@pytest.mark.anyio
async def test_empty_reply_body_does_not_fail_the_attestation() -> None:
    """★ Immunity to the ``max_tokens`` trap, measured twice.

    Gemini is a thinking model: with a small budget the deliberation eats it and
    the reply comes back EMPTY (live, 2026-08-13: ``finish_reason="length"``,
    ``completion_tokens=0``, ``content=""``) while the gateway still records
    ``backend: gemini``. Any oracle that read the body would break on the token
    budget. This one does not read the body at all.
    """
    gateway = _FakeGateway(completion_content="")
    record = await _attest(gateway)
    assert record.backend == "gemini"


# --------------------------------------------------------------------------- #
# Live smoke (skipped in CI: addopts -m "not manual")
# --------------------------------------------------------------------------- #


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_live_preflight_against_the_real_gateway() -> None:
    """Run with ``uv run pytest -m manual -k live_preflight``.

    Costs one real naysayer completion and appends one row to the live ledger.
    """
    import os

    from spirrow_mindwire.naysayer.principles import (
        NAYSAYER_EXPECTED_BACKEND,
        NAYSAYER_MODEL_TIER,
    )

    base_url = os.environ.get("MINDWIRE_NAYSAYER_BASE_URL") or "http://localhost:8110"
    record = await attest_backend(
        base_url=base_url,
        tier=NAYSAYER_MODEL_TIER,
        expected=NAYSAYER_EXPECTED_BACKEND,
    )
    assert record.backend == NAYSAYER_EXPECTED_BACKEND
    assert record.probe.startswith("cost-row#")
