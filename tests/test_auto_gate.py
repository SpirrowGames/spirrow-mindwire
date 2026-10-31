"""1c: T-gate-fires-on-pr-open. Parser tables are msg-7487 to msg-7495, U1/U2 msg-7501, the pass
msg-7619 + msg-7735, and the fire record msg-7737.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from spirrow_mindwire.conductor.gate_records import RELAY_AUTHOR, render_relay_heading
from spirrow_mindwire.conductor.handoff import HandoffKind, resolve_handoff
from spirrow_mindwire.github.client import (
    GatePr,
    GatePrListing,
    GitHubClient,
    OpenPrListingError,
    PrRef,
    ReviewInfo,
)
from spirrow_mindwire.magickit.client import MagickitMcpError, ThreadResolvedError
from spirrow_mindwire.pr_event_advance.auto_gate.__main__ import (
    AutoGateConfigError,
    cap_digest_line,
    parse_auto_gate_config,
    parse_remote_url,
)
from spirrow_mindwire.pr_event_advance.auto_gate.decide import (
    THREAD_LINE_RE,
    AutoGateNoop,
    ThreadLink,
    already_fired,
    has_gate_hold,
    is_bot_author,
    prefilter,
    render_marker,
    resolve_thread_link,
    tail_decision,
)
from spirrow_mindwire.pr_event_advance.auto_gate.runner import (
    AutoGateConfig,
    DailyCounterStore,
    PassReport,
    RepoTarget,
    run_pass,
)
from spirrow_mindwire.pr_event_advance.decide import PR_EVENT_RELAY_AUTHOR, Post

pytestmark = pytest.mark.anyio

_REPO = "acme/widgets"
_LOGIN = "spirrowgames-ops"
_NOW = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)  # 12:00 JST on 2026-11-01
_TODAY = "2026-11-01"
_TARGET = RepoTarget("proj", "acme", "widgets")
_LIVE = AutoGateConfig(live=True)
_HEAD = "703b836737f29fe0f4139d86d2d02077c662ce5f"
_OLD = "1111111111111111111111111111111111111111"


def _pr(
    number: int = 7,
    *,
    body: str = "Thread: T-x",
    draft: bool = False,
    base: str = "main",
    login: str = "dev",
    user_type: str = "User",
    head: str = _HEAD,
) -> GatePr:
    return GatePr(
        ref=PrRef("acme", "widgets", number),
        draft=draft,
        head_sha=head,
        base_ref=base,
        body=body,
        author_login=login,
        author_type=user_type,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _msg(content: str, author: str = "Bohr", msg_id: str = "msg-1") -> dict[str, Any]:
    return {
        "author": author,
        "content": content,
        "msg_id": msg_id,
        "timestamp": "2026-10-31T00:00:00Z",
    }


class FakeMcp:
    def __init__(
        self,
        threads: dict[str, list[dict[str, Any]]] | None = None,
        *,
        fail_posts: dict[str, Exception] | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self.threads = threads if threads is not None else {"T-x": [_msg("done\n\nNEXT: none")]}
        self.fail_posts = fail_posts or {}
        self.statuses = statuses or {}
        self.posts: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        thread_id = arguments["thread_id"]
        if name == "chatroom_get_thread":
            if thread_id not in self.threads:
                raise MagickitMcpError("no such thread")
            return {
                "messages": self.threads[thread_id],
                "thread": {"status": self.statuses.get(thread_id, "active")},
            }
        assert name == "chatroom_post_message"
        ref = arguments["content"].splitlines()[0].rsplit(" ", 1)[-1]
        if ref in self.fail_posts:
            raise self.fail_posts[ref]
        self.posts.append(arguments)
        self.threads[thread_id].append(
            _msg(arguments["content"], arguments["author"], f"msg-{len(self.posts) + 100}")
        )
        return {"msg": {"msg_id": f"msg-{len(self.posts) + 100}"}}


class FakeGh:
    def __init__(
        self,
        prs: list[GatePr],
        *,
        reviews: list[ReviewInfo] | None = None,
        review_error: Exception | None = None,
    ) -> None:
        self.prs = prs
        self.reviews = reviews or []
        self.review_error = review_error
        self.list_calls = 0
        self.review_calls = 0

    async def list_open_gate_prs(self, owner: str, repo: str) -> GatePrListing:
        self.list_calls += 1
        return GatePrListing(prs=tuple(self.prs), examined=len(self.prs))

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]:
        self.review_calls += 1
        if self.review_error is not None:
            raise self.review_error
        return self.reviews


async def _run(
    mcp: FakeMcp,
    gh: FakeGh,
    tmp_path: Path,
    *,
    config: AutoGateConfig = _LIVE,
    now: datetime = _NOW,
) -> PassReport:
    return await run_pass(
        mcp=mcp,
        gh=gh,
        targets=[_TARGET],
        config=config,
        store=DailyCounterStore(tmp_path / "state" / "auto-gate-daily.json"),
        review_login=_LOGIN,
        now=now,
    )


def _seed(tmp_path: Path, fired: int, date_jst: str = _TODAY) -> Path:
    path = tmp_path / "state" / "auto-gate-daily.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date_jst": date_jst, "fired": fired}), encoding="utf-8")
    return path


def _fired_state(tmp_path: Path) -> Any:
    return json.loads((tmp_path / "state" / "auto-gate-daily.json").read_text(encoding="utf-8"))


def _posted_refs(mcp: FakeMcp) -> list[str]:
    return [p["content"].splitlines()[0].rsplit(" ", 1)[-1] for p in mcp.posts]


# ---- the PR-body grammar ------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "Gate: hold",
        "Gate: hold  ",
        "Gate: hold\r\n",
        "x\rGate: hold\ry",
        "GATE:HOLD",
        "gate:HOLD",
        "\u00a0Gate: hold",
        "\ufeffGate: hold",
        "Gate: hold until mw#463 merges",
        "Gate: hold(API待ち)",
        "Gate: hold待ち",
        "```\nGate: hold\n```",  # O3: fences are not tracked, and this errs toward not firing
    ],
)
def test_gate_hold_matches(body: str) -> None:
    assert has_gate_hold(body)


@pytest.mark.parametrize(
    "body", ["> Gate: hold", "Gate: holder", "Gate: hold-out", "see Gate: hold", ""]
)
def test_gate_hold_does_not_match(body: str) -> None:
    assert not has_gate_hold(body)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Thread: T-foo", "T-foo"),
        ("Thread:T-foo", "T-foo"),
        ("THREAD: T-foo", "T-foo"),
        ("Thread: T-FooBar", "T-FooBar"),
        ("Thread: T-foo # backend用", "T-foo"),
        ("Thread: T-123 (draft)", "T-123"),
        ("Thread: T-123(draft)", "T-123"),
        ("Thread: T-foo T-foo", "T-foo"),
        ("Thread: T-foo.", "T-foo"),
        ("Thread: T-foo。", "T-foo"),
        ("Thread: T-foo. 修正完了", "T-foo"),
        ("Thread: T-foo.修正完了", "T-foo"),
        ("Thread: [T-foo]", "T-foo"),
        ("Thread: (T-foo)", "T-foo"),
        ("Thread: `T-foo`", "T-foo"),
        ("Thread: 「T-foo」", "T-foo"),
        ("Thread: {T-foo}", "T-foo"),
        ("Thread: 【T-foo】", "T-foo"),
        ("Thread: ( T-foo )", "T-foo"),
        ("Thread: 下記参照 T-foo", "T-foo"),
        ("Thread: T-foo: backend", "T-foo"),
        ("Thread: T-foo!", "T-foo"),
        ("Thread: T-foo#msg-123", "T-foo"),
        ("Thread: T-pr-review-spirrow-mindwire-194", "T-pr-review-spirrow-mindwire-194"),
        ("Thread: T-foo  \r\n", "T-foo"),
    ],
)
def test_thread_line_resolves(line: str, expected: str) -> None:
    assert resolve_thread_link(line).thread_id == expected


@pytest.mark.parametrize(
    "line",
    [
        "Thread: foo",
        "Thread: 未定",
        "Thread:",
        "Thread: t-foo",
        "Thread: T-foo.bar",
        "Thread: xT-a",
    ],
)
def test_thread_line_unparsed_keeps_the_line_for_the_log(line: str) -> None:
    link = resolve_thread_link(line)
    assert link.noop is AutoGateNoop.THREAD_LINE_UNPARSED
    assert link.unparsed_lines == (line.strip(),)


@pytest.mark.parametrize(
    "body",
    [
        "Thread: T-foo,T-bar",
        "Thread: [T-foo] [T-bar]",
        "Thread: T-foo # T-old の後継",
        "Thread: T-foo\nThread: T-bar",
        "Thread: T-foo\nCloses-thread: T-bar",
    ],
)
def test_two_ids_are_ambiguous(body: str) -> None:
    link = resolve_thread_link(body)
    assert link.noop is AutoGateNoop.AMBIGUOUS
    assert len(link.ids) == 2


def test_thread_and_closes_thread_naming_one_id_is_that_id() -> None:
    assert resolve_thread_link("Thread: T-foo\nCloses-thread: T-foo").thread_id == "T-foo"


def test_closes_thread_alone_links() -> None:
    assert resolve_thread_link("Closes-thread: T-foo").thread_id == "T-foo"


def test_no_line_at_all_is_no_thread() -> None:
    assert resolve_thread_link("just a body").noop is AutoGateNoop.NO_THREAD


def test_quoted_thread_line_is_not_read() -> None:
    assert resolve_thread_link("> Thread: T-foo").noop is AutoGateNoop.NO_THREAD


@pytest.mark.parametrize(
    "quote",
    [
        "> Closes-thread: T-foo",
        ">Closes-thread: T-foo",
        "  > Closes-thread: T-foo",
        ">> Closes-thread: T-foo",
    ],
)
def test_quoted_closes_thread_line_is_not_read(quote: str) -> None:
    """The regex runs on the raw body, so its ``^`` anchor is what keeps a quote out."""
    assert resolve_thread_link(quote).noop is AutoGateNoop.NO_THREAD
    for body in (f"Thread: T-bar\n\n{quote}\n", f"{quote}\r\nThread: T-bar"):
        link = resolve_thread_link(body)
        assert link.noop is None
        assert link.thread_id == "T-bar"


def test_unclosed_fence_does_not_hide_the_line_below_it() -> None:
    assert resolve_thread_link("```\ncode\nThread: T-foo").thread_id == "T-foo"


def test_thread_regex_compiles_with_scoped_flag_only_on_the_keyword() -> None:
    assert THREAD_LINE_RE.match("THREAD: T-foo") is not None
    assert resolve_thread_link("THREAD: T-foo").thread_id == "T-foo"
    assert resolve_thread_link("Thread: t-foo").noop is AutoGateNoop.THREAD_LINE_UNPARSED


# ---- prefilter ------------------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (_pr(draft=True), AutoGateNoop.DRAFT),
        (_pr(login="dependabot[bot]"), AutoGateNoop.BOT_AUTHOR),
        (_pr(login="github-actions[bot]"), AutoGateNoop.BOT_AUTHOR),
        (_pr(login="renovate", user_type="Bot"), AutoGateNoop.BOT_AUTHOR),
        (_pr(base="feature/x"), AutoGateNoop.STACKED),
        (_pr(body="Thread: T-x\nGate: hold"), AutoGateNoop.HOLD),
    ],
)
def test_prefilter_noops(pr: GatePr, expected: AutoGateNoop) -> None:
    assert prefilter(pr, ("main", "develop")) is expected


@pytest.mark.parametrize("base", ["main", "develop"])
def test_prefilter_passes_gate_bases(base: str) -> None:
    result = prefilter(_pr(base=base), ("main", "develop"))
    assert isinstance(result, ThreadLink)
    assert result.thread_id == "T-x"


def test_bot_rule_reads_login_suffix_and_type() -> None:
    assert is_bot_author("x[bot]", "User")
    assert is_bot_author("x", "Bot")
    assert not is_bot_author("bot", "User")


# ---- the fire record (msg-7737) --------------------------------------------------------------- #


def _marker_post(repo: str = _REPO, pr: int = 7) -> dict[str, Any]:
    return _msg(f"heading\n\n{render_marker(repo=repo, pr=pr)}\nNEXT: human", PR_EVENT_RELAY_AUTHOR)


def test_marker_from_the_relay_is_a_record() -> None:
    assert already_fired([_marker_post()], repo=_REPO, number=7)


@pytest.mark.parametrize("post", [_marker_post(pr=8), _marker_post(repo="acme/other")])
def test_a_record_for_another_pr_or_repo_does_not_count(post: dict[str, Any]) -> None:
    assert not already_fired([post], repo=_REPO, number=7)


def test_a_human_quoting_the_marker_is_not_a_record() -> None:
    marker = render_marker(repo=_REPO, pr=7)
    assert not already_fired([_msg(f"{marker}\n\nNEXT: human", "human")], repo=_REPO, number=7)
    assert not already_fired(
        [_msg(f"> {marker}\n\nNEXT: human", PR_EVENT_RELAY_AUTHOR)], repo=_REPO, number=7
    )


def test_manual_final_line_counts() -> None:
    assert already_fired([_msg("PR ready\n\nNEXT: pr-review acme/widgets#7")], repo=_REPO, number=7)


def test_manual_final_line_counts_under_the_loops_source_comment() -> None:
    body = "PR ready\n\nNEXT: pr-review acme/widgets#7\n\n<!-- source: tools=3 -->"
    assert already_fired([_msg(body)], repo=_REPO, number=7)


def test_manual_line_mid_body_does_not_count() -> None:
    body = "earlier: NEXT: pr-review acme/widgets#7\nNEXT: pr-review acme/widgets#7\nmore text"
    assert not already_fired([_msg(body)], repo=_REPO, number=7)


def test_manual_line_for_another_pr_does_not_count() -> None:
    assert not already_fired([_msg("NEXT: pr-review acme/widgets#8")], repo=_REPO, number=7)


# ---- the post itself ------------------------------------------------------------------------ #


def test_post_has_marker_directly_above_next_and_routes_to_the_gate() -> None:
    decision = tail_decision("NEXT: none", repo=_REPO, number=7)
    assert isinstance(decision, Post)
    lines = decision.render().splitlines()
    assert lines[-1] == "NEXT: pr-review acme/widgets#7"
    assert lines[-2] == render_marker(repo=_REPO, pr=7)
    handoff = resolve_handoff(decision.render(), {})
    assert handoff.kind is HandoffKind.PR_REVIEW
    assert handoff.token == "acme/widgets#7"


@pytest.mark.parametrize("token", ["none", "operator", "None"])
def test_idle_tail_fires(token: str) -> None:
    assert isinstance(tail_decision(f"NEXT: {token}", repo=_REPO, number=7), Post)


@pytest.mark.parametrize(
    "tail",
    ["NEXT: human", "MERGE-WAIT: x\nNEXT: human", "NEXT: Heisenberg", "NEXT: pr-review a/b#1", "x"],
)
def test_busy_tail_queues(tail: str) -> None:
    assert tail_decision(tail, repo=_REPO, number=7) is AutoGateNoop.QUEUED_BEHIND


# ---- the pass: msg-7735 table ------------------------------------------------------------------ #


async def test_old_noops_do_not_use_slots(tmp_path: Path) -> None:
    prs = [
        _pr(1, login="dependabot[bot]"),
        _pr(2, body="Thread: T-x\nGate: hold"),
        _pr(3, body="no link"),
        _pr(4),
    ]
    mcp = FakeMcp()
    report = await _run(mcp, FakeGh(prs), tmp_path)
    assert _posted_refs(mcp) == ["acme/widgets#4"]
    assert report.fired == 1


async def test_five_targets_fire_oldest_three_then_the_rest(tmp_path: Path) -> None:
    prs = [_pr(n, body=f"Thread: T-{n}") for n in (5, 3, 1, 4, 2)]
    threads = {f"T-{n}": [_msg("NEXT: none")] for n in range(1, 6)}
    mcp = FakeMcp(threads)
    first = await _run(mcp, FakeGh(prs), tmp_path)
    assert _posted_refs(mcp) == ["acme/widgets#1", "acme/widgets#2", "acme/widgets#3"]
    assert first.fired == 3
    second = await _run(mcp, FakeGh(prs), tmp_path)
    assert _posted_refs(mcp)[3:] == ["acme/widgets#4", "acme/widgets#5"]
    assert second.fired == 5
    assert second.noops["already-auto-fired"] == [
        "acme/widgets#1",
        "acme/widgets#2",
        "acme/widgets#3",
    ]


async def test_cap_minus_one_fires_one_and_next_tick_lists_nothing(tmp_path: Path) -> None:
    _seed(tmp_path, 29)
    prs = [_pr(n, body=f"Thread: T-{n}") for n in (1, 2, 3)]
    mcp = FakeMcp({f"T-{n}": [_msg("NEXT: none")] for n in (1, 2, 3)})
    gh = FakeGh(prs)
    await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp) == ["acme/widgets#1"]
    assert _fired_state(tmp_path)["fired"] == 30
    assert gh.list_calls == 1
    report = await _run(mcp, gh, tmp_path)
    assert gh.list_calls == 1  # the listing was not called again
    assert report.status == "cap_reached"
    assert report.message == "auto-gate: daily cap reached (30/30), pass skipped"
    assert len(mcp.posts) == 1


async def test_failed_post_is_recorded_uses_no_slot_and_is_retried(tmp_path: Path) -> None:
    prs = [_pr(n, body=f"Thread: T-{n}") for n in (1, 2, 3, 4)]
    mcp = FakeMcp(
        {f"T-{n}": [_msg("NEXT: none")] for n in (1, 2, 3, 4)},
        fail_posts={"acme/widgets#1": MagickitMcpError("boom")},
    )
    gh = FakeGh(prs)
    report = await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp) == ["acme/widgets#2", "acme/widgets#3", "acme/widgets#4"]
    assert report.fired == 3
    assert report.failures[0]["pr_ref"] == "acme/widgets#1"
    assert mcp.threads["T-1"] == [_msg("NEXT: none")]  # no record, so it stays eligible
    mcp.fail_posts = {}
    await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp)[-1] == "acme/widgets#1"


async def test_failed_post_is_not_counted(tmp_path: Path) -> None:
    mcp = FakeMcp(fail_posts={"acme/widgets#7": MagickitMcpError("boom")})
    report = await _run(mcp, FakeGh([_pr()]), tmp_path)
    assert report.fired == 0
    assert not (tmp_path / "state" / "auto-gate-daily.json").exists()


async def test_resolved_thread_at_write_time_is_reported_not_raised(tmp_path: Path) -> None:
    mcp = FakeMcp(fail_posts={"acme/widgets#7": ThreadResolvedError("resolved")})
    report = await _run(mcp, FakeGh([_pr()]), tmp_path)
    assert report.failures[0]["reason"] == "thread-resolved"
    assert report.fired == 0


async def test_dry_run_judges_all_posts_nothing_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path, 30)  # dry-run is not stopped by the cap
    prs = [_pr(n, body=f"Thread: T-{n}") for n in range(1, 6)]
    mcp = FakeMcp({f"T-{n}": [_msg("NEXT: none")] for n in range(1, 6)})
    report = await _run(mcp, FakeGh(prs), tmp_path, config=AutoGateConfig())
    assert [d["result"] for d in report.decisions] == ["would-fire"] * 5
    assert mcp.posts == []
    assert _fired_state(tmp_path) == {"date_jst": _TODAY, "fired": 30}
    assert report.as_dict()["mode"] == "dry-run"


async def test_dry_run_is_the_default() -> None:
    assert AutoGateConfig().live is False
    assert parse_auto_gate_config({}).live is False


async def test_live_noops_collapse_to_one_entry_per_reason(tmp_path: Path) -> None:
    prs = [_pr(1, draft=True), _pr(2, draft=True), _pr(3, base="feature/x")]
    report = await _run(FakeMcp(), FakeGh(prs), tmp_path)
    out = report.as_dict()
    assert out["noops"] == {
        "draft": ["acme/widgets#1", "acme/widgets#2"],
        "stacked": ["acme/widgets#3"],
    }
    assert "decisions" not in out


# ---- the pass: msg-7619 counter ---------------------------------------------------------------- #


async def test_jst_date_change_resets_and_lists(tmp_path: Path) -> None:
    _seed(tmp_path, 30, "2026-10-31")
    mcp = FakeMcp()
    gh = FakeGh([_pr()])
    report = await _run(mcp, gh, tmp_path)
    assert gh.list_calls == 1
    assert report.fired == 1
    assert _fired_state(tmp_path)["date_jst"] == _TODAY


async def test_the_date_boundary_is_jst_not_utc(tmp_path: Path) -> None:
    _seed(tmp_path, 30, _TODAY)
    # 2026-11-01 15:30 UTC is already 2026-11-02 00:30 JST.
    report = await _run(
        FakeMcp(), FakeGh([_pr()]), tmp_path, now=datetime(2026, 11, 1, 15, 30, tzinfo=UTC)
    )
    assert report.status == "ran"
    assert report.date_jst == "2026-11-02"


async def test_thirty_first_is_refused(tmp_path: Path) -> None:
    _seed(tmp_path, 30)
    mcp = FakeMcp()
    report = await _run(mcp, FakeGh([_pr()]), tmp_path)
    assert report.status == "cap_reached"
    assert mcp.posts == []


async def test_corrupt_counter_fails_closed_live(tmp_path: Path) -> None:
    path = _seed(tmp_path, 1)
    path.write_text("{not json", encoding="utf-8")
    mcp = FakeMcp()
    gh = FakeGh([_pr()])
    report = await _run(mcp, gh, tmp_path)
    assert report.status == "counter_unreadable"
    assert gh.list_calls == 0
    assert mcp.posts == []


async def test_counter_write_failure_never_double_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: DailyCounterStore, counter: Any) -> None:
        raise OSError("disk")

    monkeypatch.setattr(DailyCounterStore, "save", boom)
    mcp = FakeMcp()
    gh = FakeGh([_pr()])
    first = await _run(mcp, gh, tmp_path)
    assert len(mcp.posts) == 1
    assert "counter-write" in first.failures[0]["reason"]
    second = await _run(mcp, gh, tmp_path)
    assert len(mcp.posts) == 1
    assert second.noops["already-auto-fired"] == ["acme/widgets#7"]


# ---- the pass: msg-7737 table ------------------------------------------------------------------ #


async def test_record_and_no_verdict_is_noop_and_reads_no_reviews(tmp_path: Path) -> None:
    mcp = FakeMcp({"T-x": [_marker_post(), _msg("NEXT: none")]})
    gh = FakeGh([_pr()])
    report = await _run(mcp, gh, tmp_path)
    assert report.noops == {"already-auto-fired": ["acme/widgets#7"]}
    assert mcp.posts == []
    assert gh.review_calls == 0


async def test_head_moved_after_the_record_stays_noop(tmp_path: Path) -> None:
    mcp = FakeMcp({"T-x": [_marker_post(), _msg("NEXT: none")]})
    report = await _run(mcp, FakeGh([_pr(head=_OLD)]), tmp_path)
    assert report.noops == {"already-auto-fired": ["acme/widgets#7"]}
    assert mcp.posts == []


def _review(commit: str, state: str = "CHANGES_REQUESTED", login: str = _LOGIN) -> ReviewInfo:
    return ReviewInfo(login=login, state=state, commit_id=commit, submitted_at="2026-10-30T00:00Z")


async def test_current_head_review_is_already_gated(tmp_path: Path) -> None:
    mcp = FakeMcp()
    report = await _run(mcp, FakeGh([_pr()], reviews=[_review(_HEAD)]), tmp_path)
    assert report.noops == {"already-gated": ["acme/widgets#7"]}
    assert mcp.posts == []


async def test_current_head_relay_verdict_is_already_gated(tmp_path: Path) -> None:
    verdict = _msg(
        f"{render_relay_heading('acme/widgets#7', _HEAD)}\n\nVERDICT: approve\n\nNEXT: none",
        RELAY_AUTHOR,
    )
    gh = FakeGh([_pr()])
    report = await _run(FakeMcp({"T-x": [verdict]}), gh, tmp_path)
    assert report.noops == {"already-gated": ["acme/widgets#7"]}
    assert gh.review_calls == 0


async def test_old_head_only_verdict_fires_once(tmp_path: Path) -> None:
    """The vw#339 shape, called out in msg-7737 as a fire to check in the dry-run."""
    mcp = FakeMcp()
    gh = FakeGh([_pr()], reviews=[_review(_OLD)])
    await _run(mcp, gh, tmp_path)
    assert len(mcp.posts) == 1
    report = await _run(mcp, gh, tmp_path)
    assert len(mcp.posts) == 1
    assert report.noops == {"already-auto-fired": ["acme/widgets#7"]}


@pytest.mark.parametrize(
    "review",
    [_review(_HEAD, login="copilot"), _review(_HEAD, state="DISMISSED")],
)
async def test_other_reviewers_and_dismissed_reviews_are_not_verdicts(
    tmp_path: Path, review: ReviewInfo
) -> None:
    mcp = FakeMcp()
    await _run(mcp, FakeGh([_pr()], reviews=[review]), tmp_path)
    assert len(mcp.posts) == 1


async def test_unreadable_reviews_fail_closed_for_that_pr_only(tmp_path: Path) -> None:
    mcp = FakeMcp()
    gh = FakeGh([_pr()], review_error=RuntimeError("503"))
    report = await _run(mcp, gh, tmp_path)
    assert mcp.posts == []
    assert report.failures[0]["reason"].startswith("gh-error")


# ---- the pass: other rows ---------------------------------------------------------------------- #


async def test_finished_thread_is_a_noop(tmp_path: Path) -> None:
    mcp = FakeMcp(statuses={"T-x": "resolved"})
    report = await _run(mcp, FakeGh([_pr()]), tmp_path)
    assert report.noops == {"thread-finished": ["acme/widgets#7"]}
    assert mcp.posts == []


async def test_unreadable_thread_is_reported(tmp_path: Path) -> None:
    report = await _run(FakeMcp({}), FakeGh([_pr()]), tmp_path)
    assert report.failures[0]["reason"].startswith("thread-unreadable")


async def test_unparsed_and_ambiguous_lines_reach_the_log(tmp_path: Path) -> None:
    prs = [_pr(1, body="Thread: 未定"), _pr(2, body="Thread: T-a,T-b")]
    report = await _run(FakeMcp(), FakeGh(prs), tmp_path)
    assert report.details[0]["lines"] == ["Thread: 未定"]
    assert report.details[1]["ids"] == ["T-a", "T-b"]
    json.dumps(report.as_dict(), ensure_ascii=True)


async def test_queued_behind_reports_the_fields_msg_7485_names(tmp_path: Path) -> None:
    mcp = FakeMcp({"T-x": [_msg("NEXT: Heisenberg", msg_id="msg-55")]})
    report = await _run(mcp, FakeGh([_pr()]), tmp_path)
    detail = report.details[0]
    assert detail["reason"] == "queued-behind"
    assert detail["thread_id"] == "T-x"
    assert detail["tail_next"] == "Heisenberg"
    assert detail["tail_msg_id"] == "msg-55"
    assert detail["waiting_since"].startswith("2026-10-31")


async def test_one_repos_listing_failure_costs_only_that_repo(tmp_path: Path) -> None:
    class Flaky(FakeGh):
        async def list_open_gate_prs(self, owner: str, repo: str) -> GatePrListing:
            if repo == "broken":
                raise OpenPrListingError("down", outcome="http_error")
            return await super().list_open_gate_prs(owner, repo)

    mcp = FakeMcp()
    report = await run_pass(
        mcp=mcp,
        gh=Flaky([_pr()]),
        targets=[RepoTarget("proj", "acme", "broken"), _TARGET],
        config=_LIVE,
        store=DailyCounterStore(tmp_path / "c.json"),
        review_login=_LOGIN,
        now=_NOW,
    )
    assert report.list_errors[0]["repo"] == "acme/broken"
    assert len(mcp.posts) == 1


# ---- msg-7485: two PRs on one thread, the three orders ---------------------------------- #


async def test_second_pr_fires_after_the_first_merges_with_none(tmp_path: Path) -> None:
    mcp = FakeMcp()
    gh = FakeGh([_pr(1), _pr(2)])
    await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp) == ["acme/widgets#1"]
    report = await _run(mcp, gh, tmp_path)  # 1 is in flight: the tail is its pr-review
    assert report.noops["queued-behind"] == ["acme/widgets#2"]
    mcp.threads["T-x"].append(_msg("APPROVE\nMERGE-WAIT: x\nNEXT: human", RELAY_AUTHOR))
    assert (await _run(mcp, gh, tmp_path)).noops["queued-behind"] == ["acme/widgets#2"]
    mcp.threads["T-x"].append(_msg("merged\n\nNEXT: none", PR_EVENT_RELAY_AUTHOR))
    await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp) == ["acme/widgets#1", "acme/widgets#2"]


async def test_second_pr_waits_through_request_changes(tmp_path: Path) -> None:
    mcp = FakeMcp()
    gh = FakeGh([_pr(1), _pr(2)])
    await _run(mcp, gh, tmp_path)
    mcp.threads["T-x"].append(_msg("RC\n\nNEXT: Heisenberg", RELAY_AUTHOR))
    assert (await _run(mcp, gh, tmp_path)).noops["queued-behind"] == ["acme/widgets#2"]
    mcp.threads["T-x"].append(_msg("fixed\n\nNEXT: pr-review acme/widgets#1", "Heisenberg"))
    assert (await _run(mcp, gh, tmp_path)).noops["queued-behind"] == ["acme/widgets#2"]
    mcp.threads["T-x"].append(_msg("merged\n\nNEXT: none", PR_EVENT_RELAY_AUTHOR))
    await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp)[-1] == "acme/widgets#2"


async def test_second_pr_fires_after_close_and_proposers_none(tmp_path: Path) -> None:
    mcp = FakeMcp()
    gh = FakeGh([_pr(1), _pr(2)])
    await _run(mcp, gh, tmp_path)
    mcp.threads["T-x"].append(_msg("closed\n\nNEXT: Bohr", PR_EVENT_RELAY_AUTHOR))
    assert (await _run(mcp, gh, tmp_path)).noops["queued-behind"] == ["acme/widgets#2"]
    mcp.threads["T-x"].append(_msg("STOP: done\nNEXT: none", "Bohr"))
    await _run(mcp, gh, tmp_path)
    assert _posted_refs(mcp)[-1] == "acme/widgets#2"


# ---- configuration and CLI helpers ----------------------------------------------------------- #


def test_config_reads_every_key() -> None:
    config = parse_auto_gate_config(
        {"auto_gate": {"mode": "on", "auto_gate_daily_cap": 5, "gate_bases": ["trunk"]}}
    )
    assert config == AutoGateConfig(live=True, daily_cap=5, gate_bases=("trunk",))


def test_config_defaults_are_the_design_values() -> None:
    assert parse_auto_gate_config({"auto_gate": {}}) == AutoGateConfig(
        live=False, daily_cap=30, gate_bases=("main", "develop")
    )


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"mode": "maybe"},
        {"auto_gate_daily_cap": -1},
        {"auto_gate_daily_cap": True},
        {"gate_bases": []},
        {"gate_bases": "main"},
    ],
)
def test_bad_config_is_refused(raw: Any) -> None:
    with pytest.raises(AutoGateConfigError):
        parse_auto_gate_config({"auto_gate": raw})


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widgets.git\n",
        "https://github.com/acme/widgets",
        "git@github.com:acme/widgets.git",
    ],
)
def test_remote_url(url: str) -> None:
    assert parse_remote_url(url) == ("acme", "widgets")


def test_non_github_remote_is_none() -> None:
    assert parse_remote_url("https://example.com/acme/widgets.git") is None


def test_cap_line_shows_today_and_yesterday_only() -> None:
    def report(capped: str) -> PassReport:
        return PassReport(cap=30, capped_on=capped)

    assert "2026-11-01" in (cap_digest_line(report(_TODAY), _NOW) or "")
    assert cap_digest_line(report("2026-10-31"), _NOW) is not None
    assert cap_digest_line(report("2026-10-30"), _NOW) is None
    assert cap_digest_line(PassReport(cap=30), _NOW) is None


async def test_hitting_the_cap_records_the_day_for_the_digest(tmp_path: Path) -> None:
    _seed(tmp_path, 29)
    await _run(FakeMcp(), FakeGh([_pr()]), tmp_path)
    assert _fired_state(tmp_path)["capped_on"] == _TODAY
    # The next day's fire resets the count but keeps the day the digest has yet to show.
    await _run(FakeMcp(), FakeGh([_pr()]), tmp_path, now=_NOW + timedelta(days=1))
    state = _fired_state(tmp_path)
    assert state["fired"] == 1
    assert state["capped_on"] == _TODAY


# ---- the GitHub listing ---------------------------------------------------------------------- #


def _listing_row(number: int, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": number,
        "draft": False,
        "head": {"sha": _HEAD},
        "base": {"ref": "develop"},
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "body": "Thread: T-x",
        "created_at": "2026-10-01T00:00:00Z",
    }
    row.update(over)
    return row


def test_gate_listing_reads_the_fields_1c_decides_on_and_counts_bad_rows() -> None:
    rows = [
        _listing_row(1),
        _listing_row(2, body=None),
        _listing_row(3, base=None),
        _listing_row(4, user=None),
    ]
    client = GitHubClient(
        "tok", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows))
    )
    listing = asyncio.run(client.list_open_gate_prs("acme", "widgets"))
    assert listing.examined == 4
    assert listing.unrecognized == 2
    first, second = listing.prs
    assert (first.base_ref, first.author_login, first.author_type, first.body) == (
        "develop",
        "dependabot[bot]",
        "Bot",
        "Thread: T-x",
    )
    assert second.body == ""


def test_gate_listing_failure_is_loud() -> None:
    client = GitHubClient(
        "tok", transport=httpx.MockTransport(lambda _: httpx.Response(500, json={}))
    )
    with pytest.raises(OpenPrListingError):
        asyncio.run(client.list_open_gate_prs("acme", "widgets"))
