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
async def test_open_alert_swallows_already_exists_as_success() -> None:
    """Second (or Nth) open: ``already exists`` envelope folds to ``already_exists=True``.

    Pins msg-1963 D-4 — the fixed thread_id IS the idempotency key. A second
    open in the project's lifetime is the expected call, not a bug.
    """
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomIntegrityError",
                "Thread 'T-gate-bootstrap-spirrow-verimend' already exists in project 'x'",
            ),
        }
    )
    result = await open_alert(mcp, project="spirrow-verimend")
    assert result == OpenResult(thread_id="T-gate-bootstrap-spirrow-verimend", already_exists=True)


@pytest.mark.anyio
async def test_open_alert_reraises_other_envelopes() -> None:
    """Any envelope other than ``already exists`` re-raises unchanged.

    Pins msg-1963 D-6 open-side fail-closed rule: swallow only the deliberate
    collision. Everything else is a genuine failure the operator must see; the
    next tick retries (idempotent).
    """
    mcp = _FakeMcp(
        results={
            "chatroom_open_thread": _error_envelope(
                "ChatroomAuthError", "not authorised to open thread"
            ),
        }
    )
    with pytest.raises(MagickitMcpError, match="ChatroomAuthError"):
        await open_alert(mcp, project="spirrow-verimend")


# --- close_alert ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_close_alert_closes_by_thread_id_with_owner_only() -> None:
    """Close call carries owner, no role. Fact-sync, not a judgement (msg-1967 N-5-B).

    Pins the semantic form: the sweeper opened this thread and the sweeper
    takes it down; the payload does NOT claim ``closeable_role``. Any role
    field would be the exact ADR-10 hack the design rejects (msg-1965 N-4).
    """
    mcp = _FakeMcp(results={"chatroom_close_thread": {"ok": True}})
    result = await close_alert(
        mcp,
        project="spirrow-verimend",
        merge_commit_sha="deadbeef",
    )
    assert result == CloseResult(thread_id="T-gate-bootstrap-spirrow-verimend", was_open=True)
    args = mcp.args_for("chatroom_close_thread")
    assert args["thread_id"] == "T-gate-bootstrap-spirrow-verimend"
    assert args["owner"] == DEFAULT_SWEEPER_OWNER
    # Explicit assertion: no role claim.
    assert "role" not in args
    assert "closeable_role" not in args
    # merge_commit_sha lands in the decide body (evidence for the fact-sync).
    assert "deadbeef" in args["decide_content"]


@pytest.mark.anyio
async def test_close_alert_swallows_not_found_as_already_closed() -> None:
    """A thread that is not there is already in the desired end-state (msg-1963 D-4)."""
    mcp = _FakeMcp(
        results={
            "chatroom_close_thread": _error_envelope(
                "ChatroomNotFoundError", "Thread 'T-gate-bootstrap-x' not found in project 'x'"
            ),
        }
    )
    result = await close_alert(mcp, project="x")
    assert result == CloseResult(thread_id="T-gate-bootstrap-x", was_open=False)


@pytest.mark.anyio
async def test_close_alert_swallows_already_resolved_as_already_closed() -> None:
    """A thread already closed by a human is also the desired end-state."""
    mcp = _FakeMcp(
        results={
            "chatroom_close_thread": _error_envelope(
                "ChatroomIntegrityError", "Thread already resolved"
            ),
        }
    )
    result = await close_alert(mcp, project="x")
    assert result == CloseResult(thread_id="T-gate-bootstrap-x", was_open=False)


@pytest.mark.anyio
async def test_close_alert_raises_close_refused_on_role_check_denial() -> None:
    """A permission-shaped envelope surfaces loudly as GateBootstrapCloseError.

    This is the msg-1968 obligation: if magickit's ``chatroom_close_thread``
    enforces ``closeable_roles`` BEFORE the owner check, the sweeper cannot
    close. That must not be swallowed — the operator needs to see it so the
    server-side carve-out (``system-alert`` tag + owner-close permits close)
    can be added upstream. Never inventing a role on the sweeper is the
    design's non-negotiable (msg-1965 N-4).
    """
    mcp = _FakeMcp(
        results={
            "chatroom_close_thread": _error_envelope(
                "ChatroomPermissionError",
                "closeable_roles check failed: 'orchestrator' lacks required role",
            ),
        }
    )
    with pytest.raises(GateBootstrapCloseError, match="closeable_roles"):
        await close_alert(mcp, project="x")


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
    """DECLARED → the tick attempts (idempotent) close of the alert thread.

    Pins the closing side of msg-1963 D-4: the same predicate opens and closes
    this alert. When DECLARED, the tick tries to take the thread down; if it
    was never open, ``already_closed`` folds it to a nop.
    """
    (tmp_path / ".mindwire-gate").write_text("#!/usr/bin/env bash\nexit 0\n")
    fake = _FakeMcp(results={"chatroom_close_thread": {"ok": True}})
    exit_code, out = await _CLI._run_tick(
        "spirrow-example",
        tmp_path,
        owner=DEFAULT_SWEEPER_OWNER,
        mcp_url=None,
        merge_commit_sha="deadbeef",
        mcp_factory=lambda _url: fake,
    )
    assert exit_code == 0
    assert out["status"] == GateStatus.DECLARED.value
    assert out["action"] == "closed"
    assert out["thread_id"] == "T-gate-bootstrap-spirrow-example"
    args = fake.args_for("chatroom_close_thread")
    assert args["owner"] == DEFAULT_SWEEPER_OWNER
    assert "role" not in args


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
