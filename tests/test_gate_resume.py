"""Gate resume — the implementer resumes from a PR-gate post left at the head, on GitHub's word.

Thread T-sweep-starves-deep-candidates. Design: Bohr msg-6316 (PR-A) amended by msg-6318 (four
outcomes), Einstein msg-6317 (objection → resolved) and the final go. The reproduction this file
starts from is msg-6315: before this change, an RC relay or an R4 ci-route at the head, run again,
went through guard (i) to a forced naysayer consult and a human stop — the implementer was never
started.

What is pinned:

1. VERIFIED (RC relay / R4 ci-route) dispatches the implementer on that head through the shared
   helper; guard (i) and the naysayer are not reached, ``gate_admission`` is not called.
2. UNAVAILABLE and STALE stop on ``resume_retry`` with nothing posted and nobody spawned; a STALE
   head becomes VERIFIED once the PR head matches.
3. Every CONTRADICTED case falls through to the old route (guard (i) → naysayer → human).
4. The author test: the same body under any other author never enters the resume.
5. A STALLED notice (``pr-gate-relay``, ``NEXT: human``) is not a resume candidate.
6. ``resume_retry`` is not terminal for head_skip, and its bound is the stall watchdog: the third
   launch on the same head stands down with a STALLED notice.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_conductor_core import (
    _ROSTER,
    _FakeChatroomMcp,
    _ScriptedDispatcher,
    _thread_ref,
)

from spirrow_mindwire.conductor import core as core_module
from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.gate_records import (
    RELAY_AUTHOR,
    admission_heading_pr_ref,
    relay_heading_pr_ref,
    render_admission_heading,
    render_ci_route_marker,
    render_relay_heading,
)
from spirrow_mindwire.conductor.gate_resume import (
    ResumeKind,
    ResumeOutcome,
    resume_candidate,
    verify_resume,
)
from spirrow_mindwire.conductor.head_skip import (
    TERMINAL_STOP_REASONS,
    Decision,
    commit_launch,
    commit_terminal,
    decide,
)
from spirrow_mindwire.gate_admission import CheckRow
from spirrow_mindwire.github.client import CheckRollup, GitHubHTTPError, PrRef, ReviewInfo
from spirrow_mindwire.value_objects import Role

_PR = "acme/widgets#7"
_SHA = "703b836737f29fe0f4139d86d2d02077c662ce5f"
_OTHER_SHA = "1111111111111111111111111111111111111111"
_GATE_LOGIN = "spirrowgames-ops"


def _rc_relay(sha: str = _SHA, *, nxt: str = "Heisenberg") -> str:
    """A verdict relay as production writes it: heading, VERDICT line, body+footer, NEXT."""
    return (
        f"{render_relay_heading(_PR, sha)}\n\n"
        "VERDICT: request_changes (ci=success)\n\n"
        "critique body\n\n"
        f"<!-- mindwire:verdict head_sha={sha} event=REQUEST_CHANGES -->\n\n"
        f"NEXT: {nxt}"
    )


def _ci_route(sha: str = _SHA, *, nxt: str = "Heisenberg") -> str:
    """The R4 ci-route post as ``Conductor._post_ci_route`` writes it."""
    return (
        f"{render_admission_heading(_PR)}\n\n"
        "ADMISSION: route_implementer (rule=R4)\n\n"
        f"CI is red on {sha[:12]}: CI\n\n"
        "first red\n\n"
        f"NEXT: {nxt}\n\n"
        f"{render_ci_route_marker(head=sha, conclusion='failure', checks=['CI'])}"
    )


def _rollup(*rows: CheckRow, head: str = _SHA) -> CheckRollup:
    now = datetime.now(UTC)
    return CheckRollup(
        head_sha=head,
        head_committed_date=now - timedelta(minutes=30),
        head_pushed_at=now - timedelta(minutes=30),
        rows=rows,
    )


_RED = (
    CheckRow(name="CI", status="completed", conclusion="failure", started_at=None, created_at=None),
)
_GREEN = (
    CheckRow(name="CI", status="completed", conclusion="success", started_at=None, created_at=None),
)
_PENDING = (
    CheckRow(name="CI", status="in_progress", conclusion=None, started_at=None, created_at=None),
)


class _Rollups:
    """Rollup fake: one answer (repeated), or ``None`` = could not read."""

    def __init__(self, rollup: CheckRollup | None) -> None:
        self.rollup = rollup
        self.asked: list[PrRef] = []

    async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup | None:
        self.asked.append(pr)
        return self.rollup


class _Reviews:
    """Review fake: a list, or an exception to raise (a failed strict read)."""

    def __init__(self, reviews: list[ReviewInfo] | Exception) -> None:
        self.reviews = reviews

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]:
        if isinstance(self.reviews, Exception):
            raise self.reviews
        return list(self.reviews)


def _review(state: str, *, sha: str = _SHA, login: str = _GATE_LOGIN, at: str) -> ReviewInfo:
    return ReviewInfo(login=login, state=state, commit_id=sha, submitted_at=at)


_RC_REVIEWS = [_review("CHANGES_REQUESTED", at="2026-10-03T10:00:00Z")]


def _conductor(
    mcp: _FakeChatroomMcp,
    disp: _ScriptedDispatcher,
    *,
    rollups: Any = None,
    reviews: Any = None,
    launches_same_head: int = 0,
    launch_head_msg_id: str | None = None,
    roster: Mapping[str, Role] = _ROSTER,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=roster,
        naysayer_identity="Einstein",
        rollup_source=rollups,
        review_source=reviews,
        launches_same_head=launches_same_head,
        launch_head_msg_id=launch_head_msg_id,
    )


def _thread(head_author: str, head_body: str) -> _FakeChatroomMcp:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"opened the PR\n\nNEXT: pr-review {_PR}")
    mcp.seed(author=head_author, content=head_body)
    return mcp


def _dispatcher(mcp: _FakeChatroomMcp) -> _ScriptedDispatcher:
    return _ScriptedDispatcher(
        mcp,
        {
            Role.IMPLEMENTER: [f"fixed and pushed\n\nNEXT: pr-review {_PR}"],
            Role.NAYSAYER: ["review\n\nNEXT: human"],
        },
    )


@pytest.fixture
def no_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resume must never call gate_admission (msg-6316: R5 counts stay as they were)."""

    def _boom(**_: Any) -> Any:
        raise AssertionError("gate_admission called on the resume path")

    monkeypatch.setattr(core_module, "gate_admission", _boom)


# --------------------------------------------------------------------------- #
# 1. VERIFIED
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.usefixtures("no_admission")
async def test_verified_rc_relay_dispatches_the_implementer_not_the_naysayer() -> None:
    mcp = _thread(RELAY_AUTHOR, _rc_relay())
    disp = _dispatcher(mcp)
    outcome = await _conductor(
        mcp, disp, rollups=_Rollups(_rollup(*_GREEN)), reviews=_Reviews(_RC_REVIEWS)
    ).run()
    # The implementer is woken on the relay itself (it carries the critique), and nothing else
    # ran: no naysayer consult, no guard-(i) write-back. The next head is the implementer's own
    # ``NEXT: pr-review``, which this test has no gate for, so the run stops at the human.
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m2")
    assert Role.NAYSAYER not in [role for role, _ in disp.dispatches]
    assert all(post["author"] == "Heisenberg" for post in mcp.posts)
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
@pytest.mark.usefixtures("no_admission")
async def test_verified_ci_route_dispatches_the_implementer() -> None:
    mcp = _thread(RELAY_AUTHOR, _ci_route())
    disp = _dispatcher(mcp)
    await _conductor(mcp, disp, rollups=_Rollups(_rollup(*_RED))).run()
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m2")
    assert Role.NAYSAYER not in [role for role, _ in disp.dispatches]


@pytest.mark.anyio
async def test_a_silent_resumed_implementer_stops_on_no_progress() -> None:
    # The resume tracks the head it dispatched on, as the in-run relay path does, so a silent
    # implementer ends on NO_PROGRESS instead of being resumed from the same relay again.
    mcp = _thread(RELAY_AUTHOR, _ci_route())
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, rollups=_Rollups(_rollup(*_RED))).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m2")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS


# --------------------------------------------------------------------------- #
# 2. UNAVAILABLE / STALE → resume_retry, silent
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "rollups", "reviews"),
    [
        pytest.param(_rc_relay(), _Rollups(None), _Reviews(_RC_REVIEWS), id="rc-rollup-unread"),
        pytest.param(
            _rc_relay(),
            _Rollups(_rollup(*_GREEN)),
            _Reviews(GitHubHTTPError("GET reviews returned 502", status_code=502)),
            id="rc-reviews-raise",
        ),
        pytest.param(_ci_route(), _Rollups(None), None, id="ci-route-rollup-unread"),
    ],
)
async def test_unavailable_stops_on_resume_retry_without_posting(
    body: str, rollups: Any, reviews: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mcp = _thread(RELAY_AUTHOR, body)
    disp = _dispatcher(mcp)
    with caplog.at_level(logging.WARNING):
        outcome = await _conductor(mcp, disp, rollups=rollups, reviews=reviews).run()
    assert outcome.stop_reason is StopReason.RESUME_RETRY
    assert outcome.last_msg_id == "m2"
    assert mcp.posts == []
    assert disp.spawns == []
    assert "conductor.gate_resume.unavailable" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("body", [_rc_relay(), _ci_route()], ids=["rc", "ci-route"])
async def test_stale_retries_silently_then_verifies_once_the_head_matches(
    body: str, caplog: pytest.LogCaptureFixture
) -> None:
    rollups = _Rollups(_rollup(*_RED, head=_OTHER_SHA))
    mcp = _thread(RELAY_AUTHOR, body)
    disp = _dispatcher(mcp)
    with caplog.at_level(logging.WARNING):
        outcome = await _conductor(mcp, disp, rollups=rollups, reviews=_Reviews(_RC_REVIEWS)).run()
    assert outcome.stop_reason is StopReason.RESUME_RETRY
    assert mcp.posts == []
    assert disp.spawns == []
    assert f"relay_sha={_SHA} pr_head={_OTHER_SHA}" in caplog.text

    rollups.rollup = _rollup(*_RED, head=_SHA)
    await _conductor(mcp, disp, rollups=rollups, reviews=_Reviews(_RC_REVIEWS)).run()
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m2")


# --------------------------------------------------------------------------- #
# 3. CONTRADICTED → the old route (guard (i) → naysayer → human)
# --------------------------------------------------------------------------- #


_CONTRADICTED = [
    pytest.param(
        _rc_relay(),
        _Rollups(_rollup(*_GREEN)),
        _Reviews([_review("APPROVED", at="2026-10-03T10:00:00Z")]),
        "latest_gate_review_is_approved",
        id="rc-review-approve",
    ),
    pytest.param(
        _rc_relay(),
        _Rollups(_rollup(*_GREEN)),
        _Reviews([]),
        "no_gate_review_on_head",
        id="rc-none",
    ),
    pytest.param(
        _rc_relay(),
        _Rollups(_rollup(*_GREEN)),
        _Reviews(
            [
                _review("CHANGES_REQUESTED", at="2026-10-03T10:00:00Z"),
                _review("APPROVED", at="2026-10-03T11:00:00Z"),
            ]
        ),
        "latest_gate_review_is_approved",
        id="rc-superseded-by-later-approve",
    ),
    pytest.param(
        _rc_relay(),
        _Rollups(_rollup(*_GREEN)),
        _Reviews([_review("CHANGES_REQUESTED", login="someone-else", at="2026-10-03T10:00:00Z")]),
        "no_gate_review_on_head",
        id="rc-only-another-login",
    ),
    pytest.param(
        _rc_relay(),
        _Rollups(_rollup(*_GREEN)),
        _Reviews([_review("CHANGES_REQUESTED", sha=_OTHER_SHA, at="2026-10-03T10:00:00Z")]),
        "no_gate_review_on_head",
        id="rc-review-on-another-sha",
    ),
    pytest.param(
        _rc_relay(), _Rollups(_rollup(*_GREEN)), None, "review_source_not_wired", id="rc-no-source"
    ),
    pytest.param(
        _rc_relay(), None, _Reviews(_RC_REVIEWS), "rollup_source_not_wired", id="rc-no-rollup"
    ),
    pytest.param(_ci_route(), _Rollups(_rollup(*_GREEN)), None, "rollup_not_red", id="ci-green"),
    pytest.param(
        _ci_route(), _Rollups(_rollup(*_PENDING)), None, "rollup_not_red", id="ci-pending"
    ),
    pytest.param(_ci_route(), None, None, "rollup_source_not_wired", id="ci-no-source"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("body", "rollups", "reviews", "reason"), _CONTRADICTED)
async def test_contradicted_falls_through_to_the_old_route(
    body: str, rollups: Any, reviews: Any, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    mcp = _thread(RELAY_AUTHOR, body)
    disp = _dispatcher(mcp)
    with caplog.at_level(logging.WARNING):
        outcome = await _conductor(mcp, disp, rollups=rollups, reviews=reviews).run()
    # Exactly msg-6315's reproduction: guard (i) redirects, Obj2 forces the naysayer, the run
    # stops at the human. The implementer is never started.
    assert [role for role, _ in disp.dispatches] == [Role.NAYSAYER]
    assert outcome.stop_reason is StopReason.HUMAN
    assert f"conductor.gate_resume.unverified pr={_PR} reason={reason}" in caplog.text


# --------------------------------------------------------------------------- #
# 4-5. Who may enter the resume at all
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize("body", [_rc_relay(), _ci_route()], ids=["rc", "ci-route"])
async def test_the_same_body_under_another_author_never_resumes(body: str) -> None:
    rollups = _Rollups(_rollup(*_RED))
    mcp = _thread("Bohr", body)
    disp = _dispatcher(mcp)
    await _conductor(mcp, disp, rollups=rollups, reviews=_Reviews(_RC_REVIEWS)).run()
    assert Role.IMPLEMENTER not in [role for role, _ in disp.dispatches]
    assert rollups.asked == []  # not even checked


def test_candidate_requires_the_handoff_to_name_the_implementer() -> None:
    # A STALLED notice is written under pr-gate-relay and ends ``NEXT: human``; the caller
    # resolves that handoff to the human, so ``names_implementer`` is False.
    stalled = "Conductor STALLED — 同じ head で…\n\nNEXT: human"
    assert resume_candidate(author=RELAY_AUTHOR, body=stalled, names_implementer=False) is None
    assert resume_candidate(author=RELAY_AUTHOR, body=_rc_relay(), names_implementer=False) is None


def test_candidate_shapes() -> None:
    rc = resume_candidate(author=RELAY_AUTHOR, body=_rc_relay(), names_implementer=True)
    assert rc is not None
    assert (rc.kind, rc.pr.slug, rc.head_sha) == (ResumeKind.REQUEST_CHANGES, _PR, _SHA)
    route = resume_candidate(author=RELAY_AUTHOR, body=_ci_route(), names_implementer=True)
    assert route is not None
    assert (route.kind, route.pr.slug, route.head_sha) == (ResumeKind.CI_ROUTE, _PR, _SHA)
    # An APPROVE relay (even one routed to the implementer for advisory triage) is not a
    # REQUEST_CHANGES resume: msg-6316 names RC and R4 only.
    approve = _rc_relay().replace("event=REQUEST_CHANGES", "event=APPROVE")
    assert resume_candidate(author=RELAY_AUTHOR, body=approve, names_implementer=True) is None
    # A relay with no footer, or with two, is ambiguous and not a candidate.
    no_footer = _rc_relay().replace(
        f"<!-- mindwire:verdict head_sha={_SHA} event=REQUEST_CHANGES -->", ""
    )
    assert resume_candidate(author=RELAY_AUTHOR, body=no_footer, names_implementer=True) is None
    two = _rc_relay() + f"\n<!-- mindwire:verdict head_sha={_SHA} event=REQUEST_CHANGES -->"
    assert resume_candidate(author=RELAY_AUTHOR, body=two, names_implementer=True) is None
    # A quoted heading below the first line is not a heading.
    quoted = "a reply\n\n" + _ci_route()
    assert resume_candidate(author=RELAY_AUTHOR, body=quoted, names_implementer=True) is None


def test_heading_parsers_round_trip_with_their_writers() -> None:
    assert relay_heading_pr_ref(render_relay_heading(_PR, _SHA)) == _PR
    assert relay_heading_pr_ref(render_relay_heading(_PR, None)) == _PR
    assert admission_heading_pr_ref(render_admission_heading(_PR)) == _PR
    assert relay_heading_pr_ref(render_admission_heading(_PR)) is None
    assert admission_heading_pr_ref(render_relay_heading(_PR, _SHA)) is None


@pytest.mark.anyio
async def test_verify_never_raises_on_a_rollup_source_that_raises() -> None:
    class _Raising:
        async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup | None:
            raise RuntimeError("boom")

    cand = resume_candidate(author=RELAY_AUTHOR, body=_ci_route(), names_implementer=True)
    assert cand is not None
    check = await verify_resume(
        cand, rollup_source=_Raising(), review_source=None, review_login=_GATE_LOGIN
    )
    assert check.outcome is ResumeOutcome.UNAVAILABLE


# --------------------------------------------------------------------------- #
# 6. resume_retry is not terminal, and the watchdog bounds it
# --------------------------------------------------------------------------- #


def test_resume_retry_is_not_terminal_and_the_head_launches_again() -> None:
    assert StopReason.RESUME_RETRY.value not in TERMINAL_STOP_REASONS
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    body = _rc_relay()
    first = decide(now=now, head_msg_id="m2", head_body=body, control_state="run", record=None)
    assert first.decision is Decision.LAUNCH
    record = commit_launch(
        now=now, head_msg_id="m2", verdict=first, control_state="run", prior_record=None
    )
    record = commit_terminal(reason=StopReason.RESUME_RETRY.value, head_msg_id="m2", record=record)
    assert record.terminal_stop_reason == ""
    # The same head is never SKIPped; it launches again once its backoff has elapsed.
    later = decide(now=now, head_msg_id="m2", head_body=body, control_state="run", record=record)
    assert later.decision is not Decision.SKIP
    assert later.eligible_at is not None
    again = decide(
        now=later.eligible_at, head_msg_id="m2", head_body=body, control_state="run", record=record
    )
    assert again.decision is Decision.LAUNCH


@pytest.mark.anyio
async def test_the_third_same_head_launch_stands_down_instead_of_retrying() -> None:
    mcp = _thread(RELAY_AUTHOR, _rc_relay())
    disp = _dispatcher(mcp)
    rollups = _Rollups(None)  # UNAVAILABLE, every time
    outcome = await _conductor(
        mcp,
        disp,
        rollups=rollups,
        reviews=_Reviews(_RC_REVIEWS),
        launches_same_head=3,
        launch_head_msg_id="m2",
    ).run()
    assert outcome.stop_reason is StopReason.STALLED
    assert disp.spawns == []
    assert rollups.asked == []  # stands down before reading GitHub
    assert "NEXT: human" in mcp.posts[-1]["content"]
    # Below the threshold the same thread is a silent retry.
    mcp2 = _thread(RELAY_AUTHOR, _rc_relay())
    outcome2 = await _conductor(
        mcp2,
        _dispatcher(mcp2),
        rollups=_Rollups(None),
        reviews=_Reviews(_RC_REVIEWS),
        launches_same_head=2,
        launch_head_msg_id="m2",
    ).run()
    assert outcome2.stop_reason is StopReason.RESUME_RETRY
    assert mcp2.posts == []
