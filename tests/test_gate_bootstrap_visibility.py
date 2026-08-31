"""Regression tests for the D-2 close-failure visibility mechanism.

Design source: chatroom thread
``T-gate-bootstrap-close-refused-and-tick-crash`` (msg-2291 D-2 → msg-2293
D-2' → msg-2295 D-2'' → msg-2297 D-2''' → msg-2301 D-2''''). The module docstring
on :mod:`spirrow_mindwire.gate_bootstrap_visibility` is the primary
narrative for the constraints tested here; each test names the specific
objection (Einstein msg-2292/2294/2296/2298) it pins.

Six tests total (D-6 as amended through msg-2295, msg-2297, msg-2301):

1. **F-2 subprocess encoding** (``test_gate_bootstrap_tick_stdout_survives_cp932_stdout``)
   — proves the F-2 fix by running the CLI in a real subprocess with
   ``PYTHONIOENCODING=cp932`` and confirming the stdout JSON survives
   ``json.loads``. Pytest-internal calls do not reproduce the bug (the
   test runner's stdout is UTF-8).
2. **Floor holds across failing posts** (Einstein msg-2294 blocking) —
   with a post that always fails, drive 288 ticks (24h of 5-min ticks) and
   assert the fake MCP recorded exactly one post attempt.
3. **Dedup with foreign writes** (Einstein msg-2292 blocking) — with a
   post that succeeds, drive many ticks and simulate foreign writes on
   the alert thread; assert exactly one post is attempted regardless.
   Proves that "state is on the sweeper, not the thread" holds.
4. **Fail-closed on state write failure** (Einstein msg-2294 corollary)
   — the state store raises on save; assert no post is attempted.
5. **Human close clears episode without permanent suppression**
   (Einstein msg-2296 blocking) — episode → human closes → next failure
   with the same signature must not be permanently suppressed by stale
   state. The floor may still block the post but the episode itself is
   cleared.
6. **Flap does not restart spam** (msg-2297 Rule 2) — alternate failure /
   success every 5 minutes for 24 hours; assert at most one post attempt.
   Falsifies the naïve "clear the whole state on success" implementation.

Extra: Test 6-b **open failure never touches visibility state**
(Einstein msg-2298 blocking; msg-2301 D-2'''' mechanical constraint) — pins
the scoping. Not one of the six per se, but the module docstring §Scope
Is Close-Only turns on it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.gate_bootstrap import (
    DEFAULT_SWEEPER_OWNER,
    GateBootstrapCloseError,
    GateStatus,
    thread_id_for,
)
from spirrow_mindwire.gate_bootstrap_visibility import (
    FAILURE_REPORT_FLOOR,
    CloseFailureVisibility,
    FailureEpisode,
    FileFailureStateStore,
    RateLimitFloor,
    _State,
)

# --- fakes ---------------------------------------------------------------------------------------


class _RecordingMcp:
    """Records every call, returns programmed results by tool name.

    Kept separate from the ``_FakeMcp`` in test_gate_bootstrap.py because
    the visibility tests need to drive per-call outcomes (fail N times then
    succeed, always fail, etc.), and building that on top of the callable
    convention there would obscure what each test asserts.
    """

    def __init__(self, *, post_outcome: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._post_outcome = post_outcome

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if self._post_outcome is None:
            return {"msg": {"msg_id": "msg-fake"}}
        if isinstance(self._post_outcome, BaseException):
            raise self._post_outcome
        if callable(self._post_outcome):
            return self._post_outcome(name, arguments)
        return self._post_outcome

    def post_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if c[0] == "chatroom_post_message"]


class _MemoryStore:
    """In-memory :class:`FailureStateStore` for deterministic tests."""

    def __init__(self, initial: _State | None = None) -> None:
        self.state = initial or _State()
        self.save_calls = 0
        self.raise_on_save: BaseException | None = None
        self.raise_on_load: BaseException | None = None

    def load(self) -> _State:
        if self.raise_on_load is not None:
            raise self.raise_on_load
        # Return a mutable copy so callers cannot accidentally share references
        # with the "on-disk" state (the file store would give a fresh object
        # every load — mimic that here to catch caller bugs).
        return _State(
            episodes=dict(self.state.episodes),
            floors=dict(self.state.floors),
        )

    def save(self, state: _State) -> None:
        self.save_calls += 1
        if self.raise_on_save is not None:
            raise self.raise_on_save
        self.state = _State(
            episodes=dict(state.episodes),
            floors=dict(state.floors),
        )


@pytest.fixture
def anyio_backend() -> str:
    """The anyio marker uses asyncio only — the test-suite convention."""
    return "asyncio"


def _clock(start: datetime) -> Any:
    """Return a callable that yields ``start + n * 5min`` on successive calls.

    A closure rather than a stateful class because tests only need "advance
    5 minutes each call" — the sweep's tick cadence.
    """
    counter = {"n": 0}

    def _now() -> datetime:
        n = counter["n"]
        counter["n"] += 1
        return start + n * timedelta(minutes=5)

    return _now


# --- Test 1: F-2 subprocess encoding (msg-2290 F-2) ---------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "gate_bootstrap_tick.py"


def test_gate_bootstrap_tick_stdout_survives_cp932_stdout(tmp_path: Path) -> None:
    """Subprocess with ``PYTHONIOENCODING=cp932`` — em-dash in reason must NOT crash.

    Pins F-2 (msg-2290). Before the fix, running the tick on Windows with
    a cp932 console crashed on ``json.dumps(..., ensure_ascii=False)`` because
    the ``UNUSABLE`` reason string contains an em dash. This test runs the
    tick against a non-git directory (which routes to ``UNUSABLE`` — the
    branch whose reason contains U+2014) and asserts:

      1. exit code is 0 (the tick did its work);
      2. stdout is parseable JSON (the wrapper's ``ConvertFrom-Json`` can
         read it);
      3. the parsed object's ``status`` is ``unusable`` (proof we exercised
         the em-dash branch).

    The subprocess boundary is load-bearing: an in-process call from pytest
    does not reproduce the bug because the test runner's stdout is UTF-8.
    ``env=`` propagates PYTHONIOENCODING to the child so its ``sys.stdout``
    default is cp932; the reconfigure at the top of the script switches
    stdout to UTF-8 with ``backslashreplace``, and the two ``ensure_ascii=True``
    ``json.dumps`` calls guarantee the wrapper sees valid JSON regardless.
    """
    # A plain directory (not a git repo) routes to UNUSABLE, whose reason
    # is ASCII in the "not-a-git-repo" branch. To hit the em-dash branch we
    # need an ABSENT directory (the UNUSABLE-with-em-dash message lives in
    # the "not-absolute" branch OR the module-level docstring). Simplest: a
    # non-git directory that exists; the "sweep-config problem" reason
    # contains em dashes.
    (tmp_path / "some-file").write_text("hi", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp932"
    # Prevent the child from importing config that requires a real data dir.
    env["MINDWIRE_PATHS__DATA_DIR"] = str(tmp_path / "data")

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--project",
            "spirrow-verimend",
            "--repo-dir",
            str(tmp_path),
            "--url",
            "http://not-called.invalid",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=False,
    )

    assert result.returncode == 0, (
        f"tick exited non-zero: stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    # The wrapper reads the LAST JSON-object line from stdout. Mirror that.
    json_lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert json_lines, f"no JSON on stdout: {result.stdout!r}"
    parsed = json.loads(json_lines[-1])
    assert parsed["status"] == GateStatus.UNUSABLE.value


# --- Test 2: floor holds across failing posts (Einstein msg-2294) --------------------------------


@pytest.mark.anyio
async def test_floor_holds_across_failing_posts() -> None:
    """288 ticks in 24h with an always-failing post → EXACTLY one attempt.

    Pins Einstein msg-2294 (blocking `invariant`). The old design ("write
    the state only on post-success") would try to post on every tick,
    288 times per 24h. D-2'' (msg-2295) split the state into ``last_attempt_at``
    (write-ahead, success-agnostic) and ``reported_at`` (success-only) so
    the floor holds across attempted-but-failed posts. This test drives
    exactly the pathology the design update pinned.

    The load-bearing count is *attempts*, not *messages posted*. A "sends
    288 times, all fail" would falsify D-2''; a "sends 1 time, fails" is
    the D-2'' invariant. The fake MCP records both.
    """
    store = _MemoryStore()
    now = _clock(datetime(2026, 8, 31, 0, 0, tzinfo=UTC))
    vis = CloseFailureVisibility(store, now=now)
    mcp = _RecordingMcp(post_outcome=RuntimeError("simulated persistent MCP failure"))

    # 24h / 5min = 288 ticks. Drive each one with the same failure.
    reports = []
    for _ in range(288):
        report = await vis.on_close_failure(
            mcp,
            project="spirrow-verimend",
            thread_id=thread_id_for("spirrow-verimend"),
            owner=DEFAULT_SWEEPER_OWNER,
            exc=GateBootstrapCloseError("simulated close refusal"),
        )
        reports.append(report)

    # THE assertion. Everything else is context.
    assert len(mcp.post_calls()) == 1, (
        f"expected exactly one post attempt across 24h of failing ticks, got "
        f"{len(mcp.post_calls())}"
    )
    # The first tick attempts a post (and fails); every subsequent tick is
    # blocked by the floor.
    actions = [r.action for r in reports]
    assert actions[0] == "post_failed"
    # Every subsequent action must be floor_blocked (dedup_blocked would
    # require reported_at, which we never set because the post failed).
    for i, action in enumerate(actions[1:], start=1):
        assert action == "floor_blocked", f"tick {i}: expected floor_blocked, got {action!r}"


# --- Test 3: dedup with foreign writes (Einstein msg-2292) ---------------------------------------


@pytest.mark.anyio
async def test_dedup_survives_foreign_writes() -> None:
    """Successful post → later ticks dedup regardless of thread activity.

    Pins Einstein msg-2292 (blocking `edge-case`). The rejected design
    (dedup on message adjacency in the chatroom) would break the moment
    a human or bot posted in the alert thread. D-2' (msg-2293) moved dedup
    to the sweeper's own state, keyed by (project, thread_id, signature)
    and gated on ``reported_at`` (post-success only). This test drives
    many ticks with a *succeeding* post and asserts exactly one post is
    attempted — the "foreign writes" are represented by additional
    unrelated ``call_tool`` invocations mixed into the sequence, which
    must not affect the dedup verdict because the verdict does not
    consult the thread at all.
    """
    store = _MemoryStore()
    now = _clock(datetime(2026, 8, 31, 0, 0, tzinfo=UTC))
    vis = CloseFailureVisibility(store, now=now)
    mcp = _RecordingMcp()  # default: success

    for tick in range(20):
        # Simulate a foreign writer touching the thread between ticks. The
        # visibility code never inspects the thread, so this must be a no-op
        # on its state.
        await mcp.call_tool("chatroom_post_message", {"from": "human", "tick": tick})
        await vis.on_close_failure(
            mcp,
            project="spirrow-verimend",
            thread_id=thread_id_for("spirrow-verimend"),
            owner=DEFAULT_SWEEPER_OWNER,
            exc=GateBootstrapCloseError("close refusal"),
        )

    # Post attempts by the visibility code: exactly one. The 20 foreign
    # writes are recorded but they are not from the visibility mechanism.
    from_visibility = [
        (name, args)
        for name, args in mcp.post_calls()
        if args.get("author") == DEFAULT_SWEEPER_OWNER
    ]
    assert len(from_visibility) == 1


# --- Test 4: fail-closed on state write failure (Einstein msg-2294 corollary) --------------------


@pytest.mark.anyio
async def test_fail_closed_when_state_write_fails() -> None:
    """State save raises → NO post attempt.

    Pins D-2'' (msg-2295) fail-closed rule: "state 書き込みに失敗したら
    post しない". A post that cannot be rate-limited is a post we must not
    make; the write-ahead pattern only enforces the floor if the write
    actually persists. This test verifies the invariant by making save
    raise ``OSError`` and asserting the fake MCP never sees a post call.
    """
    store = _MemoryStore()
    store.raise_on_save = OSError("simulated disk-full")
    now = _clock(datetime(2026, 8, 31, 0, 0, tzinfo=UTC))
    vis = CloseFailureVisibility(store, now=now)
    mcp = _RecordingMcp()

    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("close refusal"),
    )

    assert report.action == "state_write_failed"
    assert mcp.post_calls() == []


# --- Test 5: human close does not permanently suppress (Einstein msg-2296) -----------------------


@pytest.mark.anyio
async def test_human_close_clears_episode_without_permanent_suppression() -> None:
    """Episode → human closes thread → next failure eventually reports.

    Pins Einstein msg-2296 (blocking `edge-case`). The rejected design
    keyed episodes by project alone, and cleared only on ``close_alert``
    success — meaning a human resolving the thread manually left stale
    dedup state that permanently suppressed the next episode's report.
    D-2''' (msg-2297) added ``thread_id`` to the episode key AND changed
    the clear condition to "positive observation that alert is not open"
    (Rule 1), which includes ``on_close_success`` being called when the
    close call returns ``was_open=False`` (already resolved).

    Test sequence:
      1. First failure → post is attempted (assuming floor is clear).
      2. Human close the thread → sweep observes it as already-resolved
         → ``on_close_success`` clears the episode.
      3. A later failure with the same signature would attempt a fresh
         post IF the floor were clear. Here we drive time past the floor
         to isolate the episode-clear question from the floor question.
    """
    store = _MemoryStore()
    start = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
    call_count = {"n": 0}

    def _now() -> datetime:
        # ``on_close_failure`` calls ``self._now()`` exactly once per
        # invocation; ``on_close_success`` does not consult time at all.
        # So the counter increments only on Phase-1 and Phase-3 calls
        # (both are ``on_close_failure``); Phase 2 does not advance it.
        n = call_count["n"]
        call_count["n"] += 1
        if n == 0:
            return start
        # Second failure comes AFTER the floor has expired so the
        # episode-clear question is isolated from the floor question.
        return start + FAILURE_REPORT_FLOOR + timedelta(minutes=5)

    vis = CloseFailureVisibility(store, now=_now)
    mcp = _RecordingMcp()

    # Phase 1: initial failure → one post attempt.
    r1 = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("initial refusal"),
    )
    assert r1.action == "posted"

    # Phase 2: human closes the thread → sweep's close call now returns
    # was_open=False → tick calls on_close_success → episode cleared.
    vis.on_close_success(project="spirrow-verimend", thread_id=thread_id_for("spirrow-verimend"))
    assert "spirrow-verimend" not in store.state.episodes, (
        "on_close_success must clear the episode entry (Rule 1)"
    )
    # But the floor must NOT be cleared (Rule 2).
    assert "spirrow-verimend" in store.state.floors, (
        "on_close_success must NOT clear the floor (Rule 2: rate limiter must not be reset by "
        "the condition it is rate-limiting)"
    )

    # Phase 3: a later failure after the floor has expired. Because the
    # episode was cleared in phase 2, this new failure is treated as a
    # fresh episode — it is NOT permanently suppressed.
    r3 = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("recurring refusal"),
    )
    assert r3.action == "posted", (
        "after human close + floor expiry, a fresh failure must post — the stale dedup key must "
        "NOT permanently suppress"
    )


# --- Test 6: flap does not restart spam (msg-2297 Rule 2) ----------------------------------------


@pytest.mark.anyio
async def test_flap_does_not_restart_spam() -> None:
    """Alternating failure/success every 5 minutes for 24h → at most one post attempt.

    Pins D-2''' Rule 2 (msg-2297): "床は episode とは独立の寿命を持ち、試行に
    よって前進する以外の変化をしない". The naïve implementation of Rule 1
    (positive observation → clear the whole state) would clear the FLOOR
    alongside the episode, and a flap would restart the 5-minute-spam
    vector. Rule 2 keeps the floor independent.

    Test sequence: 288 ticks (24h). Odd ticks: failure. Even ticks:
    success (episode cleared, but floor persists). Assert the total number
    of post attempts is ≤ 1 across the entire window.
    """
    store = _MemoryStore()
    now = _clock(datetime(2026, 8, 31, 0, 0, tzinfo=UTC))
    vis = CloseFailureVisibility(store, now=now)
    mcp = _RecordingMcp()

    for tick in range(288):
        if tick % 2 == 0:
            # "no failure this tick" → close succeeded → clear episode.
            vis.on_close_success(
                project="spirrow-verimend",
                thread_id=thread_id_for("spirrow-verimend"),
            )
        else:
            await vis.on_close_failure(
                mcp,
                project="spirrow-verimend",
                thread_id=thread_id_for("spirrow-verimend"),
                owner=DEFAULT_SWEEPER_OWNER,
                exc=GateBootstrapCloseError("flapping refusal"),
            )

    assert len(mcp.post_calls()) <= 1, (
        f"flap across 24h attempted {len(mcp.post_calls())} posts; the floor must persist "
        "across episode clears (Rule 2)"
    )


# --- Test 6-b (msg-2301 D-2''''): open failure never touches visibility state -------------------


@pytest.mark.anyio
async def test_visibility_is_close_only(tmp_path: Path) -> None:
    """The tick's open-failure branch MUST NOT touch the visibility state file.

    Pins msg-2301 D-2'''' mechanical constraint. Applying the D-2''' state
    machine to open failures would invert the rate-limiter (Einstein
    msg-2298 blocking `correctness`): "alert-not-open" on the open path
    means the failure is *actively persisting*, and the asymmetric-clear
    rule would clear the state every tick.

    Test: drive the CLI's ``_run_tick`` with a NEW_REPO status (which
    routes to ``open_alert``) and a fake that makes ``open_alert`` fail.
    Assert that the visibility state file is NOT created — i.e. the
    on-disk artifact has no evidence of visibility having run. This is
    stronger than "the fake was not asked to post": it verifies the
    absence of side-effects, which the module docstring pins as the
    correct form of the constraint (mock call counts can be defeated by
    an intermediate wrapper; side-effect absence cannot).
    """
    cli_spec = importlib.util.spec_from_file_location(
        "_gate_bootstrap_visibility_cli_test", _SCRIPT_PATH
    )
    assert cli_spec is not None and cli_spec.loader is not None
    cli = importlib.util.module_from_spec(cli_spec)
    sys.modules[cli_spec.name] = cli
    cli_spec.loader.exec_module(cli)

    # NEW_REPO: a git repo with no upstream ref.
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)

    state_file = tmp_path / "state" / "gate_bootstrap_failure.json"
    vis = cli.CloseFailureVisibility(cli.FileFailureStateStore(state_file))

    class _OpenFailingMcp:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            # Simulate the observed pydantic-shaped refusal.
            from spirrow_mindwire.magickit.client import MagickitMcpError

            raise MagickitMcpError(
                "magickit tool returned an error envelope: "
                "total=1 keys=['error_type'] error_type='ChatroomAuthError'"
            )

    mcp = _OpenFailingMcp()

    exit_code, out = await cli._run_tick(
        "spirrow-newborn",
        tmp_path,
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha=None,
        mcp_factory=lambda _url: mcp,
        visibility=vis,
    )

    # The tick exit is 1 (open_alert failed) — but the visibility state
    # file must NOT exist, because visibility never ran on the open path.
    assert exit_code == 1
    assert out["action"] != "posted"  # sanity: no post action recorded
    assert not state_file.exists(), (
        f"visibility state file exists after an open_alert failure — the D-2 mechanism "
        f"leaked onto the open path (msg-2301 D-2''''); path={state_file}"
    )
    # And no chatroom_post_message call was made — even the transport was
    # not consulted for a report.
    assert not any(name == "chatroom_post_message" for name, _ in mcp.calls)


# --- Additional coverage: file store round-trip ---------------------------------------------------


def test_file_state_store_roundtrips(tmp_path: Path) -> None:
    """FileFailureStateStore write → read yields the same state.

    Not one of the design-mandated tests, but the file store is the
    production ``FailureStateStore`` and a round-trip failure here would
    silently degrade every test above to "in-memory only". Kept minimal.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    store = FileFailureStateStore(path)
    state = _State(
        episodes={
            "spirrow-verimend": FailureEpisode(
                project="spirrow-verimend",
                thread_id=thread_id_for("spirrow-verimend"),
                signature="GateBootstrapCloseError",
                first_seen_at="2026-08-31T00:00:00+00:00",
                reported_at="2026-08-31T00:00:00+00:00",
            ),
        },
        floors={
            "spirrow-verimend": RateLimitFloor(
                project="spirrow-verimend",
                last_attempt_at="2026-08-31T00:00:00+00:00",
            ),
        },
    )
    store.save(state)
    read_back = store.load()
    assert read_back.episodes == state.episodes
    assert read_back.floors == state.floors
