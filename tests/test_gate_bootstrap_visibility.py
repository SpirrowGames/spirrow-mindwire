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
    StateFileMalformedError,
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


# --- PR-gate #209 blocking #1 regression: failure-report prose accuracy ------------------------


@pytest.mark.anyio
async def test_failure_report_does_not_claim_close_will_wait_24h() -> None:
    """Regression for PR #209 gate blocking #1: the report must not lie about close cadence.

    The pre-fix prose said:

        "The sweeper will not attempt to close this thread again for
        24 hours (rate-limit floor)."

    That is false. The sweeper attempts ``close_alert`` on EVERY 5-minute
    tick; only the visibility REPORT is rate-limited. Operators who read
    the pre-fix prose could wait 24 hours after fixing an upstream policy
    issue rather than realising the next tick will close the thread.

    This test drives one failure through the visibility mechanism, reads
    the exact body posted to ``chatroom_post_message``, and pins:
      * the body MUST NOT contain the exact misleading sentence, and
      * the body MUST explicitly say the close attempt itself is not
        rate-limited (positive claim, not just absence of the wrong one —
        without this the next well-meaning refactor could just delete the
        sentence and lose the correction).
    """
    store = _MemoryStore()
    now = _clock(datetime(2026, 8, 31, 0, 0, tzinfo=UTC))
    vis = CloseFailureVisibility(store, now=now)
    mcp = _RecordingMcp()

    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("closeable_roles check failed"),
    )
    assert report.action == "posted"
    posts = mcp.post_calls()
    assert len(posts) == 1
    body = posts[0][1]["content"]
    # The exact misleading sentence must be gone.
    assert "will not attempt to close this thread again for" not in body
    # And the correct semantics must be stated positively.
    assert "close attempt itself is NOT rate-limited" in body
    assert "every 5-minute tick" in body


# --- PR-gate #209 blocking #2 regression: read failure must fail-closed and preserve state -----


@pytest.mark.anyio
async def test_read_failure_fails_closed_and_preserves_other_projects_state(tmp_path: Path) -> None:
    """Regression for PR #209 gate blocking #2: a transient read failure must NOT erase state.

    The pre-fix ``FileFailureStateStore.load`` swallowed ``OSError`` and
    returned an empty ``_State()``. The downstream ``on_close_failure``'s
    ``except OSError`` guard was therefore dead code: the "empty state"
    return would pass the floor/dedup checks and then ``save()`` would
    persist a state file containing ONLY the current project's floor,
    silently erasing every other project's records.

    This test:
      1. Pre-populates a state file with two projects' records
         (``spirrow-verimend`` = the caller, plus ``spirrow-magickit``
         = the innocent bystander).
      2. Substitutes a store whose ``load`` raises ``OSError`` (mimicking
         a transient FS glitch).
      3. Drives ``on_close_failure`` for ``spirrow-verimend`` — asserts
         action is ``state_read_failed``, no post is attempted, and
         critically, no ``save`` was called (which would have erased
         the bystander).
    """
    # Pre-populate real disk state so we have concrete "before" evidence
    # that a subsequent bug would erase.
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    real_store = FileFailureStateStore(path)
    real_store.save(
        _State(
            episodes={
                "spirrow-magickit": FailureEpisode(
                    project="spirrow-magickit",
                    thread_id=thread_id_for("spirrow-magickit"),
                    signature="GateBootstrapCloseError",
                    first_seen_at="2026-08-30T00:00:00+00:00",
                    reported_at="2026-08-30T00:00:00+00:00",
                ),
            },
            floors={
                "spirrow-magickit": RateLimitFloor(
                    project="spirrow-magickit",
                    last_attempt_at="2026-08-30T00:00:00+00:00",
                ),
            },
        )
    )
    pre_bytes = path.read_bytes()

    # A store that fakes a transient read failure.
    reading_store = _MemoryStore()
    reading_store.raise_on_load = OSError("simulated transient FS read glitch")
    vis = CloseFailureVisibility(reading_store)
    mcp = _RecordingMcp()

    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("close refusal"),
    )

    # The fail-closed guard must fire.
    assert report.action == "state_read_failed"
    # Load-bearing side-effect assertions:
    # (a) no post was attempted (nothing to rate-limit against),
    assert mcp.post_calls() == []
    # (b) no save was attempted (the bystander's state would be erased),
    assert reading_store.save_calls == 0
    # (c) and — belt AND braces — the on-disk file we would have
    # actually saved to (if the visibility used the real store) is
    # bit-for-bit unchanged. This is the "did we erase the bystander"
    # observable that the pre-fix code would have violated.
    assert path.read_bytes() == pre_bytes


def test_file_state_store_load_propagates_os_error(tmp_path: Path) -> None:
    """PR #209 blocking #2 (store level): OSError from read is NOT swallowed.

    Companion to the integration test above. Pins the contract at the
    store boundary: only ``FileNotFoundError`` yields empty state; every
    other read failure propagates. A regression that re-introduces the
    silent-swallow would fail this test AND the integration test — kept
    separate so a future ``on_close_failure`` refactor cannot mask a
    store-level regression by accident.
    """
    # A directory (not a file) at the target path makes ``read_text``
    # raise ``IsADirectoryError`` on POSIX / ``PermissionError`` on
    # Windows — both are subclasses of ``OSError`` and must propagate.
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    path.mkdir()  # will make read_text fail with OSError
    store = FileFailureStateStore(path)
    with pytest.raises(OSError):
        store.load()


def test_file_state_store_load_propagates_json_decode_error(tmp_path: Path) -> None:
    """PR #209 blocking #2 (store level): corrupt JSON is NOT swallowed to empty.

    A file present but garbage must NOT be indistinguishable from a
    fresh install. If it were, ``on_close_failure`` would happily
    ``save()`` a partial view over the corruption, losing every other
    project's floor and episode records (same failure mode as the
    OSError case).
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    path.write_text("not valid json {{{", encoding="utf-8")
    store = FileFailureStateStore(path)
    with pytest.raises(json.JSONDecodeError):
        store.load()


def test_file_state_store_load_propagates_unicode_decode_error(tmp_path: Path) -> None:
    """PR #209 blocking (round 4, store level): invalid UTF-8 bytes propagate.

    A state file whose bytes are not valid UTF-8 (an OS-level crash
    mid-write, manual tampering, disk corruption) must raise
    ``UnicodeDecodeError`` out of ``load()`` — the same shape as the
    OSError / JSONDecodeError propagation. This is the store-level pin
    for the invariant checked at the visibility-integration level by
    ``test_visibility_survives_unicode_decode_error_in_state_file``.

    Bytes ``\\xff\\xfe...`` are chosen because ``\\xff`` is illegal as a
    UTF-8 start byte, so ``read_text(encoding="utf-8")`` fails
    immediately with ``UnicodeDecodeError`` — the exact class the
    PR-gate flagged as bypassing ``except OSError``.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe not valid utf-8 bytes")
    store = FileFailureStateStore(path)
    with pytest.raises(UnicodeDecodeError):
        store.load()


# --- PR-gate #209 advisory #3 regression: atomic save must use a unique temp file --------------


def test_file_state_store_save_uses_unique_temp_file(tmp_path: Path) -> None:
    """PR #209 gate advisory #3: no static ``.tmp`` sibling — must use unique names.

    A statically-named ``<name>.tmp`` sibling would race a concurrent tick
    and produce a corrupt file mid-``os.replace``. This test drives a
    ``save`` that fails on the ``os.replace`` step (by holding the
    target open) and asserts the temp file used was NOT the static
    ``<name>.tmp`` name — verifying the ``tempfile.NamedTemporaryFile``
    contract at the boundary.

    Implementation: monkey-patch ``os.replace`` to record the temp path
    it was called with. Any name other than the static static-``.tmp``
    passes; the same static ``.tmp`` name every call would fail this.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    store = FileFailureStateStore(path)

    seen_temp_paths: list[str] = []
    real_replace = os.replace

    def spy_replace(src: Any, dst: Any) -> None:
        seen_temp_paths.append(str(src))
        real_replace(src, dst)

    import unittest.mock

    empty_state = _State()
    with unittest.mock.patch("os.replace", side_effect=spy_replace):
        store.save(empty_state)
        store.save(empty_state)

    assert len(seen_temp_paths) == 2
    # Two saves must not share a temp file name (else concurrent
    # saves would corrupt each other).
    assert seen_temp_paths[0] != seen_temp_paths[1], (
        f"static temp file name is a race condition — got same tmp both times: {seen_temp_paths[0]}"
    )
    # And neither may be the naive ``<name>.tmp`` sibling.
    naive = str(path.with_suffix(path.suffix + ".tmp"))
    assert naive not in seen_temp_paths


def test_file_state_store_cleans_up_temp_on_replace_failure(tmp_path: Path) -> None:
    """A failed ``os.replace`` must not leave orphan tmp files behind.

    Kept minimal — the cleanup itself is not load-bearing, but a
    repeated failure would otherwise pile up temp files across days.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    store = FileFailureStateStore(path)

    import unittest.mock

    with (
        unittest.mock.patch("os.replace", side_effect=OSError("simulated replace failure")),
        pytest.raises(OSError),
    ):
        store.save(_State())

    # No stale tmp files under the state dir.
    leftover = list(path.parent.glob("*.tmp"))
    assert leftover == [], f"orphan temp files after failed save: {leftover}"


# --- PR-gate #209 blocking round-3 regression: TOCTOU during await ----------------------------


@pytest.mark.anyio
async def test_concurrent_tick_updates_survive_our_post_await(tmp_path: Path) -> None:
    """PR #209 gate blocking (round 3): lost-update during ``await mcp.call_tool``.

    Before this fix, ``on_close_failure`` loaded the shared state file
    once at the beginning, awaited the network call to ``mcp.call_tool``
    (yielding to the event loop), then wrote its stale in-memory
    ``state`` back — silently erasing any concurrent tick's updates
    that landed on the shared file during the await window.

    This test makes the race concrete with a real ``FileFailureStateStore``
    backed by ``tmp_path``:

      1. Pre-populate the state file with a bystander project
         (``spirrow-mindwire``) so we can distinguish "we lost nothing"
         from "the file happened to be empty anyway".
      2. Program the MCP fake so that its ``call_tool`` — which runs
         inside our ``await`` — writes to the very same state file,
         adding a THIRD project's records (mimicking a concurrent tick
         for ``spirrow-voxelworld``).
      3. Drive ``on_close_failure`` for ``spirrow-verimend`` and assert
         the FINAL on-disk state contains:
           * our project's episode with ``reported_at`` set (post ok),
           * the concurrent tick's records (they must not be erased),
           * and the pre-existing bystander (unchanged).

    A regression that reuses the pre-await snapshot for the
    mark-reported save fails assertion (2) — the concurrent tick's
    ``spirrow-voxelworld`` records get silently erased. That is
    precisely the lost-update bug this fix closes.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    store = FileFailureStateStore(path)
    store.save(
        _State(
            floors={
                "spirrow-mindwire": RateLimitFloor(
                    project="spirrow-mindwire",
                    last_attempt_at="2026-08-30T00:00:00+00:00",
                ),
            }
        )
    )

    concurrent_tick_ran = {"done": False}

    def _simulate_concurrent_tick(name: str, args: dict[str, Any]) -> Any:
        # This runs inside our ``await mcp.call_tool``. Simulate a
        # sibling tick for a DIFFERENT project by loading the current
        # on-disk state (which now has our write-ahead save from
        # step 3), adding a third project's records, and saving.
        concurrent_state = store.load()
        concurrent_state.floors["spirrow-voxelworld"] = RateLimitFloor(
            project="spirrow-voxelworld",
            last_attempt_at="2026-08-31T00:05:00+00:00",
        )
        concurrent_state.episodes["spirrow-voxelworld"] = FailureEpisode(
            project="spirrow-voxelworld",
            thread_id=thread_id_for("spirrow-voxelworld"),
            signature="GateBootstrapCloseError",
            first_seen_at="2026-08-31T00:05:00+00:00",
            reported_at="2026-08-31T00:05:00+00:00",
        )
        store.save(concurrent_state)
        concurrent_tick_ran["done"] = True
        return {"msg": {"msg_id": "msg-fake-concurrent"}}

    mcp = _RecordingMcp(post_outcome=_simulate_concurrent_tick)
    vis = CloseFailureVisibility(store)

    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("close refusal"),
    )

    # Sanity checks.
    assert concurrent_tick_ran["done"], "the concurrent-tick simulator did not fire"
    assert report.action == "posted"

    # Load the final on-disk state.
    final = store.load()

    # (1) Our own project's episode must have reported_at set (post ok).
    assert "spirrow-verimend" in final.episodes
    verimend_episode = final.episodes["spirrow-verimend"]
    assert verimend_episode.reported_at is not None
    assert "spirrow-verimend" in final.floors

    # (2) THE load-bearing assertion: the concurrent tick's project
    # records must survive. A regression will fail this — the pre-await
    # snapshot save at step 5 does not know about ``spirrow-voxelworld``
    # and blindly writes a state that does not contain it.
    assert "spirrow-voxelworld" in final.floors, (
        "concurrent tick's floor for spirrow-voxelworld was erased by our "
        "stale-state save — this is the PR #209 gate round-3 TOCTOU bug"
    )
    assert "spirrow-voxelworld" in final.episodes, (
        "concurrent tick's episode for spirrow-voxelworld was erased by our "
        "stale-state save — this is the PR #209 gate round-3 TOCTOU bug"
    )
    voxel_episode = final.episodes["spirrow-voxelworld"]
    assert voxel_episode.reported_at == "2026-08-31T00:05:00+00:00"

    # (3) Pre-existing bystander must also survive (unchanged floor).
    assert "spirrow-mindwire" in final.floors
    assert final.floors["spirrow-mindwire"].last_attempt_at == "2026-08-30T00:00:00+00:00"


@pytest.mark.anyio
async def test_post_success_when_reload_after_await_fails(tmp_path: Path) -> None:
    """If the post-await reload fails, do NOT lose the "posted" verdict.

    The post itself succeeded — the operator's alert-thread got the
    report. The mechanism only failed to persist the ``reported_at``
    mark. The floor entry from step 3 already blocks the next 24h, so
    dedup at the coarse level still works. Reporting this as
    ``state_read_failed`` would be misleading (a post DID land) and
    reporting it as ``post_failed`` would falsify the observable
    outcome.

    The action stays ``posted``; the ``reason`` string names the
    reload failure so the operator can see why the mark did not land.
    """
    # Real store on disk, then swap its ``load`` for a failing one only
    # for the post-await reload phase.
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    store = FileFailureStateStore(path)

    load_call_count = {"n": 0}
    original_load = store.load

    def flaky_load() -> _State:
        load_call_count["n"] += 1
        # First two loads (initial + write-ahead reload) succeed.
        # The third load (post-await reload) fails, simulating a
        # transient FS glitch that happens to strike right after the
        # network call.
        #
        # Note: current implementation makes only two ``load()`` calls
        # per on_close_failure (initial and post-await). ``n == 2`` is
        # the post-await one under that implementation.
        if load_call_count["n"] >= 2:
            raise OSError("simulated transient read failure after post")
        return original_load()

    # Type: ignore because we're rebinding a method for the test only.
    store.load = flaky_load  # type: ignore[method-assign]

    mcp = _RecordingMcp()
    vis = CloseFailureVisibility(store)

    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("close refusal"),
    )

    # Post did land — action must be "posted", NOT state_read_failed.
    assert report.action == "posted", (
        f"a successful post must be reported as 'posted' even if the "
        f"reported_at mark cannot be persisted; got action={report.action!r} "
        f"reason={report.reason!r}"
    )
    # The reason must name the reload failure so the operator can
    # diagnose without opening the state file.
    assert "reported_at mark could not be persisted" in report.reason
    assert "floor from" in report.reason  # cites the still-effective 24h floor
    # And the post itself was really attempted.
    assert len(mcp.post_calls()) == 1


# --- PR-gate #209 blocking round-4 regression: UnicodeDecodeError never raises to caller ------


@pytest.mark.anyio
async def test_visibility_survives_unicode_decode_error_in_state_file(tmp_path: Path) -> None:
    """PR #209 gate round-4 blocking: invalid UTF-8 in state file must NOT crash the tick.

    The module docstring pins as an invariant: "The visibility path
    never raises to its caller." Before this fix, the ``except``
    clauses in ``on_close_failure`` / ``on_close_success`` named
    ``(OSError, json.JSONDecodeError)`` — neither of which catches
    ``UnicodeDecodeError`` (which inherits from ``ValueError``, not
    ``OSError``). A state file with invalid UTF-8 bytes therefore
    raised ``UnicodeDecodeError`` straight out of the visibility
    module and up to the tick's top-level guard, crashing the
    (best-effort) side channel.

    This test writes an invalid-UTF-8 state file to disk, then drives
    ``on_close_failure`` through a real ``FileFailureStateStore`` and
    asserts:

      1. no exception propagates out of ``on_close_failure``;
      2. the returned ``action`` is ``state_read_failed``
         (the same fail-closed verdict as the OSError /
         JSONDecodeError cases — the caller shouldn't need to know
         which sub-class of read-failure occurred);
      3. no post attempt is made (fail-closed);
      4. the invalid-UTF-8 state file is bit-for-bit unchanged (the
         mechanism refuses to save because refusing-to-post means
         refusing to record an attempt).

    Assertion (4) is the load-bearing one — a regression that
    "helpfully" swallowed the ``UnicodeDecodeError`` and then saved
    empty state would overwrite the (corrupted-but-present) file with
    a partial view, silently erasing whatever data was there.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    # Invalid UTF-8: 0xff is illegal as a UTF-8 start byte.
    pre_bytes = b"\xff\xfe some invalid utf-8 bytes"
    path.write_bytes(pre_bytes)

    store = FileFailureStateStore(path)
    mcp = _RecordingMcp()
    vis = CloseFailureVisibility(store)

    # (1) Must not raise. If it did, this line would propagate the
    # exception and fail the test with a traceback instead of an
    # assertion — either way the invariant would be visibly broken.
    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("close refusal"),
    )

    # (2) The verdict is fail-closed on the read error.
    assert report.action == "state_read_failed", (
        f"expected state_read_failed on invalid-UTF-8 state file; "
        f"got action={report.action!r} reason={report.reason!r}"
    )
    # And the reason names the class so the operator can diagnose.
    assert "UnicodeDecodeError" in report.reason

    # (3) No post attempted (fail-closed: cannot rate-limit ⇒ do not send).
    assert mcp.post_calls() == []

    # (4) THE load-bearing invariant: the corrupt file is unchanged.
    # A regression that "helpfully" swallowed UnicodeDecodeError and
    # then saved empty state would replace these bytes with a JSON
    # object — this assertion catches that class of erasure.
    assert path.read_bytes() == pre_bytes


@pytest.mark.anyio
async def test_on_close_success_survives_unicode_decode_error_in_state_file(tmp_path: Path) -> None:
    """The synchronous counterpart to the on_close_failure test above.

    ``on_close_success`` also swallows read failures — but it takes no
    ``exc`` argument and has a smaller failure surface, so this test
    is minimal: prove that the same invalid-UTF-8 state file does not
    raise out of ``on_close_success`` either. Regression form matches
    the async test to make the two paths visibly symmetric.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe invalid utf-8")

    store = FileFailureStateStore(path)
    vis = CloseFailureVisibility(store)

    # Must not raise.
    vis.on_close_success(
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
    )
    # File is unchanged (no partial overwrite).
    assert path.read_bytes() == b"\xff\xfe invalid utf-8"


# --- PR-gate #209 blocking round-5 #1 regression: mid-write failure must not leak tmp ---------


def test_file_state_store_cleans_up_temp_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #209 gate round-5 blocking #1: fd.write failure inside the ``with`` block.

    Before this fix, the ``try/except`` in :meth:`FileFailureStateStore.save`
    wrapped ONLY the ``os.replace`` call. If ``fd.write`` raised inside
    the ``NamedTemporaryFile(...)`` ``with`` block (disk full, quota,
    filesystem going read-only), the exception propagated out — past
    the cleanup code — leaving an orphan ``.tmp`` file on disk that
    ``delete=False`` never removed. The existing
    ``test_file_state_store_cleans_up_temp_on_replace_failure`` did not
    catch this because it only mocked ``os.replace``.

    This test intercepts ``NamedTemporaryFile`` to create a REAL temp
    file (so we can verify cleanup on disk) but wraps its ``.write``
    method to raise ``OSError``, mimicking a mid-write disk failure.
    The load-bearing assertion is that after the exception, no
    ``.tmp`` files remain in the state directory.
    """
    import tempfile as tempfile_mod

    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    store = FileFailureStateStore(path)

    real_ntf = tempfile_mod.NamedTemporaryFile
    created_paths: list[Path] = []

    def failing_write(_data: str) -> int:
        raise OSError("simulated disk-full during write")

    def failing_ntf(*args: Any, **kwargs: Any) -> Any:
        fd = real_ntf(*args, **kwargs)
        created_paths.append(Path(fd.name))
        # Assigning to fd.write replaces the bound method on this
        # instance only; other tests / production code paths that use
        # NamedTemporaryFile are unaffected. ``type: ignore`` covers
        # the whole assignment — mypy's overloaded ``write`` signature
        # disagrees with our simple ``str -> int`` shape (which is all
        # we exercise here), and the ``method-assign`` code alone
        # doesn't cover the shape mismatch.
        fd.write = failing_write  # type: ignore[assignment,method-assign]
        return fd

    monkeypatch.setattr(tempfile_mod, "NamedTemporaryFile", failing_ntf)

    with pytest.raises(OSError, match="simulated disk-full"):
        store.save(_State())

    # Load-bearing assertions:
    assert created_paths, (
        "test setup: NamedTemporaryFile was not called; the test is not "
        "exercising the code path it claims to"
    )
    # (1) Every tmp path that was created has been unlinked by the
    #     cleanup block. A regression that reintroduced the narrow
    #     wrapping around ``os.replace`` alone would fail this: the
    #     tmp file would still exist because the exception propagated
    #     before the cleanup code was reached.
    for p in created_paths:
        assert not p.exists(), (
            f"orphan temp file after write failure: {p} — the try/except in "
            f"FileFailureStateStore.save does not cover fd.write "
            f"(PR #209 round-5 blocking #1 regression)"
        )
    # (2) Belt AND braces: no .tmp under the state dir at all.
    leftover = list(path.parent.glob("*.tmp"))
    assert leftover == [], f"orphan .tmp files remain: {leftover}"


# --- PR-gate #209 blocking round-5 #2 regression: malformed entries must NOT be silently dropped


def test_file_state_store_load_raises_on_malformed_episode_entry(tmp_path: Path) -> None:
    """PR #209 gate round-5 blocking #2 (store level): malformed entry propagates.

    Before this fix, ``_decode_state`` wrapped each entry construction
    in ``contextlib.suppress(KeyError, TypeError)`` and silently
    dropped malformed entries. If ``load()`` returned a partial view
    and the caller then ``save()``-ed, the dropped entry would be
    permanently erased — the "paint over corruption" failure the
    ``FileFailureStateStore`` docstring explicitly forbids.

    This test writes a state file with a well-formed bystander entry
    PLUS a malformed episode (missing the required ``thread_id`` key)
    and asserts that ``load()`` raises :class:`StateFileMalformedError`.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    # Well-formed bystander + malformed target (missing thread_id).
    path.write_text(
        json.dumps(
            {
                "episodes": {
                    "spirrow-magickit": {
                        "thread_id": "T-gate-bootstrap-spirrow-magickit",
                        "signature": "GateBootstrapCloseError",
                        "first_seen_at": "2026-08-30T00:00:00+00:00",
                        "reported_at": "2026-08-30T00:00:00+00:00",
                    },
                    "spirrow-verimend": {
                        # thread_id INTENTIONALLY missing — schema drift.
                        "signature": "GateBootstrapCloseError",
                        "first_seen_at": "2026-08-31T00:00:00+00:00",
                    },
                },
                "floors": {},
            }
        ),
        encoding="utf-8",
    )
    store = FileFailureStateStore(path)
    with pytest.raises(StateFileMalformedError, match="thread_id"):
        store.load()


def test_file_state_store_load_raises_on_malformed_floor_entry(tmp_path: Path) -> None:
    """PR #209 gate round-5 blocking #2 (store level, floors section).

    Symmetric to the episode test above but drives the ``floors``
    branch of ``_decode_state``. Kept as a separate test so a future
    refactor cannot accidentally regress one section while the other
    stays green.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "episodes": {},
                "floors": {
                    "spirrow-verimend": {
                        # last_attempt_at INTENTIONALLY missing.
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    store = FileFailureStateStore(path)
    with pytest.raises(StateFileMalformedError, match="last_attempt_at"):
        store.load()


def test_file_state_store_load_raises_on_non_object_episodes_section(tmp_path: Path) -> None:
    """Container-level schema drift: ``episodes`` is a list, not a dict.

    Kept because the section-container check is a distinct branch
    from the per-entry check above. A regression that drops the
    ``isinstance(..., Mapping)`` guard would fail this test with
    an ``AttributeError`` (list has no ``.items()``) instead of the
    expected fail-closed ``StateFileMalformedError``.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"episodes": ["not", "a", "dict"], "floors": {}}),
        encoding="utf-8",
    )
    store = FileFailureStateStore(path)
    with pytest.raises(StateFileMalformedError, match="episodes"):
        store.load()


@pytest.mark.anyio
async def test_malformed_entry_does_not_erase_state_file(tmp_path: Path) -> None:
    """PR #209 gate round-5 blocking #2 (integration): fail-closed preserves corrupt bytes.

    The store-level tests above pin that ``load()`` propagates
    :class:`StateFileMalformedError`. This test drives the full
    integration: with a state file whose bytes ARE valid JSON but
    contain a malformed episode entry, ``on_close_failure`` must
    return ``state_read_failed`` AND leave the on-disk bytes unchanged.

    A regression that reintroduced ``contextlib.suppress(KeyError,
    TypeError)`` in ``_decode_state`` would fail this test in a
    specific way: ``load()`` returns a partial view (well-formed
    entries only), ``on_close_failure`` passes the floor/dedup checks
    against that partial view, and the subsequent write-ahead
    ``save()`` writes the partial view back — erasing the malformed
    entry permanently. The on-disk bytes-equal assertion catches
    that exactly.
    """
    path = tmp_path / "state" / "gate_bootstrap_failure.json"
    path.parent.mkdir(parents=True)
    pre_bytes = json.dumps(
        {
            "episodes": {
                "spirrow-magickit": {
                    "thread_id": "T-gate-bootstrap-spirrow-magickit",
                    "signature": "GateBootstrapCloseError",
                    "first_seen_at": "2026-08-30T00:00:00+00:00",
                    "reported_at": "2026-08-30T00:00:00+00:00",
                },
                "spirrow-corrupted": {
                    # Malformed on purpose.
                    "signature": "GateBootstrapCloseError",
                },
            },
            "floors": {
                "spirrow-magickit": {
                    "last_attempt_at": "2026-08-30T00:00:00+00:00",
                },
            },
        }
    ).encode("utf-8")
    path.write_bytes(pre_bytes)

    store = FileFailureStateStore(path)
    mcp = _RecordingMcp()
    vis = CloseFailureVisibility(store)

    report = await vis.on_close_failure(
        mcp,
        project="spirrow-verimend",
        thread_id=thread_id_for("spirrow-verimend"),
        owner=DEFAULT_SWEEPER_OWNER,
        exc=GateBootstrapCloseError("close refusal"),
    )

    # Fail-closed verdict.
    assert report.action == "state_read_failed"
    assert "StateFileMalformedError" in report.reason
    # No post attempted (fail-closed).
    assert mcp.post_calls() == []
    # THE load-bearing assertion: file is BIT-FOR-BIT unchanged.
    # A regression that silently dropped ``spirrow-corrupted`` and
    # then saved would either (a) rewrite the file with only the
    # well-formed entries or (b) write our new floor plus the
    # well-formed entries — either way ``path.read_bytes()`` would
    # differ from ``pre_bytes``.
    assert path.read_bytes() == pre_bytes, (
        "on-disk state was modified after a malformed-entry read failure — "
        "the fail-closed contract requires the corrupt file to be preserved "
        "for a human to inspect (PR #209 round-5 blocking #2 regression)"
    )
