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
from spirrow_mindwire.naysayer.principles import (
    NAYSAYER_EXPECTED_BACKEND,
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
)
from spirrow_mindwire.value_objects import AttestationRecord

_ROUTE = "http://100.79.84.62:8110"
_TIER = "naysayer"
_EXPECTED = "gemini"
_NOW = datetime(2026, 8, 13, 4, 29, 54, tzinfo=UTC)

# MEASURED off the live gateway, never imported from the code under test — the
# same discipline the M5 block below states. Writing ``NAYSAYER_UPSTREAM_MODEL``
# here would make the fixture agree with the implementation by construction,
# which is precisely how the pre-2026-09-01 fixture went on passing while
# production had stopped working.
_LIVE_MODEL_ID = "gemini-3.1-pro-preview"
_LIVE_LIGHT_MODEL_ID = "Qwen3.8-27B"


def _row(
    row_id: int,
    *,
    tier: str | None = _TIER,
    model: str = _LIVE_MODEL_ID,
    backend: str = _EXPECTED,
) -> dict[str, Any]:
    """One ``/stats/costs/recent`` row, shaped like the live gateway's.

    Field names and types copied from a live read (2026-09-02T22:47:45Z, row
    8765): ``{"id": 8765, "timestamp": ..., "model": "gemini-3.1-pro-preview",
    "backend": "gemini", "endpoint": "/v1/chat/completions", "user_id": None,
    ..., "success": 1, "tier": "naysayer", "pricing_known": 0}``.

    **This shape is dated on purpose.** It replaces one copied on 2026-08-13
    (row 6032), which had no ``tier`` column and echoed the requested alias into
    ``model``. Lexora changed both on 2026-09-01T00:23Z. Because this fake was
    the only witness to the gateway's shape, the whole suite went on passing
    green while every design-time naysayer spawn in production failed — 1663
    tests proved only that the code agreed with a fixture, and the fixture had
    quietly stopped describing the world. See ``_old_shape_row`` below for the
    pin that makes a *return* to the old shape fail rather than pass.
    """
    return {
        "id": row_id,
        "timestamp": "2026-09-02T22:47:45.188094+00:00",
        "model": model,
        "backend": backend,
        "endpoint": "/v1/chat/completions",
        "user_id": None,
        "tokens_input": 2,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "duration_seconds": 1.96,
        "success": 1,
        "tier": tier,
        "pricing_known": 0,
    }


def _light_row(row_id: int) -> dict[str, Any]:
    """A row for a *different* tier, in the live shape (measured alongside 8765)."""
    return _row(row_id, tier="light", model=_LIVE_LIGHT_MODEL_ID, backend="light")


def _old_shape_row(row_id: int) -> dict[str, Any]:
    """A row written **before** 2026-09-01T00:23Z, as the endpoint renders it today.

    ``model`` carries the requested alias. That is the pre-switch shape and it
    is what a resurrected ``model`` fallback would match, so this fixture holds
    the deletion of the backward-compatible branch in place: such a selector
    would accept this row, and the test that feeds it would go green.

    ``tier`` and ``pricing_known`` are ``None``, **not absent** — measured
    2026-09-03 over ``/stats/costs/recent?limit=1000`` (ids 7778-8777, a window
    that spans the 8282/8283 cut). All 1000 rows carry an identical 13-key set,
    the 505 pre-switch rows included, because the endpoint renders every row it
    returns in the *current* schema; live row 7778 comes back as
    ``{"model": "naysayer", "backend": "gemini", "tier": None,
    "pricing_known": None, ...}``. **This gateway has no key-absent drift
    signature to produce.** An earlier revision of this fixture deleted the two
    keys, which taught the suite a failure mode production cannot reach — the
    same fake-agrees-with-code-and-neither-agrees-with-the-world defect the
    whole PR is about, reintroduced in the fixture written to fix it.
    """
    row = _row(row_id, tier=None, model=_TIER)
    row["pricing_known"] = None
    return row


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
        self.ledger: list[dict[str, Any]] = list(ledger or [_light_row(6031)])
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
    gateway = _FakeGateway(append_on_call=[[_light_row(6032)]] * 3)
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
# ★ The 2026-09-01 outage: which column identifies our row
#
# Lexora added a ``tier`` column and repurposed ``model`` to mean the resolved
# upstream model id. The selector still read ``model``, so it matched nothing,
# and every design-time naysayer spawn across four projects failed for 46 hours
# while CI stayed green — the fake gateway was the only witness to the row shape
# and it had been frozen on 2026-08-13.
#
# These tests bracket the fix from both sides: the live shape must attest, and
# the retired shape must NOT. Neither can go tautological, because the row
# values here are literals measured off the gateway rather than constants
# imported from the code under test.
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_row_is_selected_by_its_tier_column_not_its_model_column() -> None:
    """★ The live row's ``model`` is the upstream model id, not our tier alias.

    Point the selector back at ``model`` and this row stops being a candidate,
    the attempt raises, and this reds. That is the whole 2026-09-01 defect,
    executable.
    """
    gateway = _FakeGateway(append_on_call=[[_row(6032)]])
    record = await _attest(gateway)

    # The value the selector must NOT be keying on...
    assert gateway.ledger[-1]["model"] == "gemini-3.1-pro-preview"
    # ...and the value it must be keying on.
    assert gateway.ledger[-1]["tier"] == "naysayer"
    assert record.probe == "cost-row#6032"
    assert record.backend == "gemini"


@pytest.mark.anyio
async def test_the_outage_ledger_attests_with_concurrent_other_tier_traffic() -> None:
    """The exact shape of the last quarantined spawn, replayed.

    Live ids: baseline 8752, our probe wrote 8753 (``tier=naysayer``), and
    concurrent ``light`` traffic wrote 8754+. Only 8753 is ours, and the probe
    id must name it alone — the neighbours are neither candidates nor evidence.
    """
    gateway = _FakeGateway(
        ledger=[_light_row(8752)],
        append_on_call=[[_row(8753), _light_row(8754), _light_row(8755)]],
    )
    record = await _attest(gateway)
    assert record.probe == "cost-row#8753"


@pytest.mark.anyio
async def test_the_pre_schema_change_row_shape_is_not_matched() -> None:
    """★ No backward-compatible branch — and the deletion is held here.

    A row with ``tier`` null and ``model == "naysayer"`` is the shape the
    gateway last wrote at ledger id 8282 and has not written since. Accepting it
    would mean carrying a branch whose condition can never again be true. If
    anyone re-adds a ``model`` fallback, this test goes green-to-red the moment
    the fallback starts matching, which is the only warning the deletion gets.

    What is asserted is that the row was **seen and rejected**, not that it was
    absent — i.e. the attempt reached the missed branch. The message may not be
    asked to explain *why* the row is unselectable: that is the very claim the
    branch is forbidden to make (see the raise, and the standing test below).
    """
    gateway = _FakeGateway(
        append_on_call=[[_old_shape_row(8283)], [_old_shape_row(8284)], [_old_shape_row(8285)]]
    )
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    message = str(excinfo.value)
    assert "1 accounting row(s) appeared" in message
    assert "none carries tier 'naysayer'" in message
    assert "no accounting row at all" not in message


# --------------------------------------------------------------------------- #
# ★ The message has to accuse the right party (F-2)
#
# The old wording — "no accounting row for tier 'naysayer' appeared" — was
# FALSE during the outage. The rows appeared, were routed and were billed; only
# our selection missed them. Because the sentence blamed the gateway, every
# reader who acted on it went and looked at a gateway that was healthy, and
# stopped there. Distinguishing the two cases costs no new observation: the
# caller already holds both row sets.
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_gateway_that_wrote_no_row_at_all_says_exactly_that() -> None:
    gateway = _FakeGateway(append_on_call=[[], [], []])
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    message = str(excinfo.value)
    assert "no accounting row at all appeared" in message
    assert "a 2xx response is not evidence of routing" in message


@pytest.mark.anyio
async def test_rows_the_selector_missed_are_reported_with_their_shape() -> None:
    """★ The one-line diagnosis the outage did not get.

    When rows are present but none is selectable, the message must say so,
    count them, name the baseline it counted from, and show the shape it could
    not read — the column set (which names a schema change outright) and the
    first row's tier/model/backend. Those are all *observations*, true whichever
    of the branch's two states we are actually in.

    It must **not** say why, and this test no longer asks it to. It used to
    require "this host's row selector and the gateway's row shape disagree" and
    "the probe was served and billed" — both false in the state this fixture
    actually builds, where every row above the baseline is well-formed ``light``
    traffic and our own row was simply never written. The test was requiring the
    falsehood.
    """
    gateway = _FakeGateway(
        ledger=[_light_row(8752)],
        append_on_call=[
            [_light_row(8753), _light_row(8754)],
            [_light_row(8755), _light_row(8756)],
            [_light_row(8757), _light_row(8758)],
        ],
    )
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    message = str(excinfo.value)

    assert "2 accounting row(s) appeared" in message
    assert "none carries tier 'naysayer'" in message
    assert "baseline id 8756" in message
    # The shape, bounded: columns plus the first row's identifying triple.
    assert "'tier'" in message and "'model'" in message and "'backend'" in message
    # The third attempt's rows: the reported shape belongs to the attempt that
    # actually failed last, and names the earliest row above ITS baseline.
    assert "id=8757" in message
    assert "tier='light'" in message
    assert "model='Qwen3.8-27B'" in message
    assert "backend='light'" in message


@pytest.mark.anyio
async def test_the_missed_branch_asserts_nothing_the_observation_does_not_show() -> None:
    """★ THE STANDING TEST (msg-2400 C-3). An invariant, not a wording.

    The missed branch is reachable from two states and cannot tell them apart:
    our row is among the rows above the baseline and the selector cannot
    identify it, or our row is not there at all and those rows are other
    traffic. Nothing the caller holds decides which — there is no key joining a
    ``ChatCompletion`` to a ledger row. So the branch must assert **neither**.

    That is pinned twice, because either half alone is weak:

    (1) *Negative, wording-keyed.* In the no-drift world — our row never
        written, every row above the baseline well-formed ``light`` traffic —
        the message must not claim a shape disagreement and must not claim our
        probe's row was billed. Both are false in this world. **This is the
        assertion that is RED against 2ceb2fc**, whose message asserted both.
        It can only ban phrasings already thought of, hence (2).

    (2) *Structural.* The prose is byte-identical across both worlds; only the
        bounded ``_observed_shape`` projection may differ (the row counts and
        baselines are held equal here so nothing else can). A world-specific
        claim cannot be added without a discriminator, and a discriminator
        would break this equality. msg-2400 §4 measured that no such
        discriminator exists: the gate proposed ``"tier" not in keys``, and all
        1000 rows in a window spanning the schema cut carry the ``tier`` key.

    Note what is deliberately *not* banned: the bare words "schema" or "drift".
    C-1 requires the message to **name** both candidate explanations, and one
    of them is drift. Banning the topic would forbid the wording C-1 mandates.
    What is banned is the assertive form. The gap between "asserts X" and
    "mentions X as one of two possibilities" is not decidable by substring, and
    (2) is what covers it.
    """
    # Same ids, same counts, same baselines in both worlds — so any difference
    # in the prose can only have come from a claim about the world.
    attempts = [(9001, 9002), (9003, 9004), (9005, 9006)]

    # World A: our row was never written. These rows are other tiers', and
    # perfectly well-formed — there is no drift here to diagnose.
    no_drift = _FakeGateway(append_on_call=[[_light_row(a), _light_row(b)] for a, b in attempts])
    with pytest.raises(PreflightError) as world_a:
        await _attest(no_drift)
    message = str(world_a.value)

    for claim in (
        "row shape disagree",
        "shapes disagree",
        "served and billed",
        "was billed",
        "were billed",
    ):
        assert claim not in message, f"asserts {claim!r}, which is false in this world"

    # World B: our row IS above the baseline, but its tier moved out from under
    # the selector. This is the drift the other explanation names.
    drifted = _FakeGateway(
        append_on_call=[[_row(a, tier="naysayer-v2"), _light_row(b)] for a, b in attempts]
    )
    with pytest.raises(PreflightError) as world_b:
        await _attest(drifted)

    marker = "observed columns"
    assert marker in message and marker in str(world_b.value)
    assert message.split(marker)[0] == str(world_b.value).split(marker)[0]
    # ...and the projection, which is observation, does differ between them.
    assert message.split(marker)[1] != str(world_b.value).split(marker)[1]


@pytest.mark.anyio
async def test_only_the_last_attempt_s_failure_reaches_the_caller() -> None:
    """A residual, pinned here so it is found now rather than during an outage.

    ``attest_backend`` reports ``last`` — the final attempt's exception — so if
    an earlier attempt produced the informative "rows appeared but none carries
    tier X" and the final one produced the bare "no row at all", the diagnostic
    is the one that survives. This does **not** weaken the fix for the failure
    it was written for: a schema drift makes every attempt take the informative
    branch, because every probe is served and every probe writes a row. It bites
    only when the gateway is *also* intermittently writing nothing. Widening it
    (keeping the most informative failure, or refusing to retry a selector
    mismatch at all) changes retry semantics and is deliberately not done here.
    """
    gateway = _FakeGateway(
        # attempt 1 writes an unselectable row; attempts 2 and 3 write nothing.
        append_on_call=[[_light_row(6032)], [], []],
    )
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    assert "no accounting row at all appeared" in str(excinfo.value)
    assert "none carries tier" not in str(excinfo.value)


@pytest.mark.anyio
async def test_the_two_empty_candidate_failures_do_not_share_a_message() -> None:
    """They are one failure operationally and two facts about the world.

    Same exception type deliberately — :class:`PreflightError`'s docstring
    refuses a taxonomy a caller could act on, and nothing here gives one. What
    changes is only what a human reads.
    """
    nothing_written = _FakeGateway(append_on_call=[[], [], []])
    with pytest.raises(PreflightError) as empty:
        await _attest(nothing_written)

    unselectable = _FakeGateway(
        append_on_call=[[_light_row(6032)], [_light_row(6033)], [_light_row(6034)]]
    )
    with pytest.raises(PreflightError) as missed:
        await _attest(unselectable)

    assert type(empty.value) is type(missed.value) is PreflightError
    assert str(empty.value) != str(missed.value)
    # The empty case may say nothing appeared; the other case must not.
    assert "no accounting row at all" not in str(missed.value)
    # What the missed branch owes the reader, all of it observation: how many
    # rows it saw, the baseline it counted from, and their shape.
    assert "1 accounting row(s) appeared" in str(missed.value)
    assert "baseline id 6033" in str(missed.value)
    assert "observed columns" in str(missed.value)


@pytest.mark.anyio
async def test_the_failure_message_does_not_dump_whole_rows() -> None:
    """The projection is bounded on purpose.

    These strings are quoted verbatim into durable chatroom posts. ``user_id``
    is in the row today and something worse may be tomorrow; the diagnostic
    needs the column *names*, never every column's value.
    """

    def _row_with_a_secret(row_id: int) -> dict[str, Any]:
        row = _light_row(row_id)
        row["user_id"] = "sgadmin@example.internal"
        return row

    gateway = _FakeGateway(
        append_on_call=[
            [_row_with_a_secret(6032)],
            [_row_with_a_secret(6033)],
            [_row_with_a_secret(6034)],
        ]
    )
    with pytest.raises(PreflightError) as excinfo:
        await _attest(gateway)
    message = str(excinfo.value)
    assert "'user_id'" in message  # the column is named...
    assert "sgadmin@example.internal" not in message  # ...its value is not.


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
# ★ M5 (Tier-C msg-970 §3) — the PRODUCTION constants, exercised in CI
#
# Everything above this line drives ``attest_backend`` with the module's own
# ``_TIER`` / ``_EXPECTED`` literals, so the suite never touches the values the
# daemon actually spawns with. That gap is what let a mutation of
# ``NAYSAYER_EXPECTED_BACKEND`` survive 1109 green tests on PR #143.
#
# These two tests close it from both sides, and neither is deselected:
# the ledger row's ``backend`` is the hardcoded value MEASURED off the live
# gateway (row 6032), never a constant under test, so the assertions cannot go
# tautological the way the manual smoke test's did.
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_production_constants_attest_against_a_live_shaped_row() -> None:
    """The values the daemon spawns with must attest a row shaped like the real one.

    Mutate ``NAYSAYER_EXPECTED_BACKEND`` to the upstream model id — the tidy-up
    that looks like de-duplication — and the row saying ``backend: "gemini"``
    stops matching, ``attest_backend`` raises, and this reds. In production the
    same mutation reds nothing until the first spawn, where it takes the daemon
    down and quarantines the candidate.
    """
    gateway = _FakeGateway(
        ledger=[_light_row(6031)],
        # Literals, deliberately: this is what the live gateway wrote, not what
        # the code under test expects it to have written.
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
    """The other bracket: WHY the two constants must stay distinct.

    The row names the backend family. Asking it for the model id asks for a fact
    it does not carry, so the comparison can never succeed — on any row, for any
    request. Pinning the consequence here means the reason the values differ is
    executable, not just a comment someone may delete along with the difference.
    """
    gateway = _FakeGateway(
        ledger=[_light_row(6031)],
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
# Live smoke (skipped in CI: addopts -m "not manual")
# --------------------------------------------------------------------------- #


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_live_preflight_against_the_real_gateway() -> None:
    """Run with ``uv run pytest -m manual -k live_preflight``.

    Costs one real naysayer completion and appends one row to the live ledger.

    This test is **deselected in CI** and its ``record.backend ==
    NAYSAYER_EXPECTED_BACKEND`` assertion is a tautology on success (``_attempt``
    returns ``expected``), so what it really checks is "the live gateway did not
    make us raise". That is the right job for a live smoke test and the wrong
    place to pin a constant — the pin lives above, in
    ``test_production_constants_attest_against_a_live_shaped_row`` (Tier-C
    msg-970 §3 / M5).
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
