"""The pure half and the GitHub reads of the review-request trigger.

Thread T-gate-fires-on-review-request. Design: Bohr msg-10106 (v1), msg-10108 (v1.1) and
msg-10110 (v1.2). The runner / conductor / sweep tests of design §C land with the I/O half.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from spirrow_mindwire.github.client import (
    GatePr,
    GateRequestReadError,
    GitHubClient,
    GitHubHTTPError,
    IssueCommentInfo,
    PrRef,
    ReviewInfo,
    ReviewRequestEvent,
    _gate_pr_row,
)
from spirrow_mindwire.pr_event_advance.auto_gate.decide import already_fired
from spirrow_mindwire.pr_event_advance.auto_gate.request import (
    PROJECT_WAIT,
    RequestNotice,
    is_receiving_thread,
    latest_request,
    notice_already_posted,
    notice_window_start,
    project_wait_expired,
    receiving_thread_id,
    render_notice,
    render_request_marker,
    request_already_fired,
    request_post,
    request_prefilter,
    requests_gate,
    timeline_failure_reason,
)
from spirrow_mindwire.pr_event_advance.decide import PR_EVENT_RELAY_AUTHOR

LOGIN = "spirrowgames-ops"
_AT = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)


def _pr(*, body: str = "", requested: tuple[str, ...] = (LOGIN,), draft: bool = False) -> GatePr:
    return GatePr(
        ref=PrRef("SpirrowGames", "Spirrow-VoxelWorld", 590),
        draft=draft,
        head_sha="a" * 40,
        base_ref="feature/x",
        body=body,
        author_login="dany1468[bot]",
        author_type="Bot",
        created_at=_AT,
        requested_logins=requested,
    )


def _event(
    event_id: int = 11,
    *,
    actor: str = "dany1468",
    actor_type: str = "User",
    requested: str = LOGIN,
    at: datetime | None = _AT,
) -> ReviewRequestEvent:
    return ReviewRequestEvent(
        event_id=event_id,
        requested_login=requested,
        actor_login=actor,
        actor_type=actor_type,
        created_at=at,
    )


# --- signal and request -------------------------------------------------------------------------


def test_signal_is_the_review_login_case_insensitively() -> None:
    assert requests_gate(_pr(requested=("SpirrowGames-Ops",)), LOGIN)
    assert not requests_gate(_pr(requested=("someone",)), LOGIN)
    assert not requests_gate(_pr(requested=()), LOGIN)
    assert not requests_gate(_pr(), "")


def test_latest_request_is_the_newest_event_naming_the_login() -> None:
    events = [_event(1), _event(2, requested="other"), _event(3), _event(4, requested="x")]
    found = latest_request(events, LOGIN.upper())
    assert found is not None and found.event_id == 3
    assert latest_request([_event(5, requested="other")], LOGIN) is None


def test_receiving_thread_id_is_lower_cased_and_recognised() -> None:
    ref = PrRef("SpirrowGames", "Spirrow-VoxelWorld", 590)
    tid = receiving_thread_id(ref)
    assert tid == "T-gate-request-spirrow-voxelworld-590"
    assert is_receiving_thread(tid)
    assert not is_receiving_thread("T-pr-review-spirrow-voxelworld-590")
    assert not is_receiving_thread("T-gate-request-")


# --- M1 / M2 / M3 -------------------------------------------------------------------------------


def test_m1_bot_or_the_review_login_itself_is_refused() -> None:
    assert request_prefilter(_pr(), _event(actor="x[bot]"), LOGIN) is RequestNotice.REQUESTER_BOT
    bot_type = _event(actor="ci", actor_type="Bot")
    assert request_prefilter(_pr(), bot_type, LOGIN) is RequestNotice.REQUESTER_BOT
    assert request_prefilter(_pr(), _event(actor=LOGIN.upper()), LOGIN) is (
        RequestNotice.REQUESTER_BOT
    )


def test_m2_gate_hold_is_refused() -> None:
    assert request_prefilter(_pr(body="Gate: hold"), _event(), LOGIN) is RequestNotice.HOLD


def test_m3_draft_stacked_and_bot_author_are_not_read() -> None:
    # _pr() is a bot-authored PR on a stacked base; draft is added here.
    assert request_prefilter(_pr(draft=True), _event(), LOGIN) is None


# --- fire marker --------------------------------------------------------------------------------


def _relay(content: str) -> dict[str, Any]:
    return {"author": PR_EVENT_RELAY_AUTHOR, "content": content}


def test_fire_post_carries_the_marker_and_the_handoff_last() -> None:
    post = request_post(
        repo="SpirrowGames/Spirrow-VoxelWorld",
        number=590,
        event=_event(11),
        thread_refs=("T-design",),
    )
    rendered = post.render()
    assert rendered.splitlines()[-1] == "NEXT: pr-review SpirrowGames/Spirrow-VoxelWorld#590"
    assert "T-design" in rendered
    marker_line = rendered.splitlines()[-2]
    payload = json.loads(marker_line.removeprefix("<!-- mindwire:auto-gate v1 ")[: -len(" -->")])
    assert payload == {
        "repo": "SpirrowGames/Spirrow-VoxelWorld",
        "pr": 590,
        "trigger": "review-request",
        "event": 11,
        "requester": "dany1468",
    }


def test_request_already_fired_matches_trigger_and_event_only() -> None:
    slug = "SpirrowGames/Spirrow-VoxelWorld"
    marker = render_request_marker(repo=slug, pr=590, event=11, requester="d")
    messages = [_relay(f"x\n{marker}\nNEXT: pr-review {slug}#590")]
    assert request_already_fired(messages, repo=slug.lower(), number=590, event=11)
    assert not request_already_fired(messages, repo=slug, number=590, event=12)
    assert not request_already_fired(messages, repo=slug, number=591, event=11)
    # Only the relay author's own posts count.
    forged = [{"author": "Bohr", "content": marker}]
    assert not request_already_fired(forged, repo=slug, number=590, event=11)
    # A plain 1c marker is not a review-request fire.
    plain = [_relay(f'<!-- mindwire:auto-gate v1 {{"repo": "{slug}", "pr": 590}} -->')]
    assert not request_already_fired(plain, repo=slug, number=590, event=11)


def test_1c_reader_still_sees_a_review_request_marker() -> None:
    """v1 §4: once fired by request, 1c reads the PR as already fired (no double fire)."""
    marker = render_request_marker(repo="o/r", pr=3, event=1, requester="d")
    assert already_fired([_relay(marker)], repo="o/r", number=3)


# --- notices ------------------------------------------------------------------------------------


def _comment(body: str, *, at: datetime | None = _AT, login: str = LOGIN) -> IssueCommentInfo:
    return IssueCommentInfo(body=body, author_login=login, created_at=at)


def test_notice_dedup_is_per_event_and_reason() -> None:
    notice = render_notice(event=11, reason=RequestNotice.HOLD.value)
    comments = [_comment(notice)]
    kw: dict[str, Any] = {"review_login": LOGIN, "since": None}
    assert notice_already_posted(comments, event=11, reason="hold", **kw)
    assert not notice_already_posted(comments, event=12, reason="hold", **kw)
    assert not notice_already_posted(comments, event=11, reason="cap", **kw)
    assert not notice_already_posted(
        [_comment(notice, login="someone")], event=11, reason="hold", **kw
    )


def test_null_event_notice_window_restarts_after_a_gate_review() -> None:
    notice = render_notice(event=None, reason=RequestNotice.TIMELINE_TRANSIENT.value)
    comments = [_comment(notice, at=_AT)]
    reviews = [
        ReviewInfo(
            login=LOGIN.upper(),
            state="COMMENTED",
            commit_id=None,
            submitted_at=(_AT + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        ),
        ReviewInfo(login="other", state="APPROVED", commit_id=None, submitted_at="2099-01-01Z"),
    ]
    since = notice_window_start(reviews, LOGIN)
    assert since == _AT + timedelta(minutes=5)
    kw: dict[str, Any] = {"event": None, "reason": "timeline-transient", "review_login": LOGIN}
    assert not notice_already_posted(comments, since=since, **kw)
    # The review list unread -> the whole PR, which keeps it to one notice.
    assert notice_already_posted(comments, since=None, **kw)
    assert notice_window_start([], LOGIN) is None


def test_notice_text_names_the_reason_and_falls_back_for_unknown_ones() -> None:
    assert "Gate: hold" in render_notice(event=1, reason="hold")
    body = render_notice(event=None, reason="timeline-http-403")
    assert "timeline-http-403" in body
    assert '"event": null' in body


def test_timeline_failure_reason() -> None:
    assert timeline_failure_reason(transient=True, code="transient") == "timeline-transient"
    assert timeline_failure_reason(transient=False, code="http-403") == "timeline-http-403"


def test_project_wait_is_measured_from_the_event() -> None:
    assert not project_wait_expired(_event(at=_AT), _AT + PROJECT_WAIT - timedelta(seconds=1))
    assert project_wait_expired(_event(at=_AT), _AT + PROJECT_WAIT)
    assert project_wait_expired(_event(at=None), _AT)


# --- GitHub reads -------------------------------------------------------------------------------


def _client(handler: Any) -> GitHubClient:
    return GitHubClient("tok", transport=httpx.MockTransport(handler))


_REF = PrRef("o", "r", 5)


def test_listing_row_carries_requested_user_reviewers_only() -> None:
    row = {
        "number": 5,
        "head": {"sha": "b" * 40},
        "base": {"ref": "main"},
        "created_at": "2026-11-27T00:00:00Z",
        "draft": False,
        "user": {"login": "dev", "type": "User"},
        "requested_reviewers": [{"login": LOGIN}, {"nope": 1}, "x"],
        "requested_teams": [{"slug": "core"}],
    }
    parsed = _gate_pr_row("o", "r", row)
    assert parsed is not None and parsed.requested_logins == (LOGIN,)
    parsed = _gate_pr_row("o", "r", {**row, "requested_reviewers": None})
    assert parsed is not None and parsed.requested_logins == ()


def test_timeline_read_pages_and_keeps_user_review_requests() -> None:
    page1 = [{"event": "commented", "id": 1}] * 99 + [
        {
            "event": "review_requested",
            "id": 7,
            "requested_reviewer": {"login": LOGIN},
            "actor": {"login": "dev", "type": "User"},
            "created_at": "2026-11-27T00:00:00Z",
        }
    ]
    page2 = [
        {"event": "review_requested", "id": 8, "requested_team": {"slug": "t"}, "actor": {}},
        {
            "event": "review_requested",
            "id": 9,
            "requested_reviewer": {"login": LOGIN},
            "actor": {"login": "dev2", "type": "User"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/o/r/issues/5/timeline"
        page = request.url.params["page"]
        return httpx.Response(200, json=page1 if page == "1" else page2)

    events = asyncio.run(_client(handler).fetch_review_request_events(_REF))
    assert [e.event_id for e in events] == [7, 9]
    assert events[0].created_at == datetime(2026, 11, 27, tzinfo=UTC)
    assert events[1].created_at is None


@pytest.mark.parametrize(
    ("status", "headers", "transient", "code"),
    [
        (503, {}, True, "transient"),
        (429, {}, True, "transient"),
        (403, {"x-ratelimit-remaining": "0"}, True, "transient"),
        (403, {}, False, "http-403"),
        (404, {}, False, "http-404"),
    ],
)
def test_timeline_failures_are_classified(
    status: int, headers: dict[str, str], transient: bool, code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json={"message": "x"})

    with pytest.raises(GateRequestReadError) as info:
        asyncio.run(_client(handler).fetch_review_request_events(_REF))
    assert (info.value.transient, info.value.code) == (transient, code)


def test_timeline_timeout_is_transient_and_bad_shape_is_permanent() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(GateRequestReadError) as info:
        asyncio.run(_client(timeout).fetch_review_request_events(_REF))
    assert info.value.transient

    def shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    with pytest.raises(GateRequestReadError) as info:
        asyncio.run(_client(shape).fetch_review_request_events(_REF))
    assert (info.value.transient, info.value.code) == (False, "shape")


def test_issue_comments_read_and_post() -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/o/r/issues/5/comments"
        if request.method == "POST":
            seen.append(("POST", json.loads(request.content)))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(
            200,
            json=[{"body": "hi", "user": {"login": LOGIN}, "created_at": "2026-11-27T00:00:00Z"}],
        )

    client = _client(handler)
    comments = asyncio.run(client.list_issue_comments(_REF))
    assert comments == [IssueCommentInfo("hi", LOGIN, datetime(2026, 11, 27, tzinfo=UTC))]
    asyncio.run(client.post_issue_comment(_REF, "body"))
    assert seen == [("POST", {"body": "body"})]


def test_issue_comment_post_failure_carries_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "no"})

    with pytest.raises(GitHubHTTPError) as info:
        asyncio.run(_client(handler).post_issue_comment(_REF, "b"))
    assert info.value.status_code == 403
