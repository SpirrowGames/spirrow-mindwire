"""A preflight that refuses the run must not count as a launch of the role.

T-preflight-failure-counts-toward-stall, design v3 (Bohr msg-9808 v1, msg-9810 v2, msg-9812 v3;
go Einstein msg-9813). Pinned here (the PowerShell side is tests/Test-PreflightRefused.ps1):

* D-1'' — every :class:`PreflightError` names its check (P0/P1/P2/P3, ``unexpected`` for the
  wrapped exception), and ``loop_runner.main`` maps it to exit 13 with exactly one
  ``MINDWIRE_PREFLIGHT_REFUSED_PAYLOAD`` row and no ``MINDWIRE_PREFLIGHT_OK`` row; the code is
  pinned to the sweep wrapper's ``$PreflightRefusedExitCode``.
* D-3' — a failed ``gh api`` is classified from stderr first; stdout is read only when stderr is
  empty; nothing on either stream → transient ``unclassified`` (retried, then deferred as exit
  11 with reason ``unclassified``), never "start unconfirmed".
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest
from test_preflight_transient import (
    OWNER,
    REPO,
    STDERR_404,
    _ep,
    _repo_steps,
    _rules,
    _run,
)

from spirrow_mindwire import loop_runner
from spirrow_mindwire import preflight as pf
from spirrow_mindwire.config import MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.preflight import (
    PREFLIGHT_DEFERRED_EXIT_CODE,
    PREFLIGHT_OK_PREFIX,
    PREFLIGHT_REFUSED_EXIT_CODE,
    PREFLIGHT_REFUSED_PAYLOAD_PREFIX,
    PreflightDeferredError,
    PreflightError,
    classify_gh_output,
    preflight_gate,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _cpe(stderr: str, stdout: str = "", rc: int = 1) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(rc, ["gh", "api"], output=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# D-3' — stderr first, stdout only when stderr is empty, nothing → unclassified
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stderr", "stdout", "expected"),
    [
        # stderr empty, stdout empty: the msg-9782 incident's shape → transient, unclassified.
        ("", "", ("transient", "unclassified")),
        ("  \n", "", ("transient", "unclassified")),
        # stderr empty, gh's JSON error body on stdout.
        ("", '{"message":"Server Error","status":"502"}', ("transient", "stdout")),
        ("", '{"message":"Too Many","status":"429"}', ("transient", "stdout")),
        ("", '{"message":"Not Found","status":"404"}', ("terminal", "stdout")),
        ("", '{"message":"Bad credentials","status": 401}', ("terminal", "stdout")),
        # stderr decides alone when it carries anything — a status on stdout cannot overturn it.
        ("gh: Not Found (HTTP 404)", '{"status":"502"}', ("terminal", "stderr")),
        ("gh: HTTP 502: Bad Gateway", '{"status":"404"}', ("transient", "stderr")),
        # A non-empty stderr nobody recognises stays terminal (exit 13, the operator is told).
        ("something nobody has seen before", "", ("terminal", "stderr")),
        ("something nobody has seen before", '{"status":"502"}', ("terminal", "stderr")),
    ],
)
def test_classify_gh_output_priority(stderr: str, stdout: str, expected: tuple[str, str]) -> None:
    assert classify_gh_output(stderr, stdout) == expected


def test_empty_stderr_is_retried_then_deferred_as_unclassified(tmp_path: Path) -> None:
    gh, _clock, sleeps, result = _run(tmp_path, {_ep(REPO): [(1.0, _cpe("", ""))]})
    assert isinstance(result, PreflightDeferredError)
    assert len(gh.calls) == pf.PREFLIGHT_MAX_ATTEMPTS
    assert sleeps == [2.0, 5.0, 10.0]
    payload = result.payload()
    assert payload["reason"] == "unclassified"
    # The detail names the exit code and both streams instead of "returned non-zero exit status 1".
    assert "gh exit=1" in str(payload["last_stderr"])
    assert "stderr=<empty>" in str(payload["last_stderr"])
    assert "stdout=<empty>" in str(payload["last_stderr"])


def test_empty_stderr_then_known_transient_defers_as_transient(tmp_path: Path) -> None:
    steps = [(1.0, _cpe("", "")), (1.0, _cpe("gh: HTTP 503: Service Unavailable"))]
    _gh, _clock, _sleeps, result = _run(tmp_path, {_ep(REPO): steps})
    assert isinstance(result, PreflightDeferredError)
    assert result.reason == "transient"


def test_empty_stderr_then_success_passes(tmp_path: Path) -> None:
    script = _repo_steps(REPO, _rules({t: [1] for t in pf._REQUIRED_RULE_TYPES}))
    script[_ep(REPO)] = [(1.0, _cpe("", "")), (1.0, {"default_branch": "main"})]
    active = {"enforcement": "active", "current_user_can_bypass": "never"}
    script[_ep(REPO, "/rulesets/1")] = [(1.0, active)]
    _gh, _clock, sleeps, result = _run(tmp_path, script)
    assert result == [f"{OWNER}/{REPO}"]
    assert sleeps == [2.0]


def test_empty_stderr_with_404_body_is_terminal_p1(tmp_path: Path) -> None:
    body = '{"message":"Not Found","status":"404"}'
    gh, _clock, sleeps, result = _run(tmp_path, {_ep(REPO): [(1.0, _cpe("", body))]})
    assert isinstance(result, PreflightError)
    assert result.check == "P1"
    assert len(gh.calls) == 1 and sleeps == []
    assert "Not Found" in result.detail


def test_terminal_404_stderr_names_p1(tmp_path: Path) -> None:
    _gh, _clock, _sleeps, result = _run(tmp_path, {_ep(REPO): [(1.0, _cpe(STDERR_404))]})
    assert isinstance(result, PreflightError) and result.check == "P1"


# --------------------------------------------------------------------------- #
# D-1'' — every refusal names its check
# --------------------------------------------------------------------------- #


def _gate(tmp_path: Path, **kw: object) -> PreflightError:
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir(exist_ok=True)
    daemon.mkdir(exist_ok=True)
    args: dict[str, object] = {
        "daemon_root": daemon,
        "api_caller": lambda _e, _t: pytest.fail("P1 must not be reached"),
        "remote_reader": lambda _p: [("origin", f"https://github.com/{OWNER}/{REPO}.git")],
        "git_toplevel": lambda _p: None,
        "hooks_path_reader": lambda _p: None,
    }
    args.update(kw)
    with pytest.raises(PreflightError) as ei:
        preflight_gate(target, **args)  # type: ignore[arg-type]
    return ei.value


def test_p0_names_its_check(tmp_path: Path) -> None:
    exc = _gate(tmp_path, daemon_root=tmp_path / "t")
    assert exc.check == "P0" and exc.detail.startswith("preflight P0 failed")


def test_p3_names_its_check(tmp_path: Path) -> None:
    exc = _gate(tmp_path)  # no core.hooksPath
    assert exc.check == "P3" and exc.detail.startswith("preflight P3 failed")


def test_p2_names_its_check(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    guard = hooks / "pre-push"
    guard.write_text("#!/bin/sh\n# mindwire-guard: non-ff-expect v1\n", encoding="utf-8")
    guard.chmod(0o755)
    exc = _gate(
        tmp_path,
        hooks_path_reader=lambda _p: str(hooks),
        remote_reader=lambda _p: [("origin", "https://example.com/x.git")],
    )
    assert exc.check == "P2" and exc.detail.startswith("preflight P2 failed")


def test_every_raise_site_in_preflight_names_a_check() -> None:
    src = (_REPO_ROOT / "src" / "spirrow_mindwire" / "preflight.py").read_text(encoding="utf-8")
    sites = len(re.findall(r"raise PreflightError\(", src))
    named = len(re.findall(r'check="P[0-3]"', src))
    assert sites == named == 15


def test_unexpected_exception_is_wrapped_as_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_p: Path) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(loop_runner, "preflight_gate", _boom)
    with pytest.raises(PreflightError) as ei:
        loop_runner._preflight(Stage3LoopConfig(repo_dir=tmp_path))
    assert ei.value.check == "unexpected"
    assert "boom" in ei.value.detail


def test_payload_detail_is_capped() -> None:
    exc = PreflightError("x" * 5000, check="P1")
    detail = str(exc.payload()["detail"])
    assert len(detail) == pf.PREFLIGHT_REFUSED_DETAIL_CAP + 1 and detail.endswith("…")


# --------------------------------------------------------------------------- #
# D-1'' — exit 13 and its payload row
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("check", ["P0", "P1", "P2", "P3", "unexpected"])
def test_main_maps_preflight_refusal_to_exit_13(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], check: pf.PreflightCheck
) -> None:
    async def _fake_run_conductor(_settings: MindwireSettings, **_kw: object) -> None:
        raise PreflightError(f"preflight {check} failed: could not fetch rules", check=check)

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == PREFLIGHT_REFUSED_EXIT_CODE == 13
    out = capsys.readouterr().out
    assert PREFLIGHT_OK_PREFIX.strip() not in out
    assert "reason=adapter_error" not in out
    rows = [ln for ln in out.splitlines() if ln.startswith(PREFLIGHT_REFUSED_PAYLOAD_PREFIX)]
    assert len(rows) == 1
    payload = json.loads(rows[0][len(PREFLIGHT_REFUSED_PAYLOAD_PREFIX) :])
    assert payload == {"check": check, "detail": f"preflight {check} failed: could not fetch rules"}


def test_deferral_still_maps_to_exit_11_not_13() -> None:
    # PreflightDeferredError is deliberately not a PreflightError, so the new except clause in
    # main cannot swallow it (its own clause sits above anyway).
    assert not issubclass(PreflightDeferredError, PreflightError)
    assert PREFLIGHT_DEFERRED_EXIT_CODE != PREFLIGHT_REFUSED_EXIT_CODE


def test_exit_code_is_the_next_free_one_and_pinned_to_the_wrapper() -> None:
    from spirrow_mindwire.clone_guard import DIRTY_CLONE_EXIT_CODE
    from spirrow_mindwire.conductor import run_budget, stall, stand_down
    from spirrow_mindwire.git_sync import GIT_SYNC_DEFERRED_EXIT_CODE
    from spirrow_mindwire.turn_isolation.errors import SECRETS_STOP_EXIT_CODE
    from spirrow_mindwire.turn_ref_snapshot import REF_SNAPSHOT_UNRECORDED_EXIT_CODE

    taken = {
        1,
        2,
        stand_down.STAND_DOWN_EXIT_CODE,
        stall.STALLED_EXIT_CODE,
        run_budget.RUN_TIMEOUT_EXIT_CODE,
        run_budget.RUN_KILLED_EXIT_CODE,
        run_budget.RUN_KILL_UNCONFIRMED_EXIT_CODE,
        DIRTY_CLONE_EXIT_CODE,
        GIT_SYNC_DEFERRED_EXIT_CODE,
        SECRETS_STOP_EXIT_CODE,
        PREFLIGHT_DEFERRED_EXIT_CODE,
        REF_SNAPSHOT_UNRECORDED_EXIT_CODE,
    }
    assert PREFLIGHT_REFUSED_EXIT_CODE not in taken
    assert max(taken) + 1 == PREFLIGHT_REFUSED_EXIT_CODE
    sweep = (_REPO_ROOT / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$PreflightRefusedExitCode\s*=\s*(\d+)\s*$", sweep, re.MULTILINE)
    assert m is not None and int(m.group(1)) == PREFLIGHT_REFUSED_EXIT_CODE
    assert f"^{PREFLIGHT_REFUSED_PAYLOAD_PREFIX.strip()}" in sweep


# --------------------------------------------------------------------------- #
# D-2' inventory finding — daemon configuration errors end before spawn, too
# --------------------------------------------------------------------------- #


def test_config_systemexit_message_becomes_a_config_refusal() -> None:
    with pytest.raises(PreflightError) as ei, loop_runner._config_refusal():
        raise SystemExit("conductor.roster is empty: set [conductor].roster")
    assert ei.value.check == "config"
    assert ei.value.detail.startswith("conductor.roster is empty")


def test_config_refusal_passes_int_exits_preflight_errors_and_other_exceptions() -> None:
    with pytest.raises(SystemExit) as ei, loop_runner._config_refusal():
        raise SystemExit(4)
    assert not isinstance(ei.value, PreflightError) and ei.value.code == 4
    with pytest.raises(PreflightError) as pe, loop_runner._config_refusal():
        raise PreflightError("preflight P0 failed: x", check="P0")
    assert pe.value.check == "P0"
    with pytest.raises(RuntimeError), loop_runner._config_refusal():
        raise RuntimeError("unknown stays generic (exit 1 -> retry/quarantine)")


def test_invalid_budget_is_a_config_refusal_before_anything_runs() -> None:
    settings = MindwireSettings()
    settings.conductor.run_budget_s = 0
    with pytest.raises(PreflightError) as ei:
        asyncio.run(loop_runner.run_conductor(settings, mcp=object()))  # type: ignore[arg-type]
    assert ei.value.check == "config"


def test_unloadable_settings_exit_13_in_conductor_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken() -> MindwireSettings:
        raise ValueError("bad toml")

    monkeypatch.setattr(loop_runner, "load_settings", _broken)
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == PREFLIGHT_REFUSED_EXIT_CODE
    rows = [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith(PREFLIGHT_REFUSED_PAYLOAD_PREFIX)
    ]
    assert len(rows) == 1
    payload = json.loads(rows[0][len(PREFLIGHT_REFUSED_PAYLOAD_PREFIX) :])
    assert payload["check"] == "config" and "bad toml" in payload["detail"]
