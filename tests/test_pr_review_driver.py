"""Tests for the ``NaysayerPrReviewDriver`` (ADR-2026-06-04-19 driver-化 unify, ex-T20 adapter).

Fake Lexora + fake GitHub clients exercise the review flow (CI-gate → diff fetch → critique →
post + GitHub submit), verdict parsing, and the fail-closed paths. The driver reviews a given
``PrRef`` directly (the orchestrator decides *whether* to fire), so the old message-parsing /
session tests are gone — what remains is the deterministic-guard + judging behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest

from spirrow_mindwire.github.client import (
    CiState,
    CiStatus,
    GitHubClient,
    GitHubHTTPError,
    PrRef,
    ReviewEvent,
    ReviewInfo,
)
from spirrow_mindwire.lexora.client import (
    LEXORA_BACKEND_TIMEOUT_SECONDS,
    ChatCompletion,
    ChatMessage,
    LexoraHTTPError,
    LexoraTimeoutError,
)
from spirrow_mindwire.naysayer.pr_review import (
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_DIFF_CHARS,
    NaysayerPrReviewDriver,
    NaysayerPrReviewError,
    PostCritique,
    _parse_verdict,
    _resolve_verdict,
)
from spirrow_mindwire.naysayer.principles import build_preamble, principles_version


class _FakeLexora:
    def __init__(
        self,
        *,
        content: str | None = "looks risky\n\nVERDICT: REQUEST_CHANGES",
        finish_reason: str = "stop",
        raise_exc: Exception | None = None,
    ) -> None:
        self._content = content
        self._fr = finish_reason
        self._raise = raise_exc
        self.calls: list[tuple[str, list[ChatMessage], int]] = []

    async def chat_completion(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion:
        self.calls.append((model, messages, max_tokens))
        if self._raise is not None:
            raise self._raise
        return ChatCompletion(
            content=self._content or "",
            reasoning_content="...",
            finish_reason=self._fr,
            model="DeepSeek-V4-Flash",
            usage={"total_tokens": 10},
        )

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def aclose(self) -> None:
        return None


class _FakeGitHub:
    def __init__(
        self,
        *,
        diff: str = "diff --git a/x b/x\n+added",
        fetch_exc: Exception | None = None,
        submit_exc: Exception | None = None,
        ci: CiStatus | None = None,
        reviews: list[ReviewInfo] | None = None,
    ) -> None:
        self._diff = diff
        self._fetch_exc = fetch_exc
        self._submit_exc = submit_exc
        self._ci = ci if ci is not None else CiStatus(CiState.SUCCESS, "sha-default", [])
        self._reviews = list(reviews) if reviews is not None else []
        self.fetched: list[PrRef] = []
        self.submitted: list[tuple[PrRef, ReviewEvent, str]] = []

    async def fetch_pr_diff(self, pr: PrRef) -> str:
        self.fetched.append(pr)
        if self._fetch_exc is not None:
            raise self._fetch_exc
        return self._diff

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:
        return self._ci

    async def fetch_pr_reviews(self, pr: PrRef) -> list[ReviewInfo]:
        return list(self._reviews)

    async def submit_review(self, pr: PrRef, *, event: ReviewEvent, body: str) -> dict[str, Any]:
        # Model the same-identity 422: the verdict event fails, but a COMMENT review (the
        # fallback) succeeds. submit_exc=None → always succeeds.
        if self._submit_exc is not None and event is not ReviewEvent.COMMENT:
            raise self._submit_exc
        self.submitted.append((pr, event, body))
        return {"id": 1, "state": event.value}

    async def aclose(self) -> None:
        return None


def _pr() -> PrRef:
    return PrRef("spirrowgames", "spirrow-mindwire", 42)


def _capture() -> tuple[list[str], PostCritique]:
    posted: list[str] = []

    async def post(body: str) -> None:
        posted.append(body)

    return posted, post


# ---------- happy path ---------------------------------------------------- #


@pytest.mark.anyio
async def test_request_changes_flow() -> None:
    lexora = _FakeLexora(content="line 3 is wrong\n\nVERDICT: REQUEST_CHANGES")
    github = _FakeGitHub()
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    outcome = await driver.review(_pr(), post_critique=post)

    assert github.fetched == [PrRef("spirrowgames", "spirrow-mindwire", 42)]
    assert len(posted) == 1
    assert "VERDICT: REQUEST_CHANGES" in posted[0]
    assert outcome.verdict is ReviewEvent.REQUEST_CHANGES
    assert len(github.submitted) == 1
    _pr_arg, event, body = github.submitted[0]
    assert event is ReviewEvent.REQUEST_CHANGES
    assert "VERDICT" in body


# ---------- debounce (cost lever): head-unchanged skip + round cap --------- #


@pytest.mark.anyio
async def test_skip_if_head_unchanged_reuses_prior_verdict_without_lexora() -> None:
    # The naysayer already reviewed THIS head (its last verdict review's commit_id == ci.head_sha)
    # → skip the Lexora/Gemini call and reuse the prior verdict; no new GitHub review submitted.
    lexora = _FakeLexora()
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "headsha", "2026-06-10T00:00:00Z"),
        ],
    )
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github, skip_if_head_unchanged=True)
    outcome = await driver.review(_pr(), post_critique=post)

    assert lexora.calls == []  # no Gemini call
    assert github.submitted == []  # the existing review stands; no duplicate
    assert outcome.skipped_head_unchanged is True
    assert outcome.verdict is ReviewEvent.REQUEST_CHANGES
    assert len(posted) == 1  # a short note is still posted to the thread


@pytest.mark.anyio
async def test_skip_if_head_unchanged_does_not_skip_when_head_moved() -> None:
    # The prior naysayer review was against a different commit → a full review runs.
    lexora = _FakeLexora(content="x\n\nVERDICT: APPROVE")
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "newsha", []),
        reviews=[
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "oldsha", "2026-06-10T00:00:00Z"),
        ],
    )
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github, skip_if_head_unchanged=True)
    outcome = await driver.review(_pr(), post_critique=post)

    assert len(lexora.calls) == 1  # head moved → full review
    assert outcome.skipped_head_unchanged is False


@pytest.mark.anyio
async def test_review_round_cap_escalates_to_comment_without_lexora() -> None:
    # Once the naysayer has submitted >= the cap, the gate short-circuits to a COMMENT (which routes
    # the conductor to the human Tier-C) instead of spending another Gemini review.
    lexora = _FakeLexora()
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "s1", "2026-06-10T00:00:01Z"),
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "s2", "2026-06-10T00:00:02Z"),
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "s3", "2026-06-10T00:00:03Z"),
        ],
    )
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github, max_review_rounds=3)
    outcome = await driver.review(_pr(), post_critique=post)

    assert lexora.calls == []  # cap hit → no Gemini call
    assert outcome.rounds_capped is True
    assert outcome.verdict is ReviewEvent.COMMENT
    assert [event for _, event, _ in github.submitted] == [ReviewEvent.COMMENT]  # GitHub trail
    assert len(posted) == 1


@pytest.mark.anyio
async def test_debounce_counts_only_naysayer_login() -> None:
    # Reviews by other logins (Copilot, the author) and non-verdict states do not trigger the skip
    # or the cap — only the naysayer's own verdict reviews do.
    lexora = _FakeLexora(content="x\n\nVERDICT: APPROVE")
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[
            ReviewInfo("copilot", "COMMENTED", "headsha", "2026-06-10T00:00:01Z"),
            ReviewInfo("takahito-spirrowgames", "COMMENTED", "headsha", "2026-06-10T00:00:02Z"),
        ],
    )
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(
        lexora=lexora, github=github, skip_if_head_unchanged=True, max_review_rounds=1
    )
    outcome = await driver.review(_pr(), post_critique=post)

    assert len(lexora.calls) == 1  # no naysayer-owned reviews → full review
    assert outcome.skipped_head_unchanged is False
    assert outcome.rounds_capped is False


@pytest.mark.anyio
async def test_round_cap_counts_only_verdict_reviews() -> None:
    # Non-verdict reviews by the naysayer login (a CI-gate-hold COMMENT, a DISMISSED review, the
    # escalation COMMENT itself) must NOT count toward the cap — only APPROVED / CHANGES_REQUESTED
    # do. Counting COMMENTs would prematurely and then permanently escalate the gate (Copilot +
    # naysayer review on #113).
    lexora = _FakeLexora(content="x\n\nVERDICT: APPROVE")
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "s1", "2026-06-10T00:00:01Z"),
            ReviewInfo("spirrowgames-ops", "COMMENTED", "s2", "2026-06-10T00:00:02Z"),
            ReviewInfo("spirrowgames-ops", "DISMISSED", "s3", "2026-06-10T00:00:03Z"),
        ],
    )
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github, max_review_rounds=2)
    outcome = await driver.review(_pr(), post_critique=post)

    # Only 1 verdict review (CHANGES_REQUESTED) < cap 2 → NOT capped; a full review runs.
    assert len(lexora.calls) == 1
    assert outcome.rounds_capped is False


@pytest.mark.anyio
async def test_shadow_mode_measures_skip_without_acting() -> None:
    # shadow=True: the head-unchanged skip is computed + recorded (would_skip_head_unchanged) but
    # NOT acted on — the full Gemini review still runs (no behaviour / coverage change).
    lexora = _FakeLexora(content="x\n\nVERDICT: APPROVE")
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "headsha", "2026-06-10T00:00:00Z"),
        ],
    )
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(
        lexora=lexora, github=github, skip_if_head_unchanged=True, shadow=True
    )
    outcome = await driver.review(_pr(), post_critique=post)

    assert len(lexora.calls) == 1  # full review ran (NOT skipped)
    assert outcome.would_skip_head_unchanged is True
    assert outcome.skipped_head_unchanged is False


@pytest.mark.anyio
async def test_shadow_mode_measures_cap_without_acting() -> None:
    # shadow=True: the round cap is computed + recorded (would_cap) but NOT acted on — the full
    # review runs and is submitted, instead of a cap-escalation COMMENT.
    lexora = _FakeLexora(content="x\n\nVERDICT: APPROVE")
    github = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "s1", "2026-06-10T00:00:01Z"),
            ReviewInfo("spirrowgames-ops", "CHANGES_REQUESTED", "s2", "2026-06-10T00:00:02Z"),
        ],
    )
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github, max_review_rounds=2, shadow=True)
    outcome = await driver.review(_pr(), post_critique=post)

    assert len(lexora.calls) == 1  # full review ran (NOT capped)
    assert outcome.would_cap is True
    assert outcome.rounds_capped is False
    assert [event for _, event, _ in github.submitted] == [ReviewEvent.APPROVE]


@pytest.mark.anyio
async def test_approve_flow() -> None:
    lexora = _FakeLexora(content="all good\n\nVERDICT: APPROVE")
    github = _FakeGitHub()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    outcome = await driver.review(_pr(), post_critique=post)
    assert github.submitted[0][1] is ReviewEvent.APPROVE
    assert outcome.verdict is ReviewEvent.APPROVE


@pytest.mark.anyio
async def test_ambiguous_verdict_defaults_to_request_changes() -> None:
    lexora = _FakeLexora(content="some comments without a verdict line")
    github = _FakeGitHub()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    await driver.review(_pr(), post_critique=post)
    assert github.submitted[0][1] is ReviewEvent.REQUEST_CHANGES


@pytest.mark.anyio
async def test_lexora_called_with_naysayer_tier_and_budget() -> None:
    lexora = _FakeLexora()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=_FakeGitHub())
    await driver.review(_pr(), post_critique=post)
    model, _messages, max_tokens = lexora.calls[0]
    assert model == "naysayer"
    assert max_tokens >= 8000  # reasoning-model floor (4096 truncated the critique)


@pytest.mark.anyio
async def test_lexora_system_message_injects_principles_preamble() -> None:
    # ADR-17 D-1 / ADR-19 single judging-behavior core: the PR-gate injects the 5-principles SOT
    # verbatim via the SAME build_preamble() entry point the design-time agent uses.
    lexora = _FakeLexora()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=_FakeGitHub())
    await driver.review(_pr(), post_critique=post)
    _model, messages, _max = lexora.calls[0]
    assert messages[0].role == "system"
    system = messages[0].content
    assert build_preamble() in system  # whole SOT, verbatim
    assert "silence is negligence" in system
    assert "VERDICT: APPROVE" in system  # PR-review task instructions still follow it


@pytest.mark.anyio
async def test_outcome_records_principles_version() -> None:
    lexora = _FakeLexora(content="all good\n\nVERDICT: APPROVE")
    github = _FakeGitHub(ci=CiStatus(CiState.SUCCESS, "sha7", []))
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    outcome = await driver.review(_pr(), post_critique=post)
    assert outcome.principles_version == principles_version()


# ---------- L1 CI-gate (ADR-2026-06-03-16) -------------------------------- #


@pytest.mark.anyio
async def test_ci_failure_short_circuits_request_changes_without_lexora() -> None:
    lexora = _FakeLexora()
    github = _FakeGitHub(ci=CiStatus(CiState.FAILURE, "sha9", ["test"]))
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    outcome = await driver.review(_pr(), post_critique=post)

    assert lexora.calls == []  # gated before the (costly) content review
    assert github.submitted[0][1] is ReviewEvent.REQUEST_CHANGES
    assert "test" in github.submitted[0][2]  # failing check named in the body
    assert outcome.ci_state is CiState.FAILURE
    assert outcome.head_sha == "sha9"  # L4
    assert outcome.ci_gated is True
    assert len(posted) == 1


@pytest.mark.anyio
async def test_ci_pending_holds_with_comment_no_approve() -> None:
    github = _FakeGitHub(ci=CiStatus(CiState.PENDING, "sha9", []))
    lexora = _FakeLexora()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    await driver.review(_pr(), post_critique=post)
    assert lexora.calls == []
    assert github.submitted[0][1] is ReviewEvent.COMMENT  # held, not approved


@pytest.mark.anyio
async def test_ci_unknown_fail_closed_never_approves() -> None:
    # Even if the model *would* approve, an unobtainable CI state blocks APPROVE.
    lexora = _FakeLexora(content="all good\n\nVERDICT: APPROVE")
    github = _FakeGitHub(ci=CiStatus(CiState.UNKNOWN, None, []))
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    await driver.review(_pr(), post_critique=post)
    assert lexora.calls == []  # never reaches the model
    assert github.submitted[0][1] is ReviewEvent.COMMENT  # held, never APPROVE


@pytest.mark.anyio
async def test_ci_success_proceeds_to_content_review_and_records_head_sha() -> None:
    lexora = _FakeLexora(content="all good\n\nVERDICT: APPROVE")
    github = _FakeGitHub(ci=CiStatus(CiState.SUCCESS, "sha7", []))
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    outcome = await driver.review(_pr(), post_critique=post)
    assert lexora.calls != []  # CI green → model WAS consulted
    assert github.submitted[0][1] is ReviewEvent.APPROVE
    assert outcome.head_sha == "sha7"  # L4
    assert outcome.ci_state is CiState.SUCCESS
    assert outcome.ci_gated is False


# ---------- fail-closed --------------------------------------------------- #


@pytest.mark.anyio
async def test_github_fetch_unreachable_fails_closed() -> None:
    github = _FakeGitHub(fetch_exc=RuntimeError("github down"))
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=_FakeLexora(), github=github)
    with pytest.raises(RuntimeError):
        await driver.review(_pr(), post_critique=post)
    assert posted == []  # nothing posted
    assert github.submitted == []


@pytest.mark.anyio
async def test_lexora_empty_review_fails_loud() -> None:
    lexora = _FakeLexora(content="", finish_reason="length")
    github = _FakeGitHub()
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    with pytest.raises(NaysayerPrReviewError):
        await driver.review(_pr(), post_critique=post)
    assert posted == []
    assert github.submitted == []  # no review submitted on empty critique


@pytest.mark.anyio
async def test_github_submit_failure_after_post_fails_loud() -> None:
    # The critique is posted first, then the GH submit fails (non-422) → propagates.
    lexora = _FakeLexora(content="bug here\n\nVERDICT: REQUEST_CHANGES")
    github = _FakeGitHub(submit_exc=RuntimeError("submit 500"))
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    with pytest.raises(RuntimeError):
        await driver.review(_pr(), post_critique=post)
    assert len(posted) == 1  # critique already posted to the thread


# ---------- M2 (T34): timeout degrades to fail-closed REQUEST_CHANGES ----- #


@pytest.mark.anyio
async def test_lexora_timeout_degrades_to_request_changes_not_raise() -> None:
    # M4 (i): a LexoraTimeoutError from the content review must NOT crash the pipeline. It degrades
    # to a fail-closed REQUEST_CHANGES via the same post_critique + GitHub submit path, and the
    # outcome records timed_out=True.
    lexora = _FakeLexora(raise_exc=LexoraTimeoutError("POST /v1/chat/completions timed out"))
    github = _FakeGitHub(ci=CiStatus(CiState.SUCCESS, "sha-to", []))
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)

    outcome = await driver.review(_pr(), post_critique=post)

    assert lexora.calls != []  # the model WAS consulted (CI was green) — it timed out
    assert len(posted) == 1  # explanatory critique posted to the thread
    # The body is generic about the timeout (no specific seconds number): a DI'd Lexora may carry a
    # different timeout than the driver default, so the body must not assert a concrete value.
    assert "timeout" in posted[0].lower()
    assert "VERDICT: REQUEST_CHANGES" in posted[0]
    assert len(github.submitted) == 1
    _pr_arg, event, _body = github.submitted[0]
    assert event is ReviewEvent.REQUEST_CHANGES  # GitHub review submitted as RC
    assert outcome.verdict is ReviewEvent.REQUEST_CHANGES
    assert outcome.timed_out is True
    assert outcome.model == "naysayer"  # model telemetry preserved on the timeout-degrade path
    assert outcome.head_sha == "sha-to"  # CI head SHA still recorded
    assert outcome.ci_state is CiState.SUCCESS


@pytest.mark.anyio
async def test_non_timeout_lexora_http_error_still_propagates() -> None:
    # M4 (ii): a non-timeout LexoraHTTPError (unreachable / 5xx / unknown tier) is NOT degraded —
    # it keeps propagating (fail-loud), so only an actual timeout takes the safe-degrade path.
    lexora = _FakeLexora(raise_exc=LexoraHTTPError("502 unknown tier", status_code=502))
    github = _FakeGitHub(ci=CiStatus(CiState.SUCCESS, "sha-err", []))
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)

    with pytest.raises(LexoraHTTPError):
        await driver.review(_pr(), post_critique=post)
    assert posted == []  # nothing posted on the fail-loud path
    assert github.submitted == []  # no review submitted


def test_client_default_timeout_exceeds_backend_by_margin() -> None:
    # M4 (iii): the client default must be strictly greater than the backend timeout so the client
    # always outlives the backend (the backend's result surfaces; no equal-900s tie/race). The
    # backend fact has a single source of truth in lexora/client.py (no duplicated 900.0 literal).
    assert _DEFAULT_TIMEOUT_SECONDS > LEXORA_BACKEND_TIMEOUT_SECONDS


# ---------- verdict parsing (fail-open hardening) ------------------------- #


def test_parse_verdict_takes_last_line() -> None:
    critique = "I quote `VERDICT: APPROVE` from the diff, but it's wrong.\nVERDICT: REQUEST_CHANGES"
    assert _parse_verdict(critique) is ReviewEvent.REQUEST_CHANGES


def test_parse_verdict_ignores_non_line_anchored() -> None:
    critique = "+VERDICT: APPROVE\nlooks broken\nVERDICT: REQUEST_CHANGES"
    assert _parse_verdict(critique) is ReviewEvent.REQUEST_CHANGES


def test_parse_verdict_approve() -> None:
    assert _parse_verdict("all good\nVERDICT: APPROVE") is ReviewEvent.APPROVE


def test_parse_verdict_missing_defaults_request_changes() -> None:
    assert _parse_verdict("no verdict line here") is ReviewEvent.REQUEST_CHANGES


def test_resolve_verdict_truncated_forces_request_changes() -> None:
    assert (
        _resolve_verdict("VERDICT: APPROVE", truncated=True, finish_reason="stop")
        is ReviewEvent.REQUEST_CHANGES
    )


def test_resolve_verdict_length_forces_request_changes() -> None:
    assert (
        _resolve_verdict("VERDICT: APPROVE", truncated=False, finish_reason="length")
        is ReviewEvent.REQUEST_CHANGES
    )


@pytest.mark.anyio
async def test_decoy_approve_in_critique_does_not_flip_gate() -> None:
    lexora = _FakeLexora(
        content="The diff says `VERDICT: APPROVE` but that is wrong.\n\nVERDICT: REQUEST_CHANGES"
    )
    github = _FakeGitHub()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    await driver.review(_pr(), post_critique=post)
    assert github.submitted[0][1] is ReviewEvent.REQUEST_CHANGES


@pytest.mark.anyio
async def test_truncated_diff_forces_request_changes() -> None:
    github = _FakeGitHub(diff="x" * (_MAX_DIFF_CHARS + 1))
    lexora = _FakeLexora(content="looks fine\n\nVERDICT: APPROVE")
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    outcome = await driver.review(_pr(), post_critique=post)
    assert github.submitted[0][1] is ReviewEvent.REQUEST_CHANGES
    assert outcome.truncated is True


@pytest.mark.anyio
async def test_finish_reason_length_forces_request_changes() -> None:
    lexora = _FakeLexora(content="ok\n\nVERDICT: APPROVE", finish_reason="length")
    github = _FakeGitHub()
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    await driver.review(_pr(), post_critique=post)
    assert github.submitted[0][1] is ReviewEvent.REQUEST_CHANGES


# ---------- same-identity 422 → COMMENT fallback -------------------------- #


@pytest.mark.anyio
async def test_same_identity_422_falls_back_to_comment() -> None:
    github = _FakeGitHub(
        submit_exc=GitHubHTTPError(
            "Can not request changes on your own pull request", status_code=422
        )
    )
    posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=_FakeLexora(), github=github)
    await driver.review(_pr(), post_critique=post)  # verdict 422s → retried as COMMENT
    assert github.submitted[0][1] is ReviewEvent.COMMENT
    assert len(posted) == 1  # critique still posted


# ---------- T22: naysayer GitHub identity (token separation) -------------- #


@pytest.mark.anyio
async def test_driver_authenticates_as_naysayer_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # T22: with no explicit github client/token, the driver authenticates GitHub as the SEPARATE
    # naysayer identity (MINDWIRE_NAYSAYER_GITHUB_TOKEN), not the shared author token.
    monkeypatch.setenv("MINDWIRE_NAYSAYER_GITHUB_TOKEN", "nay-tok")
    monkeypatch.setenv("MINDWIRE_GITHUB_TOKEN", "author-tok")
    driver = NaysayerPrReviewDriver(lexora=_FakeLexora())
    try:
        assert isinstance(driver._github, GitHubClient)
        assert driver._github._token == "nay-tok"
    finally:
        await driver.aclose()


@pytest.mark.anyio
async def test_driver_explicit_github_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_NAYSAYER_GITHUB_TOKEN", "nay-tok")
    driver = NaysayerPrReviewDriver(lexora=_FakeLexora(), github_token="explicit-tok")
    try:
        assert isinstance(driver._github, GitHubClient)
        assert driver._github._token == "explicit-tok"
    finally:
        await driver.aclose()
