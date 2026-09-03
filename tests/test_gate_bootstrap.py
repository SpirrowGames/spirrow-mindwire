"""Tests for :mod:`spirrow_mindwire.gate_bootstrap` and its CLI wrapper.

Covers the four branches of :func:`inspect_gate` on synthetic ``repo_dir``s
(built with real ``git init`` where a git working tree is needed), and the
open / close paths through a :class:`_FakeMcp` that mimics the client-boundary
contract used by :mod:`spirrow_mindwire.orchestrator`.

The design source is chatroom thread ``T-new-project-gate-bootstrap``
(msg-1962 request; msg-1963/1965/1967 design; msg-1964/1966/1968 naysayer).
Each test's docstring names the specific decision it pins.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.gate_bootstrap import (
    ALERT_TAGS,
    DEFAULT_SWEEPER_OWNER,
    THREAD_ID_PREFIX,
    CloseResult,
    GateBootstrapCloseError,
    GateStatus,
    OpenResult,
    close_alert,
    inspect_gate,
    open_alert,
    should_alert,
    thread_id_for,
)
from spirrow_mindwire.magickit.client import MagickitMcpError, raise_if_envelope

# --- fakes ---------------------------------------------------------------------------------------


def _error_envelope(error_type: str, message: str, **details: Any) -> dict[str, Any]:
    """Same shape as :func:`tests.test_orchestrator._error_envelope`, kept local.

    A local copy is deliberate — tests here should not depend on the fixture
    module of another test module. The envelope contract is the same as the
    orchestrator tests' (msg-1685 §1 elevation).
    """
    return {"error_type": error_type, "error": message, "details": dict(details)}


class _FakeMcp:
    """Records every ``call_tool`` invocation; returns programmed results by tool name.

    Mimics the client boundary at
    :meth:`StreamableHttpChatroomMcp.call_tool` (parse_tool_result +
    raise_if_envelope), so a scripted envelope becomes a
    :class:`MagickitMcpError` before the caller sees it — the same
    convention the orchestrator tests use.
    """

    def __init__(
        self,
        results: dict[str, Any] | None = None,
    ) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        result = self._results.get(name, {})
        payload = result(arguments) if callable(result) else result
        raise_if_envelope(payload)
        return payload

    def args_for(self, name: str) -> dict[str, Any]:
        return next(args for n, args in self.calls if n == name)


# --- inspect_gate: the 4-branch predicate -------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> None:
    """Run a git command inside ``repo_dir`` for test setup only."""
    subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(repo_dir: Path) -> None:
    """Initialize a git repository with a deterministic identity for CI."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "tests@spirrow-mindwire.invalid")
    _git(repo_dir, "config", "user.name", "gate-bootstrap tests")
    _git(repo_dir, "config", "commit.gpgsign", "false")


def test_inspect_gate_declared_when_gate_in_worktree(tmp_path: Path) -> None:
    """Branch 1 (DECLARED): gate present in worktree short-circuits every other check.

    Pins msg-1967 D-1 line 1 — a worktree hit is the cheapest evidence and the
    predicate must exit on it without touching git at all.
    """
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    result = inspect_gate("spirrow-example", tmp_path)
    assert result.status == GateStatus.DECLARED
    assert result.upstream_ref is None
    assert not should_alert(result.status)


def test_inspect_gate_unusable_when_repo_dir_absent(tmp_path: Path) -> None:
    """Branch 2 (UNUSABLE): a missing ``repo_dir`` is fail-closed, no alert.

    Pins msg-1963 D-6 — a broken sweep entry is a sweep-config problem, not an
    onboarding one; guessing "alert" here would spend money in the direction
    the design explicitly forbids.
    """
    absent = tmp_path / "does-not-exist"
    result = inspect_gate("spirrow-example", absent)
    assert result.status == GateStatus.UNUSABLE
    assert not should_alert(result.status)


def test_inspect_gate_unusable_when_repo_dir_is_empty_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 0 (UNUSABLE): an empty ``repo_dir`` is fail-closed BEFORE the CWD probe.

    Pins the PR-gate fix for PR #192. ``argparse`` parses an empty ``--repo-dir``
    into ``Path("")`` which normalises to ``Path(".")`` — a RELATIVE path.
    Without the ``is_absolute()`` pre-branch, ``Path(".") / ".mindwire-gate"``
    would be the bare relative ``.mindwire-gate`` and ``.is_file()`` would
    silently resolve it against the current working directory. In production
    the sweep wrapper wraps the tick in ``Push-Location $repoRoot``, so the
    CWD is the MindWire host repo — which HAS a gate — and every candidate
    with a broken ``repo_dir`` would falsely report DECLARED and fire
    spurious ``close_alert`` traffic on every tick.

    This test reproduces that condition: it changes CWD into a directory that
    contains ``.mindwire-gate``, then calls ``inspect_gate`` with an empty
    ``repo_dir``. Without the fix, the result would be DECLARED. With the
    fix, it is UNUSABLE.
    """
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.chdir(tmp_path)
    result = inspect_gate("spirrow-example", Path(""))
    assert result.status == GateStatus.UNUSABLE
    assert not should_alert(result.status)
    # Reason must name the actual cause so the operator sees why in the log.
    assert "absolute" in result.reason.lower()


def test_inspect_gate_unusable_when_repo_dir_is_whitespace_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 0 (UNUSABLE): a whitespace-only ``repo_dir`` also fails closed.

    The PowerShell wrapper's ``[string]::IsNullOrWhiteSpace`` catches this
    upstream, but the Python side must ALSO defend — the PowerShell filter is
    defence-in-depth, and the Python side is the load-bearing rejection. A
    caller that passes ``Path(" ")`` (a whitespace-only string) bypasses
    PowerShell's ``-not`` truthy check (which only catches ``""`` / ``$null``)
    and would otherwise hit the same CWD-probe attack path as the empty
    string. ``Path(" ")`` on Windows normalises to a relative path, so the
    ``is_absolute()`` pre-branch catches it too.
    """
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.chdir(tmp_path)
    result = inspect_gate("spirrow-example", Path("   "))
    assert result.status == GateStatus.UNUSABLE
    assert not should_alert(result.status)


def test_inspect_gate_unusable_when_repo_dir_is_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 0 (UNUSABLE): a relative ``repo_dir`` (even one that exists) is refused.

    The sweep contract is absolute paths (``sweep.json.example``). A caller
    that passes a relative path — even ``some-repo`` when that directory
    exists under CWD — is either broken or hostile; the CWD-relative attack
    path is the same as the empty-string case. UNUSABLE is the correct
    verdict.
    """
    # Set up a plausible-looking relative repo in the CWD, to prove that
    # the check refuses it on principle rather than because the target is
    # broken.
    real_repo = tmp_path / "some-repo"
    _init_repo(real_repo)
    monkeypatch.chdir(tmp_path)
    result = inspect_gate("spirrow-example", Path("some-repo"))
    assert result.status == GateStatus.UNUSABLE
    assert not should_alert(result.status)


def test_inspect_gate_unusable_when_not_a_git_repo(tmp_path: Path) -> None:
    """Branch 2 (UNUSABLE): a directory that is not a git tree is UNUSABLE too.

    A plain directory tells the sweeper nothing about upstream state, and the
    design forbids inventing an alert in that state (msg-1963 D-6).
    """
    (tmp_path / "some-file").write_text("hi")
    result = inspect_gate("spirrow-example", tmp_path)
    assert result.status == GateStatus.UNUSABLE
    assert not should_alert(result.status)


def test_inspect_gate_new_repo_when_no_upstream_ref(tmp_path: Path) -> None:
    """Branch 3 (NEW_REPO): a git repo with no upstream ref fires the alert.

    Pins msg-1965 N-1 correction — the earlier design (msg-1963 D-6) fell
    closed here and would have silently excluded every brand-new repo. This
    is exactly the branch the alert exists for; it MUST fire.
    """
    _init_repo(tmp_path)
    result = inspect_gate("spirrow-example", tmp_path)
    assert result.status == GateStatus.NEW_REPO
    assert result.upstream_ref is None
    assert should_alert(result.status)


def test_inspect_gate_missing_when_ref_has_no_gate(tmp_path: Path) -> None:
    """Branch 4 (MISSING): worktree and upstream ref both lack the gate → alert.

    The classic "existing repo that never adopted the gate" case. The
    predicate resolves an upstream ref via the fallback list (msg-1965: origin/HEAD
    → origin/main → origin/develop) and checks the blob path there.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "README.md").write_text("hi\n")
    _git(origin, "add", "README.md")
    _git(origin, "commit", "-q", "-m", "init")
    # Make origin's initial branch consistent so `origin/main` exists.
    _git(origin, "branch", "-M", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "tests@spirrow-mindwire.invalid")
    _git(clone, "config", "user.name", "gate-bootstrap tests")

    result = inspect_gate("spirrow-example", clone)
    assert result.status == GateStatus.MISSING
    # Whichever fallback resolved is fine; the point is that SOME upstream ref did.
    assert result.upstream_ref in {"origin/HEAD", "origin/main", "origin/develop"}
    assert should_alert(result.status)


def test_inspect_gate_stale_worktree_when_ref_carries_gate(tmp_path: Path) -> None:
    """Branch 5 (STALE_WORKTREE): gate exists at upstream but not in worktree — no alert.

    The connective in msg-1963 D-1 that prevents a stale checkout from producing a
    false MISSING. The predicate looks at the origin/main tree even if HEAD is
    parked on a branch that never learned about the gate.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    _git(origin, "add", ".mindwire-gate")
    _git(origin, "commit", "-q", "-m", "declare gate")
    _git(origin, "branch", "-M", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "tests@spirrow-mindwire.invalid")
    _git(clone, "config", "user.name", "gate-bootstrap tests")
    # Move HEAD to a branch that has no gate: same behaviour as an old checkout.
    _git(clone, "checkout", "-q", "-b", "old-branch")
    (clone / ".mindwire-gate").unlink()
    _git(clone, "commit", "-q", "-am", "drop gate on old-branch")

    result = inspect_gate("spirrow-example", clone)
    assert result.status == GateStatus.STALE_WORKTREE
    assert result.upstream_ref is not None
    assert not should_alert(result.status)


def test_thread_id_for_uses_fixed_prefix() -> None:
    """The idempotency key is a deterministic function of the project id (msg-1963 D-4)."""
    assert thread_id_for("spirrow-verimend") == f"{THREAD_ID_PREFIX}spirrow-verimend"


# --- open_alert -----------------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    """The anyio marker uses asyncio only — the test-suite convention."""
    return "asyncio"


@pytest.mark.anyio
async def test_open_alert_opens_fixed_thread_id_with_alert_tags() -> None:
    """First open: the thread is created with the fixed id, owner, and tags.

    Pins msg-1967 D-2:
      * thread_id is ``T-gate-bootstrap-<project>`` (the idempotency key).
      * owner is the sweeper identity (DEFAULT_SWEEPER_OWNER).
      * tags include both ``system-alert`` (semantic — msg-1967 N-5-B) and
        ``gate-bootstrap`` (specific alert kind).
    """
    mcp = _FakeMcp(results={"chatroom_open_thread": {"ok": True}})
    result = await open_alert(mcp, project="spirrow-verimend")
    assert result == OpenResult(thread_id="T-gate-bootstrap-spirrow-verimend", already_exists=False)
    args = mcp.args_for("chatroom_open_thread")
    assert args["project"] == "spirrow-verimend"
    assert args["thread_id"] == "T-gate-bootstrap-spirrow-verimend"
    assert args["owner"] == DEFAULT_SWEEPER_OWNER
    assert set(args["tags"]) == set(ALERT_TAGS)
    # The propose_content self-identifies as a system alert (msg-1967 N-5-B).
    assert "system alert" in args["propose_content"].lower()
    # And names the design ADRs the PR-side is expected to cite (msg-1967).
    assert "ADR-2026-05-29-10" in args["propose_content"]
    assert "ADR-2026-06-03-16" in args["propose_content"]


@pytest.mark.anyio
async def test_open_alert_returns_already_exists_when_readback_sees_active_thread() -> None:
    """Second (or Nth) open: refusal + read-back showing ``status='active'`` folds
    to ``already_exists=True``.

    Pins msg-1963 D-4 (the fixed thread_id IS the idempotency key) via
    T-gate-bootstrap-close-retried-on-resolved-thread S-4-prime (Bohr msg-2460 §2).
    The measured error_type for a duplicate open is deliberately NOT included
    in the fake — the design does not read it. What decides is the observed
    world state via ``_read_thread_reading_or_none``.

    The fake's ``chatroom_open_thread`` error_type is
    ``ChatroomIntegrityError``, which matches origin/main's fixture pin but is
    NOT itself a measured value in this codebase — it is preserved for the
    regression contract that this test's predecessor pinned (Bohr msg-2460
    §5: "テストが測定値のふりをしていないか"). If the server ever emits a
    different error_type here, this test still passes because the discriminator
    is now the read-back's ``status`` field, not the error_type.
    """
    project = "spirrow-verimend"
    thread_id = "T-gate-bootstrap-spirrow-verimend"
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomIntegrityError",
                f"Thread {thread_id!r} already exists in project {project!r}",
            ),
            # Read-back after refusal MUST see an active thread. Any other
            # status ("resolved" / "parked" / unknown) would raise — see the
            # sibling tests below.
            "chatroom_get_thread": _thread_summary_ok(project, thread_id, status="active"),
        }
    )
    result = await open_alert(mcp, project=project)
    assert result == OpenResult(thread_id=thread_id, already_exists=True)
    # Load-bearing: the read-back was actually issued.
    reads = [name for name, _ in mcp.calls if name == "chatroom_get_thread"]
    assert reads == ["chatroom_get_thread"]


@pytest.mark.anyio
async def test_open_alert_accepts_awaiting_reply_as_target_reached() -> None:
    """``status == "awaiting_reply"`` also means the alert is up and visible.

    Pins T-gate-bootstrap-close-retried-on-resolved-thread S-4-prime (Bohr msg-2460
    §2): both ``active`` and ``awaiting_reply`` are the target statuses for
    ``open_alert`` because the goal is "an alert a reader would see", and both
    values satisfy that. Same result as the ``active`` case above.
    """
    project = "spirrow-verimend"
    thread_id = "T-gate-bootstrap-spirrow-verimend"
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomIntegrityError",
                f"Thread {thread_id!r} already exists",
            ),
            "chatroom_get_thread": _thread_summary_ok(project, thread_id, status="awaiting_reply"),
        }
    )
    result = await open_alert(mcp, project=project)
    assert result == OpenResult(thread_id=thread_id, already_exists=True)


@pytest.mark.anyio
async def test_open_alert_raises_when_readback_sees_resolved_thread() -> None:
    """N-4 (Bohr msg-2460 §5): open refusal + read-back showing ``status='resolved'``
    MUST raise. It MUST NOT return ``already_exists=True``.

    This is Einstein msg-2459 correctness blocking pinned as a regression test.
    The prior implementation would have folded any "existence" observation into
    ``already_exists=True``, hiding the fact that the alert is NOT up — it was
    up and got resolved, and the fixed thread_id cannot host a second life-cycle
    (Bohr msg-2460 §3, recorded as a scope-boundaried defect). The raise
    message must name that condition so the operator sees why.

    A regression that reintroduces "existence-based" acceptance of the target
    state would fail this test.
    """
    project = "spirrow-verimend"
    thread_id = "T-gate-bootstrap-spirrow-verimend"
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomIntegrityError",
                f"Thread {thread_id!r} already exists",
            ),
            "chatroom_get_thread": _thread_summary_ok(
                project, thread_id, status="resolved", resolved_by_msg="msg-429"
            ),
        }
    )
    with pytest.raises(MagickitMcpError) as excinfo:
        await open_alert(mcp, project=project)
    # The raise must NAME the observed state so the operator can distinguish
    # "target unreached" from "transport failure" from "unknown".
    assert "state=resolved" in str(excinfo.value)
    assert "msg-429" in str(excinfo.value)
    # And the message must NAME the scope-boundaried defect so the next reader
    # does not have to re-derive that the fixed thread_id blocks re-open.
    assert "fixed thread_id" in str(excinfo.value)


@pytest.mark.anyio
async def test_open_alert_raises_when_readback_status_is_absent() -> None:
    """N-6 (Bohr msg-2460 §5): open refusal + read-back showing the thread is
    absent MUST raise.

    An open refusal followed by an "absent" observation is contradictory
    (we just tried to open — the target should exist if we succeeded, and if
    the refusal was real, the thread must exist for our refusal to make sense).
    The safe direction is raise (fail-closed).
    """
    project = "spirrow-verimend"
    thread_id = "T-gate-bootstrap-spirrow-verimend"
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomIntegrityError",
                f"Thread {thread_id!r} already exists",
            ),
            # Read-back itself refuses with not-found; ``_read_thread_reading_or_none``
            # returns None on ANY read failure (never inspects the exception).
            "chatroom_get_thread": _error_envelope(
                "ChatroomNotFoundError",
                f"Thread {thread_id!r} not found",
            ),
        }
    )
    with pytest.raises(MagickitMcpError) as excinfo:
        await open_alert(mcp, project=project)
    assert "state=unavailable" in str(excinfo.value)


@pytest.mark.anyio
async def test_open_alert_reraises_other_envelopes() -> None:
    """Any refusal whose read-back cannot confirm the target state re-raises.

    Pins msg-1963 D-6 open-side fail-closed rule via S-4-prime: only the deliberate
    collision (refusal + read-back showing target state reached) is swallowed;
    everything else surfaces. The next tick retries (idempotent).
    """
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomAuthError", "not authorised to open thread"
            ),
            # Read-back returns an unshaped payload — treated as OPEN with
            # status=None, which is NOT in {active, awaiting_reply} → raise.
            "chatroom_get_thread": {},
        }
    )
    with pytest.raises(MagickitMcpError, match="ChatroomAuthError"):
        await open_alert(mcp, project="spirrow-verimend")


# --- close_alert ----------------------------------------------------------------------------------


def _thread_summary_ok(
    project: str,
    thread_id: str,
    *,
    status: str | None = None,
    resolved_by_msg: str | None = None,
) -> dict[str, Any]:
    """Minimal shape a ``chatroom_get_thread(mode='summary')`` OK returns.

    Verified against the live magickit schema in T-new-project-gate-bootstrap
    msg-2083 (Heisenberg S-1b probe): a summary hit returns a dict with keys
    ``thread``, ``messages``, ``mode``, ``digest``.

    ``status`` (T-gate-bootstrap-close-retried-on-resolved-thread S-3): the
    ``thread.status`` field the precheck / read-back now reads. Kept optional
    for backwards compatibility with tests that only care that the call
    succeeded (those tests treat "no status" as OPEN with ``status=None``,
    which is what the new ``_classify_reading`` produces — the safe direction:
    a malformed payload is not silently classified as "resolved").

    ``resolved_by_msg`` populates ``thread.resolved_by_msg`` — the field the
    incident report (Bohr msg-2456 §2) named as the ground truth for
    "who resolved the thread". Only populated when ``status='resolved'``.
    """
    thread: dict[str, Any] = {"project": project, "thread_id": thread_id}
    if status is not None:
        thread["status"] = status
    if resolved_by_msg is not None:
        thread["resolved_by_msg"] = resolved_by_msg
    return {
        "thread": thread,
        "messages": [],
        "mode": "summary",
        "digest": None,
    }


class _SequencedResult:
    """Return a different result each call to the same tool name.

    Used to fake state that changes between the precheck (first
    ``chatroom_get_thread``) and the post-refusal read-back (second
    ``chatroom_get_thread``) — the race/TOCTOU scenarios.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self._index = 0

    def __call__(self, _arguments: dict[str, Any]) -> Any:
        if self._index >= len(self._results):
            raise AssertionError(
                f"_SequencedResult exhausted: {len(self._results)} results scripted"
            )
        result = self._results[self._index]
        self._index += 1
        return result


@pytest.mark.anyio
async def test_close_alert_prechecks_then_closes_with_current_schema() -> None:
    """Close path: precheck confirms thread exists, then close is called with the
    contract magickit's ``chatroom_close_thread`` actually requires.

    Pins T-new-project-gate-bootstrap S-3b (Heisenberg msg-2083 §Bonus, Bohr
    msg-2084 §5(2)): the payload uses ``author`` (not ``owner``),
    ``summary_content`` (not ``decide_content``), and ``embodiment`` (required
    at ADR-2026-05-29-12's mandatory-on-state-transition boundary since close
    emits a decide msg). ``tags`` carries both ``system-alert`` and
    ``gate-bootstrap`` — the former is the tag msg-1968's carve-out predicate
    names for the eventual role-check exemption, and emitting it now ensures
    the predicate fires when the magickit-side carve-out lands.

    Also pins the semantic form (msg-1967 N-5-B, msg-1965 N-4): no ``role`` /
    ``closeable_role`` claim — the sweeper does not have a role and never
    invents one to bypass the gate.
    """
    project = "spirrow-verimend"
    thread_id = "T-gate-bootstrap-spirrow-verimend"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id),
            "chatroom_close_thread": {"ok": True},
        }
    )
    result = await close_alert(
        mcp,
        project=project,
        merge_commit_sha="deadbeef",
    )
    assert result == CloseResult(thread_id=thread_id, was_open=True)

    precheck_args = mcp.args_for("chatroom_get_thread")
    assert precheck_args == {"project": project, "thread_id": thread_id, "mode": "summary"}

    args = mcp.args_for("chatroom_close_thread")
    assert args["project"] == project
    assert args["thread_id"] == thread_id
    # New contract (verified against live magickit in msg-2083 S-1b bonus).
    assert args["author"] == DEFAULT_SWEEPER_OWNER
    assert "summary_content" in args
    assert args["embodiment"] == "unknown"
    # Tags include ``system-alert`` (msg-1968 carve-out predicate) alongside
    # ``gate-bootstrap`` — matches what open_alert already commits to.
    assert set(args["tags"]) == set(ALERT_TAGS)
    assert "system-alert" in args["tags"]
    # merge_commit_sha lands in the summary body (evidence for the fact-sync).
    assert "deadbeef" in args["summary_content"]
    # Old field names (the msg-2024 mismatch) MUST NOT appear.
    assert "owner" not in args
    assert "decide_content" not in args
    # No role claim (msg-1965 N-4, msg-1967 N-5-B).
    assert "role" not in args
    assert "closeable_role" not in args


@pytest.mark.anyio
async def test_close_alert_precheck_not_found_makes_no_close_call() -> None:
    """A thread that is not there is already in the desired end-state (msg-1963 D-4)
    AND — new in S-3b — the close call is never issued.

    Pins Bohr msg-2027 §設計側の指摘 (ii) / msg-2084 §5(2)(ii) — the "呼ばない"
    behaviour that eliminates the write-shaped call every 5 minutes for the
    3 DECLARED projects that never had a T-gate-bootstrap-* thread opened
    (msg-2024 measurement: 153 close_refused/day). The precheck returns
    ``False`` and close_alert exits without touching ``chatroom_close_thread``.
    """
    project = "x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _error_envelope(
                "ChatroomNotFoundError",
                "Thread 'T-gate-bootstrap-x' not found in project 'x'",
            ),
        }
    )
    result = await close_alert(mcp, project=project)
    assert result == CloseResult(thread_id="T-gate-bootstrap-x", was_open=False)
    # Load-bearing: the write-shaped call was never issued.
    close_calls = [name for name, _ in mcp.calls if name == "chatroom_close_thread"]
    assert close_calls == []


@pytest.mark.anyio
async def test_close_alert_precheck_resolved_skips_close_entirely() -> None:
    """Bohr msg-2460 §6 item 1 (the direct fix for L-B in the incident report):
    if the precheck sees the thread is ALREADY ``resolved``, close_alert
    returns immediately WITHOUT issuing ``chatroom_close_thread``.

    Pins the 338/day close_refused traffic elimination the incident report
    named (Bohr msg-2456 §1). Prior behaviour: the two-value precheck saw
    "exists" and issued a doomed close on every tick. New behaviour: the
    three-value precheck sees ``RESOLVED`` and skips.

    Load-bearing assertion pair: NO write-shaped call AND the
    ``resolved_by_msg`` observation surfaces on the ``CloseResult`` so the
    tick's log line can carry it (msg-2456 §2 named ``msg-429`` as the
    ground truth for the incident).
    """
    project = "spirrow-playproof"
    thread_id = "T-gate-bootstrap-spirrow-playproof"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(
                project, thread_id, status="resolved", resolved_by_msg="msg-429"
            ),
        }
    )
    result = await close_alert(mcp, project=project)
    assert result == CloseResult(thread_id=thread_id, was_open=False, resolved_by_msg="msg-429")
    # Load-bearing: NO write-shaped call was issued.
    close_calls = [name for name, _ in mcp.calls if name == "chatroom_close_thread"]
    assert close_calls == []
    # Exactly one read (the precheck) — no second read either, because we
    # returned before any refusal.
    reads = [name for name, _ in mcp.calls if name == "chatroom_get_thread"]
    assert reads == ["chatroom_get_thread"]


@pytest.mark.anyio
async def test_close_alert_precheck_resolved_without_resolved_by_msg_surfaces_none() -> None:
    """A ``resolved`` thread without a ``resolved_by_msg`` field still swallows.

    ``resolved_by_msg`` is exposed for the log; its absence is diagnostic
    information, not a failure. Pins the safe direction: the observed
    ``status='resolved'`` alone determines the swallow.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id, status="resolved"),
        }
    )
    result = await close_alert(mcp, project=project)
    assert result == CloseResult(thread_id=thread_id, was_open=False, resolved_by_msg=None)


@pytest.mark.anyio
async def test_close_alert_race_resolved_between_precheck_and_close_swallows() -> None:
    """TOCTOU: precheck sees ``active``, close is refused, read-back sees
    ``resolved`` → swallow (DoD #4).

    Two ``chatroom_get_thread`` calls happen in this scenario, and each sees
    a different state (``active`` first, ``resolved`` second — the parallel
    operator resolved it in between). The refusal from ``close_thread`` is
    NOT inspected; only the read-back's observation of ``status='resolved'``
    decides the swallow. This is Bohr msg-2458 §2 as adopted after Einstein
    msg-2457's invariant blocking.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _SequencedResult(
                [
                    _thread_summary_ok(project, thread_id, status="active"),
                    _thread_summary_ok(
                        project, thread_id, status="resolved", resolved_by_msg="msg-777"
                    ),
                ]
            ),
            # Note: the error_type here is intentionally NOT
            # 'ChatroomStateError' — the design does not inspect it.
            "chatroom_close_thread": _error_envelope(
                "SomeArbitraryClassifier",
                "close refused because a human resolved it first",
            ),
        }
    )
    result = await close_alert(mcp, project=project)
    assert result == CloseResult(thread_id=thread_id, was_open=False, resolved_by_msg="msg-777")
    # Load-bearing: two reads happened (precheck + recheck) and one close.
    reads = [name for name, _ in mcp.calls if name == "chatroom_get_thread"]
    assert reads == ["chatroom_get_thread", "chatroom_get_thread"]
    closes = [name for name, _ in mcp.calls if name == "chatroom_close_thread"]
    assert closes == ["chatroom_close_thread"]


@pytest.mark.anyio
async def test_close_alert_raises_when_recheck_sees_active_after_refusal() -> None:
    """DoD #2 (widening-is-bounded): close refused + read-back sees a state that
    is NOT ``resolved`` MUST raise. Proves the swallow does not degrade into
    "swallow every close refusal that has a live thread on the other side".

    A regression that mis-scoped the recheck predicate (e.g. "swallow if any
    reading succeeds") would fail this test.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id, status="active"),
            "chatroom_close_thread": _error_envelope(
                "ChatroomPermissionError",
                "closeable_roles check failed",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError) as excinfo:
        await close_alert(mcp, project=project)
    # The raise must NAME the observed post-refusal state so the operator can
    # distinguish "target unreached" (this test) from "target reached but
    # refusal fired" (which is impossible — read-back returning 'resolved'
    # swallows the refusal by contract).
    assert "state=open" in str(excinfo.value)
    assert "'active'" in str(excinfo.value)


@pytest.mark.anyio
async def test_close_alert_raises_when_recheck_cannot_confirm_state_after_refusal() -> None:
    """Race variant: precheck sees ``active``, close is refused, read-back
    itself refuses (any envelope) → raise.

    This inverts the origin/main behaviour which swallowed a not-found
    envelope at the close boundary. Under the new design (Bohr msg-2460 §3),
    absence cannot be positively observed at the read-back — the only way to
    observe it would be to re-introduce the exact ``error_type`` filter this
    thread was opened to remove. The safe direction is raise; the next tick's
    precheck re-classifies to ABSENT and the state self-heals in 1 loud line.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _SequencedResult(
                [
                    _thread_summary_ok(project, thread_id, status="active"),
                    # Second read (the recheck) refuses with not-found. The
                    # design does not swallow this — see docstring.
                    _error_envelope(
                        "ChatroomNotFoundError",
                        f"Thread {thread_id!r} not found in project {project!r}",
                    ),
                ]
            ),
            "chatroom_close_thread": _error_envelope(
                "SomeArbitraryClassifier",
                "close refused because the thread disappeared under us",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError) as excinfo:
        await close_alert(mcp, project=project)
    assert "state=unavailable" in str(excinfo.value)


@pytest.mark.anyio
async def test_close_alert_raises_close_refused_on_precheck_permission_fault() -> None:
    """A permission-shaped envelope at the *precheck* boundary surfaces loudly
    as GateBootstrapCloseError — same contract as the close boundary.

    Pins the fix for the PR-gate blocking objection on PR #200 (invariant
    class) and Bohr msg-2087 §3(3): the invariant that
    ``close_alert``'s docstring asserts — "any other envelope raises
    :class:`GateBootstrapCloseError`" — must hold across BOTH the read
    (precheck) and the write (close) boundaries. The prior implementation
    caught permission faults only at the write boundary; a read-side fault
    from ``chatroom_get_thread`` would leak an unwrapped ``MagickitMcpError``
    and the close_alert contract would silently break. That leak would also
    look identical in the operator log to a benign "no thread here" answer,
    so every alert would fail closed without ever reaching the msg-1968
    obligation's failure surface. Discriminated by the same "not found" check
    that ``_alert_thread_state`` uses to classify the benign ABSENT case.
    """
    project = "x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _error_envelope(
                "ChatroomPermissionError",
                "read access denied: 'orchestrator' lacks required role",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError, match="precheck refused"):
        await close_alert(mcp, project=project)
    # Load-bearing: because the precheck raised, the write-shaped call was never
    # issued — the invariant does not weaken to "close_thread only".
    close_calls = [name for name, _ in mcp.calls if name == "chatroom_close_thread"]
    assert close_calls == []
    # And the wrapped exception carries the original envelope via __cause__ so
    # the operator can read the underlying error type — same shape as the
    # close-boundary wrap below.
    try:
        await close_alert(mcp, project=project)
    except GateBootstrapCloseError as wrapped:
        assert isinstance(wrapped.__cause__, MagickitMcpError)
        assert "read access denied" in str(wrapped.__cause__)


@pytest.mark.anyio
async def test_close_alert_raises_close_refused_on_role_check_denial() -> None:
    """A permission-shaped envelope surfaces loudly as GateBootstrapCloseError.

    This is the msg-1968 obligation: if magickit's ``chatroom_close_thread``
    enforces ``closeable_roles`` BEFORE the owner check, the sweeper cannot
    close. That must not be swallowed — the operator needs to see it so the
    server-side carve-out (``system-alert`` tag + owner-close permits close)
    can be added upstream. Never inventing a role on the sweeper is the
    design's non-negotiable (msg-1965 N-4).

    With S-3b's precheck in place, this envelope now arrives at the close
    call after the precheck confirms the thread is present — the surfacing
    contract is unchanged.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id),
            "chatroom_close_thread": _error_envelope(
                "ChatroomPermissionError",
                "closeable_roles check failed: 'orchestrator' lacks required role",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError, match="closeable_roles"):
        await close_alert(mcp, project=project)


@pytest.mark.anyio
async def test_precheck_not_swallowed_when_permission_error_text_contains_not_found() -> None:
    """Adversarial mock — a permission fault whose free-form ``error`` text
    happens to contain the phrase "not found" must NOT be swallowed as absent.

    Pins the naysayer's PR-gate objection on head ``9b023fe``: the round-2
    discriminator ``"not found" in str(exc).lower()`` searched natural-language
    text the server writes freely, so a genuine ``ChatroomPermissionError``
    reading "role 'orchestrator' not found in closeable_roles" was classified
    benign and swallowed.

    **What passing this proves, and what it does not.** It proves this one
    envelope is not swallowed. It does *not* prove the discriminator is
    non-textual — round 3's substring predicate also passed this test while
    remaining forgeable, which is precisely how the defect survived a round.
    The test that separates textual from field-based is
    :func:`test_discriminator_ignores_free_form_text_that_forges_the_old_marker`;
    this one stays as a regression pin on the older, weaker forgery.
    """
    project = "x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _error_envelope(
                "ChatroomPermissionError",
                "read access denied: role 'orchestrator' not found in closeable_roles",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError, match="precheck refused"):
        await close_alert(mcp, project=project)
    close_calls = [name for name, _ in mcp.calls if name == "chatroom_close_thread"]
    assert close_calls == []


@pytest.mark.anyio
async def test_close_boundary_not_swallowed_when_permission_error_text_contains_not_found() -> None:
    """Sibling of the precheck adversarial test at the *close* boundary.

    The close-boundary swallow calls the SAME predicate as the precheck (Bohr
    msg-2139 §5 併せて 1 点: 二つの swallow が二つの述語を持つと片方だけが
    直る形の divergence を作る). This test proves both boundaries reject the
    same adversarial input, so any future change to the predicate affects both
    by construction.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id),
            "chatroom_close_thread": _error_envelope(
                "ChatroomPermissionError",
                "closeable_roles check failed: role 'orchestrator' not found in project role registry",  # noqa: E501
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError, match="closeable_roles"):
        await close_alert(mcp, project=project)


def _elevated(payload: dict[str, Any]) -> MagickitMcpError:
    """The exception a real call would raise for ``payload``.

    Every classification test below goes through :func:`raise_if_envelope`
    rather than constructing :class:`MagickitMcpError` by hand. Hand-built
    exceptions were what let round 3's tests agree with round 3's bug: a test
    that writes its own message string is testing the test's idea of the
    envelope, not the client's. This is the same rule ``raise_if_envelope``'s
    own docstring states as DoD #3 ("the fake is raised from the measured
    envelope shape").
    """
    try:
        raise_if_envelope(payload)
    except MagickitMcpError as exc:
        return exc
    pytest.fail(f"raise_if_envelope did not raise on {payload!r}")


def test_raise_if_envelope_carries_error_type_to_the_discriminator() -> None:
    """The one coupling that survives D-1: the client must publish the parsed
    ``error_type``, and this module must read the same attribute.

    Round 3's coupling test pinned a *message format* — that
    ``error_type='ChatroomNotFoundError'`` appears as a substring of
    ``_elevation_message``'s output. That coupling is gone, deliberately:
    nothing here depends on how the message is formatted any more, so a format
    change can no longer break or silently weaken the discriminator. What
    replaces it is narrower and is pinned here — the attribute must exist and
    must carry the envelope's own field value.

    Envelope shape is the recorded one, measured live against the magickit MCP
    endpoint on 2026-09-03 (a read of a thread id that was never opened):
    ``{"error_type": "ChatroomNotFoundError", "error": "Thread '...' not found
    in project '...'", "details": {...}}``.
    """
    from spirrow_mindwire.gate_bootstrap import (
        _NOT_FOUND_ERROR_TYPE,
        _is_thread_not_found_envelope,
    )

    exc = _elevated(
        _error_envelope(
            "ChatroomNotFoundError",
            "Thread 'T-gate-bootstrap-x' not found in project 'x'",
            project="x",
            thread_id="T-gate-bootstrap-x",
        )
    )
    assert exc.error_type == _NOT_FOUND_ERROR_TYPE
    assert _is_thread_not_found_envelope(exc) is True


def test_discriminator_ignores_free_form_text_that_forges_the_old_marker() -> None:
    """The PR-gate objection on head ``523d400``, as an executable negative
    control.

    Round 3 matched the literal ``error_type='ChatroomNotFoundError'`` in
    ``str(exc)``. The gate's counter-example: an envelope classified
    ``ChatroomPermissionError`` whose free-form ``error`` text *quotes* that
    literal. Because ``str(exc)`` flattens the machine-owned field and the
    server's prose into one string, the quote matched and an adversarial
    permission fault was swallowed as a benign absence.

    Field equality cannot be reached by the ``error`` value at all. Both
    assertions below are load-bearing: the first shows the forgery is still
    present in the flattened message (so this test would go green for the wrong
    reason if someone "fixed" it by sanitising the message instead), the second
    shows the classification is unaffected by it.
    """
    from spirrow_mindwire.gate_bootstrap import _is_thread_not_found_envelope

    exc = _elevated(
        _error_envelope(
            "ChatroomPermissionError",
            "Project access denied: error_type='ChatroomNotFoundError'",
        )
    )
    assert "error_type='ChatroomNotFoundError'" in str(exc)
    assert _is_thread_not_found_envelope(exc) is False


def test_discriminator_rejects_prose_matches_and_non_envelope_failures() -> None:
    """The remaining shapes that must not be read as "thread is absent".

    Retained from round 3's property-style pin (Bohr msg-2139 §5 併せて 1 点)
    but rebuilt through :func:`raise_if_envelope`, and extended with the two
    cases D-1 makes newly checkable.
    """
    from spirrow_mindwire.gate_bootstrap import _is_thread_not_found_envelope
    from spirrow_mindwire.magickit.client import _ELEVATION_VALUE_LIMIT

    # Prose containing "not found" under a different classification.
    perm = _elevated(
        _error_envelope(
            "ChatroomPermissionError",
            "role 'orchestrator' not found in closeable_roles",
        )
    )
    assert _is_thread_not_found_envelope(perm) is False

    # A transport failure carries no envelope at all, so it has no
    # classification to compare — and must therefore surface, not be swallowed.
    transport = MagickitMcpError(
        "magickit MCP call 'chatroom_get_thread' failed: "
        "ConnectionError: could not reach project 'spirrow-not-found-x'"
    )
    assert transport.error_type is None
    assert _is_thread_not_found_envelope(transport) is False

    # Truncation cannot manufacture a match: an over-long classification is
    # capped by ``_bounded_str``, and the capped form is longer than the cap
    # itself, so it can never equal a 21-character enum name.
    overlong = _elevated(_error_envelope("ChatroomNotFoundError" * 40, "an absurd classification"))
    assert overlong.error_type is not None
    assert len(overlong.error_type) > _ELEVATION_VALUE_LIMIT
    assert _is_thread_not_found_envelope(overlong) is False


@pytest.mark.anyio
async def test_close_alert_precheck_and_close_boundaries_both_surface_the_forgery() -> None:
    """Both boundaries reject the forged permission envelope, though for
    DIFFERENT structural reasons after S-2-prime.

    Under origin/main this was "one predicate, two swallows". Under the new
    design (Bohr msg-2460 §6): the precheck STILL uses
    :func:`_is_thread_not_found_envelope` (the ONE remaining envelope-name
    classification site — see its docstring); the close boundary NO LONGER
    uses it (post-refusal read-back replaces the swallow entirely). The test
    survives the refactor because the *outcome* — GateBootstrapCloseError at
    either boundary — is preserved, even as the mechanism at each has
    diverged. That divergence is deliberate (msg-2460 §6 item 4: the two
    swallows have different target-state predicates: RESOLVED vs OPEN read
    payload).
    """
    forged = _error_envelope(
        "ChatroomPermissionError",
        "Project access denied: error_type='ChatroomNotFoundError'",
    )

    # Precheck boundary: forged permission fault at the read → surfaces via
    # ``_is_thread_not_found_envelope`` returning False → re-raise → wrap.
    at_precheck = _FakeMcp(results={"chatroom_get_thread": forged})
    with pytest.raises(GateBootstrapCloseError, match="precheck refused"):
        await close_alert(at_precheck, project="x")
    assert [name for name, _ in at_precheck.calls] == ["chatroom_get_thread"]

    # Close boundary: precheck succeeds → close is issued → close fails →
    # read-back runs (second chatroom_get_thread call) → observed status is
    # None (no ``status`` field on ``_thread_summary_ok`` here) → NOT
    # ``resolved`` → raise. Note we intentionally hit the same programmed
    # ``chatroom_get_thread`` result for BOTH the precheck and the recheck;
    # both see the same OPEN reading, which is what should happen under
    # this fake.
    at_close = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok("x", "T-gate-bootstrap-x"),
            "chatroom_close_thread": forged,
        }
    )
    with pytest.raises(GateBootstrapCloseError, match="chatroom_close_thread refused"):
        await close_alert(at_close, project="x")


# --- N-1..N-6 (S-2-prime / S-4-prime regression pins) -------------------------------------------


@pytest.mark.anyio
async def test_close_alert_swallows_when_random_error_type_and_readback_sees_resolved() -> None:
    """N-1 (the name-drift immunity test — the design's core invariant).

    The whole point of S-2-prime (Bohr msg-2458 §2 as adopted after Einstein
    msg-2457 invariant blocking) is that the close-refusal swallow does NOT
    depend on the ``error_type`` string. This test proves it: the fake serves
    a completely made-up ``error_type='ZZZUnknownFuture'`` at the close
    refusal, but the read-back sees ``status='resolved'`` — the swallow MUST
    still fire.

    A regression that reintroduces a name-based filter at the entry of the
    refusal branch (e.g. ``if exc.error_type == "ChatroomStateError": swallow``)
    would fail this test. That is the failure Einstein msg-2457 named as
    "設計原則を自ら破壊している".

    Bohr msg-2460 §6 DoD N-1: "このテストが無いと、将来誰かが「安価な絞り
    込み」を復活させても全テストが通ってしまう。本スレッドの争点そのもの
    を固定する回帰テストなので、DoD の筆頭に置く。"
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _SequencedResult(
                [
                    _thread_summary_ok(project, thread_id, status="active"),
                    _thread_summary_ok(
                        project, thread_id, status="resolved", resolved_by_msg="msg-99"
                    ),
                ]
            ),
            # Deliberately not a known error_type. The design must not depend
            # on this string at all.
            "chatroom_close_thread": _error_envelope(
                "ZZZUnknownFuture",
                "some far-future refusal reason nobody has measured",
            ),
        }
    )
    result = await close_alert(mcp, project=project)
    assert result == CloseResult(thread_id=thread_id, was_open=False, resolved_by_msg="msg-99")


@pytest.mark.anyio
async def test_close_alert_readback_runs_regardless_of_error_type() -> None:
    """N-2 (Bohr msg-2460 §6 DoD): the read-back must not be gated by any
    error_type filter — every ``MagickitMcpError`` at the close boundary
    triggers a read-back.

    Complements N-1: N-1 proves the swallow FIRES on an unknown error_type
    when the world reached the target state. N-2 proves the read-back is
    UNCONDITIONALLY entered on any refusal — no shortcut path exists that
    skips the recheck based on a name comparison.

    Exercised by scripting three refusals with different error_types and
    verifying that each drives the same number of read-back calls.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"

    async def _run_one(refusal_error_type: str) -> _FakeMcp:
        mcp = _FakeMcp(
            results={
                "chatroom_get_thread": _SequencedResult(
                    [
                        _thread_summary_ok(project, thread_id, status="active"),
                        _thread_summary_ok(
                            project, thread_id, status="resolved", resolved_by_msg="msg-99"
                        ),
                    ]
                ),
                "chatroom_close_thread": _error_envelope(
                    refusal_error_type, "arbitrary refusal reason"
                ),
            }
        )
        result = await close_alert(mcp, project=project)
        assert result.was_open is False
        return mcp

    for error_type in ("ChatroomStateError", "ChatroomPermissionError", "TotallyMadeUp"):
        mcp = await _run_one(error_type)
        # Two reads: the precheck AND the post-refusal read-back. Load-bearing.
        reads = [name for name, _ in mcp.calls if name == "chatroom_get_thread"]
        assert reads == ["chatroom_get_thread", "chatroom_get_thread"], (
            f"error_type={error_type!r} skipped the read-back — a hidden name-filter has reappeared"
        )


@pytest.mark.anyio
async def test_close_alert_readback_failure_at_recheck_boundary_raises() -> None:
    """N-3 (Bohr msg-2460 §6 DoD): if the post-refusal read-back itself fails
    (any envelope, transport, malformed) the caller RAISES — fail-closed.

    ``_read_thread_reading_or_none`` returns ``None`` on any read failure by
    design; the caller must interpret ``None`` as "state cannot be confirmed"
    and raise. Silent swallowing would re-introduce the "wrote a swallow
    against something we cannot see" bug family.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _SequencedResult(
                [
                    _thread_summary_ok(project, thread_id, status="active"),
                    # Recheck refuses with a permission fault (any envelope
                    # would do — the design does not distinguish).
                    _error_envelope(
                        "ChatroomPermissionError",
                        "readback denied",
                    ),
                ]
            ),
            "chatroom_close_thread": _error_envelope(
                "ChatroomStateError",
                "Cannot close thread 'T-gate-bootstrap-x' in status='resolved'",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError) as excinfo:
        await close_alert(mcp, project=project)
    assert "state=unavailable" in str(excinfo.value)


@pytest.mark.anyio
async def test_close_alert_precheck_open_status_active_proceeds_to_close() -> None:
    """N-5 companion (positive control): if the precheck sees a live thread
    that is NOT ``resolved``, the close IS issued.

    Not a bug-pin per se — this is the ordinary path — but it is load-bearing
    that the tri-state precheck did not accidentally collapse "OPEN" into
    "skip" as well. Without this control, N-3 could pass on a broken
    implementation that never issues the close.
    """
    project = "x"
    thread_id = "T-gate-bootstrap-x"
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id, status="active"),
            "chatroom_close_thread": {"ok": True},
        }
    )
    result = await close_alert(mcp, project=project)
    assert result == CloseResult(thread_id=thread_id, was_open=True, resolved_by_msg=None)
    closes = [name for name, _ in mcp.calls if name == "chatroom_close_thread"]
    assert closes == ["chatroom_close_thread"]


# --- CLI wrapper (scripts/gate_bootstrap_tick.py) -----------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "gate_bootstrap_tick.py"


def _load_cli_module() -> Any:
    """Load the standalone CLI script as a module (it is not on the import path)."""
    spec = importlib.util.spec_from_file_location("_gate_bootstrap_cli_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLI = _load_cli_module()


@pytest.mark.anyio
async def test_cli_run_tick_declared_calls_close_only(tmp_path: Path) -> None:
    """DECLARED → precheck confirms thread exists, then close is issued once.

    Pins the closing side of msg-1963 D-4 through S-3b: when DECLARED, the
    tick asks whether the alert thread is present (read), and only if it is
    does it issue ``chatroom_close_thread`` (write). Same idempotency
    contract as before, tighter execution semantics — Bohr msg-2027 §設計
    側の指摘 (ii): 「閉じる対象が無いなら呼ばない」.
    """
    project = "spirrow-example"
    thread_id = "T-gate-bootstrap-spirrow-example"
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    fake = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(project, thread_id),
            "chatroom_close_thread": {"ok": True},
        }
    )
    exit_code, out = await _CLI._run_tick(
        project,
        tmp_path,
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha="deadbeef",
        mcp_factory=lambda _url: fake,
    )
    assert exit_code == 0
    assert out["status"] == GateStatus.DECLARED.value
    assert out["action"] == "closed"
    assert out["thread_id"] == thread_id
    # Precheck: one read against the exact thread id, mode='summary'.
    assert fake.args_for("chatroom_get_thread") == {
        "project": project,
        "thread_id": thread_id,
        "mode": "summary",
    }
    args = fake.args_for("chatroom_close_thread")
    # New contract (S-3b). The old {owner, decide_content} shape would
    # never have reached the tool; this asserts the fix positively.
    assert args["author"] == DEFAULT_SWEEPER_OWNER
    assert "summary_content" in args
    assert args["embodiment"] == "unknown"
    assert "system-alert" in args["tags"]
    assert "owner" not in args
    assert "decide_content" not in args
    assert "role" not in args


@pytest.mark.anyio
async def test_cli_run_tick_declared_skips_close_when_precheck_sees_resolved(
    tmp_path: Path,
) -> None:
    """DECLARED but the alert thread is ALREADY resolved → tick issues NO write
    AND surfaces ``resolved_by_msg`` in the JSON output.

    This is the direct external-facing end of the incident report (Bohr
    msg-2456 §1): the 338/day close_refused traffic on playproof / lexora
    stops here. Under origin/main, the two-value precheck saw "exists" and
    issued a doomed close on every tick; under S-3, the three-value precheck
    sees ``RESOLVED`` and skips.

    The ``resolved_by_msg`` surfacing is what turns the log line from opaque
    ("we closed it — or thought we did") into self-explaining ("thread was
    already resolved by ``msg-429``" — the exact form the incident report
    §2 named as the ground truth).
    """
    project = "spirrow-playproof"
    thread_id = "T-gate-bootstrap-spirrow-playproof"
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    fake = _FakeMcp(
        results={
            "chatroom_get_thread": _thread_summary_ok(
                project, thread_id, status="resolved", resolved_by_msg="msg-429"
            ),
        }
    )
    exit_code, out = await _CLI._run_tick(
        project,
        tmp_path,
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha=None,
        mcp_factory=lambda _url: fake,
    )
    assert exit_code == 0
    assert out["status"] == GateStatus.DECLARED.value
    assert out["action"] == "already_closed"
    assert out["thread_id"] == thread_id
    # Load-bearing: NO write-shaped call was issued (the L-B fix).
    close_calls = [name for name, _ in fake.calls if name == "chatroom_close_thread"]
    assert close_calls == []
    # And the observed ``resolved_by_msg`` surfaces on the tick's JSON output
    # so the operator sees WHO resolved the thread (msg-2456 §2 ground truth).
    assert out["resolved_by_msg"] == "msg-429"


@pytest.mark.anyio
async def test_cli_run_tick_declared_skips_close_when_alert_thread_absent(
    tmp_path: Path,
) -> None:
    """DECLARED but no alert thread was ever opened → tick issues NO write.

    Pins the fix for the msg-2024 measurement: three DECLARED projects with
    no T-gate-bootstrap-<project> thread ever opened were still issuing
    ``chatroom_close_thread`` on every tick, 153 close_refused/day. After
    S-3b, the precheck says "not found" and no write is issued; the tick
    still reports ``already_closed`` because from the mechanism's point of
    view the desired end-state (thread not open) is already the case
    (msg-1963 D-4).
    """
    project = "spirrow-example"
    thread_id = "T-gate-bootstrap-spirrow-example"
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    fake = _FakeMcp(
        results={
            "chatroom_get_thread": _error_envelope(
                "ChatroomNotFoundError",
                f"Thread {thread_id!r} not found in project {project!r}",
            ),
        }
    )
    exit_code, out = await _CLI._run_tick(
        project,
        tmp_path,
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha=None,
        mcp_factory=lambda _url: fake,
    )
    assert exit_code == 0
    assert out["status"] == GateStatus.DECLARED.value
    assert out["action"] == "already_closed"
    assert out["thread_id"] == thread_id
    # Load-bearing: NO write-shaped call was issued.
    close_calls = [name for name, _ in fake.calls if name == "chatroom_close_thread"]
    assert close_calls == []
    # And exactly one read-shaped precheck was made.
    read_calls = [name for name, _ in fake.calls if name == "chatroom_get_thread"]
    assert read_calls == ["chatroom_get_thread"]


@pytest.mark.anyio
async def test_cli_run_tick_new_repo_opens_alert(tmp_path: Path) -> None:
    """NEW_REPO → the tick opens the T-gate-bootstrap-<project> thread.

    Pins the whole point of msg-1965 N-1: a brand-new repo IS an alert, not a
    UNUSABLE fall-closed. The tick opens the fixed-id thread and reports it.
    """
    _init_repo(tmp_path)
    fake = _FakeMcp(results={"chatroom_open_thread": {"ok": True}})
    exit_code, out = await _CLI._run_tick(
        "spirrow-newborn",
        tmp_path,
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha=None,
        mcp_factory=lambda _url: fake,
    )
    assert exit_code == 0
    assert out["status"] == GateStatus.NEW_REPO.value
    assert out["action"] == "opened"
    assert out["thread_id"] == "T-gate-bootstrap-spirrow-newborn"
    args = fake.args_for("chatroom_open_thread")
    assert args["thread_id"] == "T-gate-bootstrap-spirrow-newborn"
    assert set(args["tags"]) == set(ALERT_TAGS)


@pytest.mark.anyio
async def test_cli_run_tick_empty_repo_dir_makes_no_mcp_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty ``repo_dir`` → UNUSABLE → the tick makes ZERO MCP calls.

    The regression this pins is the PR-gate finding on PR #192: without the
    inspect_gate defence, an empty ``repo_dir`` would be silently classified
    as DECLARED (because the CWD, wrapped by ``Push-Location $repoRoot``,
    contains the host repo's own ``.mindwire-gate``), and the tick would
    fire ``chatroom_close_thread`` on every candidate on every tick. Here we
    place a gate in the CWD deliberately to reproduce the attack condition,
    then assert the fake MCP records NO calls at all.
    """
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.chdir(tmp_path)
    fake = _FakeMcp()
    exit_code, out = await _CLI._run_tick(
        "spirrow-example",
        Path(""),
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha=None,
        mcp_factory=lambda _url: fake,
    )
    assert exit_code == 0
    assert out["status"] == GateStatus.UNUSABLE.value
    assert out["action"] == "no_action"
    assert out["thread_id"] is None
    # The load-bearing assertion: no MCP traffic at all.
    assert fake.calls == []


def test_cli_main_reports_unusable_and_exits_zero(tmp_path: Path, capsys: Any) -> None:
    """The CLI wrapper prints one JSON object on stdout and exits 0 for UNUSABLE.

    UNUSABLE is fail-closed on the alert side but the tick itself is a
    successful nop — the sweep continues.
    """
    absent = tmp_path / "does-not-exist"
    exit_code = _CLI.main(
        ["--project", "x", "--repo-dir", str(absent), "--url", "http://not-called.invalid"]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out.strip())
    assert out["status"] == GateStatus.UNUSABLE.value
    assert out["action"] == "no_action"
    assert out["thread_id"] is None
    assert out["error"] is None
