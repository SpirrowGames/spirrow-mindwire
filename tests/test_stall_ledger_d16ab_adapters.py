"""D-16ab adapters and the CLI (T-stalled-pr-has-no-detector msg-4685 §3, §7-6;
msg-4697/4699 CLI). The listing snapshot and ReadOnlyMcp tests live in
``test_stall_ledger_d16ab_github_readonly.py``."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.github.client import (
    CheckRollup,
    OpenPr,
    OpenPrListingError,
    PrRef,
    PrResolution,
    PrState,
    ReviewInfo,
)
from spirrow_mindwire.magickit.read_only import ReadOnlyViolationError
from spirrow_mindwire.stall_ledger.adapters import (
    ChatroomThreadAdapter,
    GitHubOpenPrAdapter,
    Marked,
    MotionEvent,
    QuarantineFileAdapter,
    RemedyMarkerCodec,
    Unmarked,
    Unverifiable,
    build_pr_observation,
)
from spirrow_mindwire.stall_ledger.driver import TickPaths, run_tick
from spirrow_mindwire.stall_ledger.heartbeat import (
    FetchOutcome,
    HealthState,
    HeartbeatRecord,
    derive_state,
)
from spirrow_mindwire.stall_ledger.model import Unit, UnitKind

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 9, 1, tzinfo=UTC)


# ── GitHub mapping ────────────────────────────────────────────────────────────────────


def _obs(reviews: list[ReviewInfo], mergeable: str | None, ci: list[bool] | None = None) -> Any:
    pr = OpenPr(ref=PrRef("o", "r", 1), draft=False, head_sha="H", created_at=T0)
    return build_pr_observation(
        pr=pr,
        head_sha="H",
        reviews=reviews,
        mergeable_state=mergeable,
        head_committed_at=T0 + timedelta(hours=1),
        ci_rows_completed=ci or [],
    )


def _rv(state: str, sha: str = "H", at: str = "2026-09-02T00:00:00Z", rid: str = "1") -> ReviewInfo:
    return ReviewInfo(login="x", state=state, commit_id=sha, submitted_at=at, review_id=rid)


def test_pr_without_verdict_on_head_needs_actor() -> None:
    obs = _obs([_rv("APPROVED", sha="OLD")], "clean")
    assert obs.needs_actor is True
    assert obs.unit == Unit(UnitKind.PR, "o/r#1")
    assert [m.type for m in obs.motion] == ["head_push", "review"]


def test_pr_approved_clean_is_awaiting_human_merge_not_needs_actor() -> None:
    assert _obs([_rv("APPROVED")], "clean").needs_actor is False


def test_pr_request_changes_needs_actor() -> None:
    assert _obs([_rv("CHANGES_REQUESTED")], "clean").needs_actor is True


def test_pr_comment_verdict_is_indefinite_and_refireable_when_ci_definitive() -> None:
    obs = _obs([_rv("COMMENTED")], "clean", ci=[True, True])
    assert obs.needs_actor is True
    assert obs.classifier_input.verdict_is_indefinite
    assert obs.classifier_input.ci_became_definitive


def test_pr_dirty_is_externally_blocked() -> None:
    obs = _obs([_rv("APPROVED")], "dirty")
    assert obs.needs_actor is True
    assert obs.classifier_input.is_externally_blocked


def test_latest_verdict_on_head_wins() -> None:
    obs = _obs(
        [
            _rv("CHANGES_REQUESTED", at="2026-09-02T00:00:00Z", rid="1"),
            _rv("APPROVED", at="2026-09-03T00:00:00Z", rid="2"),
        ],
        "clean",
    )
    assert obs.needs_actor is False


def test_pr_with_no_review_at_all_has_no_verdict_and_needs_actor() -> None:
    """msg-5157 §3: no review on the head (here: none at all) = "no verdict", loud side."""
    obs = _obs([], "clean")
    assert obs.needs_actor is True
    assert not obs.classifier_input.verdict_is_indefinite
    assert [m.type for m in obs.motion] == ["head_push"]


# msg-5157 §3: one row per observed mergeable_state. Only clean/has_hooks are executable
# (so APPROVED is "awaiting human merge"); only dirty is an external block.
@pytest.mark.parametrize(
    ("mergeable", "executable", "externally_blocked"),
    [
        ("clean", True, False),
        ("has_hooks", True, False),
        ("dirty", False, True),
        ("blocked", False, False),
        ("behind", False, False),
        ("unstable", False, False),
    ],
)
def test_observed_mergeable_states_map(
    mergeable: str, executable: bool, externally_blocked: bool
) -> None:
    obs = _obs([_rv("APPROVED")], mergeable)
    assert obs.needs_actor is (not executable)
    assert obs.classifier_input.is_externally_blocked is externally_blocked
    gh = _FakeGitHub()
    gh.mergeable = {1: mergeable, 2: mergeable}
    result = asyncio.run(GitHubOpenPrAdapter(gh, "o", "r").fetch())  # type: ignore[arg-type]
    assert result.unobservable == frozenset()
    assert len(result.observations) == 2


@pytest.mark.parametrize("mergeable", ["unknown", None])
def test_uncomputed_mergeable_state_is_unobservable_and_closes_nothing(
    tmp_path: Path, mergeable: str | None
) -> None:
    """msg-5157 §3 (hard requirement): ``unknown`` / null -> unobservable this tick.

    PR #1 carries an APPROVE on its head, so if ``unknown`` / null leaked through as
    executable it would read as not-needs_actor and close by rule (a); if it leaked as
    ``dirty`` it would read as an external block. Neither may happen: the PR is
    reported unobservable and its open record survives untouched.
    """
    gh = _FakeGitHub()
    gh.mergeable = {1: "blocked", 2: "blocked"}
    gh.reviews = {1: [_rv("APPROVED", sha="s1", rid="r1")]}
    adapter = GitHubOpenPrAdapter(gh, "o", "r")  # type: ignore[arg-type]
    state = tmp_path / "state"

    def tick(now: datetime) -> Any:
        return asyncio.run(
            run_tick(
                paths=TickPaths(state_dir=state),
                adapters=[adapter],
                now=lambda: now,
                out=io.StringIO(),
            )
        )

    tick(NOW)
    store_path = state / "stall-ledger.json"
    before = json.loads(store_path.read_text(encoding="utf-8"))["records"]
    assert "pr:o/r#1" in before, "precondition: the record must be open"

    gh.mergeable = {1: mergeable, 2: "blocked"}
    result = asyncio.run(adapter.fetch())
    assert result.unobservable == frozenset({"pr:o/r#1"})
    assert [o.unit.key for o in result.observations] == ["pr:o/r#2"]
    assert (result.report.recognized, result.report.unrecognized) == (1, 1)

    out = tick(NOW + timedelta(hours=1))
    assert [line for line in out.lines if line["kind"] == "close"] == []
    after = json.loads(store_path.read_text(encoding="utf-8"))["records"]
    assert after["pr:o/r#1"] == before["pr:o/r#1"]


class _FakeGitHub:
    def __init__(self, *, listing_error: str | None = None, fail_pr: int | None = None) -> None:
        self.listing_error = listing_error
        self.fail_pr = fail_pr
        self.bodies: dict[str, str | None] = {}
        # Per-PR ``mergeable_state``; a PR not named here reads "clean".
        self.mergeable: dict[int, str | None] = {}
        self.reviews: dict[int, list[ReviewInfo]] = {}

    async def list_open_prs(self, owner: str, repo: str) -> Any:
        from spirrow_mindwire.github.client import OpenPrListing

        if self.listing_error:
            raise OpenPrListingError("x", outcome=self.listing_error)
        prs = tuple(
            OpenPr(ref=PrRef(owner, repo, n), draft=False, head_sha=f"s{n}", created_at=T0)
            for n in (1, 2)
        )
        return OpenPrListing(prs=prs, examined=2, unrecognized=0)

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]:
        from spirrow_mindwire.github.client import GitHubHTTPError

        if pr.number == self.fail_pr:
            raise GitHubHTTPError("boom")
        return list(self.reviews.get(pr.number, []))

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        return PrState(
            ref=pr,
            resolution=PrResolution.OPEN,
            head_sha=f"s{pr.number}",
            mergeable_state=self.mergeable.get(pr.number, "clean"),
        )

    async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup:
        return CheckRollup(head_sha="s", head_committed_date=T0, head_pushed_at=T0, rows=())

    async def fetch_review_body(self, pr: PrRef, review_id: str) -> str | None:
        from spirrow_mindwire.github.client import GitHubHTTPError

        if review_id == "err":
            raise GitHubHTTPError("x")
        return self.bodies.get(review_id)


def test_github_adapter_accounting_and_unobservable() -> None:
    adapter = GitHubOpenPrAdapter(_FakeGitHub(fail_pr=2), "o", "r")  # type: ignore[arg-type]
    result = asyncio.run(adapter.fetch())
    r = result.report
    assert (r.examined, r.recognized, r.unrecognized) == (2, 1, 1)
    assert result.unobservable == frozenset({"pr:o/r#2"})
    assert result.complete is True
    assert result.scope(Unit(UnitKind.PR, "o/r#9"))
    assert not result.scope(Unit(UnitKind.PR, "o/rr#9"))


def test_github_adapter_listing_failure() -> None:
    adapter = GitHubOpenPrAdapter(_FakeGitHub(listing_error="auth_failure"), "o", "r")  # type: ignore[arg-type]
    result = asyncio.run(adapter.fetch())
    assert result.report.fetch_outcome == FetchOutcome.AUTH_FAILURE
    assert result.complete is False


def test_github_check_marker_paths() -> None:
    fake = _FakeGitHub()
    fake.bodies = {"m": RemedyMarkerCodec.encode("a1") + "\n", "u": "hello"}
    adapter = GitHubOpenPrAdapter(fake, "o", "r")  # type: ignore[arg-type]
    unit = Unit(UnitKind.PR, "o/r#1")

    def check(rid: str) -> Any:
        return asyncio.run(adapter.check_marker(unit, MotionEvent("review", rid, T0)))

    assert check("m") == Marked("a1")
    assert check("u") == Unmarked()
    assert check("gone") == Unverifiable("deleted")
    assert check("err") == Unverifiable("fetch_failed")


# ── chatroom ──────────────────────────────────────────────────────────────────────────


class _FakeMcp:
    def __init__(self, items: list[Any], messages: list[dict[str, Any]] | None = None) -> None:
        self.items = items
        self.messages = messages or []
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "chatroom_list_threads":
            off = arguments["offset"]
            return {"items": self.items[off : off + 1], "total": len(self.items)}
        if name == "chatroom_get_thread":
            return {"messages": self.messages}
        raise AssertionError(name)


def _thread(tid: str, status: str = "active") -> dict[str, Any]:
    return {
        "thread_id": tid,
        "status": status,
        "created_at": "2026-09-01T00:00:00Z",
        "last_msg_id": f"m-{tid}",
        "last_activity_at": "2026-09-02T00:00:00Z",
    }


def test_chatroom_adapter_pages_and_accounts() -> None:
    mcp = _FakeMcp([_thread("T-a"), _thread("T-b", "resolved"), {"thread_id": 3}])
    result = asyncio.run(ChatroomThreadAdapter(mcp, "p").fetch())
    r = result.report
    assert (r.examined, r.recognized, r.unrecognized) == (3, 2, 1)
    needs = {o.unit.key: o.needs_actor for o in result.observations}
    assert needs == {"thread:p/T-a": True, "thread:p/T-b": False}
    assert result.observations[0].motion == (
        MotionEvent("chatroom_msg", "m-T-a", datetime(2026, 9, 2, tzinfo=UTC)),
    )


def test_chatroom_adapter_is_read_only() -> None:
    class Writer(_FakeMcp):
        pass

    adapter = ChatroomThreadAdapter(Writer([]), "p")
    with pytest.raises(ReadOnlyViolationError):
        asyncio.run(adapter._mcp.call_tool("chatroom_post_message", {}))


def test_chatroom_check_marker_paths() -> None:
    msgs = [
        {"msg_id": "m1", "content": RemedyMarkerCodec.encode("z") + "\nbody"},
        {"msg_id": "m2", "content": "plain"},
    ]
    adapter = ChatroomThreadAdapter(_FakeMcp([], msgs), "p")
    unit = Unit(UnitKind.THREAD, "p/T-a")

    def check(mid: str) -> Any:
        return asyncio.run(adapter.check_marker(unit, MotionEvent("chatroom_msg", mid, T0)))

    assert check("m1") == Marked("z")
    assert check("m2") == Unmarked()
    assert check("m3") == Unverifiable("deleted")


def test_chatroom_fetch_failure() -> None:
    class Broken:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise RuntimeError("down")

    result = asyncio.run(ChatroomThreadAdapter(Broken(), "p").fetch())
    assert result.report.fetch_outcome == FetchOutcome.HTTP_ERROR
    assert not result.complete


# ── quarantine.json ───────────────────────────────────────────────────────────────────


def test_quarantine_adapter_states(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.json"
    missing = asyncio.run(QuarantineFileAdapter(path).fetch())
    assert missing.report.fetch_outcome == FetchOutcome.FILE_MISSING
    path.write_text("{broken", encoding="utf-8")
    assert asyncio.run(QuarantineFileAdapter(path).fetch()).report.fetch_outcome == (
        FetchOutcome.PARSE_ERROR
    )
    path.write_text(
        json.dumps({"p/T-a": {"first_failure_at": "2026-09-01T00:00:00Z"}, "p/T-b": {"x": 1}}),
        encoding="utf-8",
    )
    ok = asyncio.run(QuarantineFileAdapter(path).fetch())
    r = ok.report
    assert (r.examined, r.recognized, r.unrecognized) == (2, 1, 1)
    [obs] = ok.observations
    assert obs.unit == Unit(UnitKind.QUARANTINE, "p/T-a")
    assert obs.needs_actor and obs.n == timedelta(0) and obs.motion == ()
    assert obs.classifier_input.is_quarantined


# ── §7-6: one outage does not hide the others; sweep.json is never opened ─────────────


def test_one_source_outage_does_not_hide_the_others(tmp_path: Path) -> None:
    good = asyncio.run(ChatroomThreadAdapter(_FakeMcp([_thread("T-a")]), "p").fetch())
    bad = asyncio.run(QuarantineFileAdapter(tmp_path / "nope.json").fetch())
    record = HeartbeatRecord.build(
        evaluated_at=NOW,
        input_format_version=good.report.observed_format_version,
        sources=(good.report, bad.report),
        previous_last_valid_ingest_at=None,
    )
    assert derive_state(record) == HealthState.INGEST_FAILURE
    assert [s.name for s in record.failing_sources()] == ["quarantine"]


def test_a_tick_never_opens_sweep_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "quarantine.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sweep.json").write_text("{}", encoding="utf-8")
    opened: list[str] = []
    real_open = builtins.open
    real_read_text = Path.read_text

    def spy_open(file: Any, *a: Any, **k: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_read_text(self: Path, *a: Any, **k: Any) -> str:
        opened.append(str(self))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=state),
            adapters=[
                QuarantineFileAdapter(state / "quarantine.json"),
                ChatroomThreadAdapter(_FakeMcp([_thread("T-a")]), "p"),
            ],
            now=lambda: NOW,
            out=io.StringIO(),
        )
    )
    assert opened, "the spy saw nothing -- it is not wired"
    assert not [p for p in opened if p.endswith("sweep.json")]


def _load_script(name: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_d16ab_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── CLI (msg-4697 §2 / msg-4699 §2) ───────────────────────────────────────────────────


def test_cli_clear_writes_request_and_list_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_script("stall_ledger_tick")
    state = tmp_path / "state"
    state.mkdir()
    store = state / "stall-ledger.json"
    q = {"pr:o/r#1": {"reason": "key_mismatch", "count": 2}}
    store.write_text(json.dumps({"schema_version": "1.0", "records": {}, "quarantined_records": q}))
    before = store.read_bytes()
    assert (
        cli.main(
            ["--data-dir", str(tmp_path), "--clear-quarantined", "pr:o/r#1", "--requested-by", "op"]
        )
        == 0
    )
    assert store.read_bytes() == before
    assert len(list((state / "stall-ledger.requests").glob("clear-*.json"))) == 1
    capsys.readouterr()
    assert cli.main(["--data-dir", str(tmp_path), "--list-quarantined"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["quarantined_records"] == q
    assert [r["unit_key"] for r in listing["pending_clear_requests"]] == ["pr:o/r#1"]
    assert store.read_bytes() == before
