"""Transient GitHub API failures in preflight P1 do not quarantine threads.

T-preflight-transient-gh-failure-quarantines-threads, design v3.2 (Bohr msg-9200 v1, msg-9202
v2, msg-9204 v3, msg-9206 v3.1, msg-9208 v3.2; go Einstein). Pinned here:

* D-1 — the transient / terminal classification of a failed ``gh api`` call.
* D-2 — transient-only retries, at most 4 tries, waits 2s -> 5s -> 10s; stderr carried, capped.
* D-2'' — ONE monotonic deadline checked before every call (first tries included) and every
  wait, each call's timeout cut to what is left; an unconfirmed endpoint at the deadline is
  deferred, never a pass.
* D-3 / D-2''' — the verdict: a CONFIRMED terminal problem (after provider aggregation) wins;
  otherwise anything unconfirmed is :class:`PreflightDeferredError`; otherwise pass. A ruleset
  provider whose fetch failed terminally (404) is a terminal PROVIDER, not a problem on its own.
* D-4 / D-5b — exit 11 with its payload row, the ``MINDWIRE_PREFLIGHT_OK`` row on a pass only,
  and the exit code pinned to the sweep wrapper.

Every I/O boundary is faked: a fake clock that the fake ``gh`` advances by each call's cost.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import loop_runner
from spirrow_mindwire import preflight as pf
from spirrow_mindwire.config import MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.preflight import (
    PREFLIGHT_DEFERRED_EXIT_CODE,
    PREFLIGHT_DEFERRED_PAYLOAD_PREFIX,
    PREFLIGHT_OK_PREFIX,
    PreflightDeferredError,
    PreflightError,
    classify_gh_failure,
    preflight_gate,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

OWNER = "SpirrowGames"
REPO = "spirrow-mindwire"
URL = f"https://github.com/{OWNER}/{REPO}.git"
OTHER = "spirrow-other"
OTHER_URL = f"https://github.com/{OWNER}/{OTHER}.git"

STDERR_404 = "gh: Not Found (HTTP 404)"
STDERR_502 = "gh: HTTP 502: Bad Gateway (https://api.github.com/repos/x)"
STDERR_SCHANNEL = (
    'Get "https://api.github.com/repos/SpirrowGames/spirrow-mindwire": '
    "schannel: failed to receive handshake, SSL/TLS connection failed"
)
STDERR_EOF = 'Get "https://api.github.com/repos/SpirrowGames/spirrow-mindwire": unexpected EOF'


def _ep(repo: str, tail: str = "") -> str:
    return f"repos/{OWNER}/{repo}{tail}"


def _active() -> dict[str, Any]:
    return {"enforcement": "active", "current_user_can_bypass": "never"}


def _evaluate() -> dict[str, Any]:
    return {"enforcement": "evaluate", "current_user_can_bypass": "never"}


def _bypassable() -> dict[str, Any]:
    return {"enforcement": "active", "current_user_can_bypass": "always"}


def _rules(providers: dict[str, list[int]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rtype, ids in providers.items():
        for rs_id in ids:
            out.append({"type": rtype, "ruleset_id": rs_id, "ruleset_source_type": "Organization"})
    return out


def _cpe(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["gh", "api"], output="", stderr=stderr)


Script = dict[str, list[tuple[float, Any]]]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeGh:
    """``script[endpoint]`` is a list of steps consumed one per call (the last one repeats).

    A step is ``(cost_s, result)``: the call takes ``cost_s`` of fake time. If ``cost_s`` is
    longer than the timeout the caller passed, the call advances by the timeout and raises
    :class:`subprocess.TimeoutExpired` (what ``subprocess.run(timeout=…)`` does). Otherwise
    ``result`` is returned, or raised when it is an exception. An endpoint with no script is a
    test bug (AssertionError), never silently a failure.
    """

    def __init__(self, clock: Clock, script: dict[str, list[tuple[float, Any]]]) -> None:
        self.clock = clock
        self.script = script
        self.calls: list[tuple[str, float]] = []

    def __call__(self, endpoint: str, timeout_s: float) -> Any:
        self.calls.append((endpoint, timeout_s))
        steps = self.script.get(endpoint)
        assert steps, f"unexpected call to {endpoint}"
        cost, result = steps.pop(0) if len(steps) > 1 else steps[0]
        if cost > timeout_s:
            self.clock.now += timeout_s
            raise subprocess.TimeoutExpired(["gh", "api", endpoint], timeout_s)
        self.clock.now += cost
        if isinstance(result, BaseException):
            raise result
        return result

    def endpoints(self) -> list[str]:
        return [e for e, _ in self.calls]


def _run(
    tmp_path: Path,
    script: dict[str, list[tuple[float, Any]]],
    *,
    remotes: tuple[str, ...] = (URL,),
    deadline_s: float = pf.PREFLIGHT_DEADLINE_S,
) -> tuple[FakeGh, Clock, list[float], list[str] | BaseException]:
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir(exist_ok=True)
    daemon.mkdir(exist_ok=True)
    clock = Clock()
    sleeps: list[float] = []

    def _sleep(s: float) -> None:
        sleeps.append(s)
        clock.now += s

    gh = FakeGh(clock, script)
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    result: list[str] | BaseException
    try:
        result = preflight_gate(
            target,
            daemon_root=daemon,
            api_caller=gh,
            remote_reader=lambda _p: [(f"r{i}", u) for i, u in enumerate(remotes)],
            git_toplevel=lambda _p: None,
            hooks_path_reader=lambda _p: str(hooks),
            clock=clock,
            sleep=_sleep,
            deadline_s=deadline_s,
        )
    except (PreflightError, PreflightDeferredError) as exc:
        result = exc
    return gh, clock, sleeps, result


def _repo_steps(
    repo: str, rules: list[dict[str, Any]], cost: float = 1.0
) -> dict[str, list[tuple[float, Any]]]:
    return {
        _ep(repo): [(cost, {"default_branch": "main"})],
        _ep(repo, "/rules/branches/main"): [(cost, rules)],
    }


# --------------------------------------------------------------------------- #
# D-1 classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stderr",
    [
        STDERR_502,
        "gh: Service Unavailable (HTTP 503)",
        "gh: Server Error (HTTP 500)",
        "gh: Too Many Requests (HTTP 429)",
        "gh: API rate limit exceeded for user ID 1. (HTTP 403)",
        STDERR_SCHANNEL,
        STDERR_EOF,
        "net/http: TLS handshake timeout",
        "read tcp 10.0.0.1:1->1.2.3.4:443: wsarecv: connection reset by peer",
        "dial tcp 1.2.3.4:443: connectex: connection refused",
        "dial tcp: lookup api.github.com: i/o timeout",
        "dial tcp: lookup api.github.com: no such host",
    ],
)
def test_transient_stderr(stderr: str) -> None:
    assert classify_gh_failure(stderr) == "transient"


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Bad credentials (HTTP 401)",
        "gh: Resource not accessible by integration (HTTP 403)",
        STDERR_404,
        "gh: Validation Failed (HTTP 422)",
        # An HTTP status wins over a transport word in the same text: a 404 is a 404.
        "gh: Not Found (HTTP 404) after connection reset",
        "something nobody has seen before",
        "",
    ],
)
def test_terminal_stderr(stderr: str) -> None:
    assert classify_gh_failure(stderr) == "terminal"


def test_timeout_and_unknown_exceptions_are_classified(tmp_path: Path) -> None:
    # A gh that hangs past the per-call timeout is transient: retried, then deferred.
    hang = {_ep(REPO): [(10_000.0, {"default_branch": "main"})]}
    gh, _clock, sleeps, result = _run(tmp_path, hang)
    assert isinstance(result, PreflightDeferredError)
    assert gh.calls[0][1] == pf.PREFLIGHT_CALL_TIMEOUT_S
    assert "timed out" in result.last_stderr
    # Anything not recognised (here a plain RuntimeError) is terminal: exit 1, as before.
    gh, _clock, sleeps, result = _run(tmp_path, {_ep(REPO): [(1.0, RuntimeError("boom"))]})
    assert isinstance(result, PreflightError)
    assert len(gh.calls) == 1 and sleeps == []


# --------------------------------------------------------------------------- #
# D-2 retries
# --------------------------------------------------------------------------- #


def test_transient_failures_are_retried_then_pass(tmp_path: Path) -> None:
    script = _repo_steps(REPO, _rules({t: [1] for t in pf._REQUIRED_RULE_TYPES}))
    script[_ep(REPO)] = [
        (1.0, _cpe(STDERR_SCHANNEL)),
        (1.0, _cpe(STDERR_EOF)),
        (1.0, {"default_branch": "main"}),
    ]
    script[_ep(REPO, "/rulesets/1")] = [(1.0, _active())]
    gh, _clock, sleeps, result = _run(tmp_path, script)
    assert result == [f"{OWNER}/{REPO}"]
    assert sleeps == [2.0, 5.0]
    assert gh.endpoints().count(_ep(REPO)) == 3


def test_four_transient_failures_defer_with_payload(tmp_path: Path) -> None:
    script = {_ep(REPO): [(1.0, _cpe(STDERR_502))]}
    gh, _clock, sleeps, result = _run(tmp_path, script)
    assert isinstance(result, PreflightDeferredError)
    assert len(gh.calls) == pf.PREFLIGHT_MAX_ATTEMPTS == 4
    assert sleeps == [2.0, 5.0, 10.0]
    payload = result.payload()
    assert payload["owner"] == OWNER and payload["repo"] == REPO
    assert payload["endpoint"] == _ep(REPO)
    assert payload["attempts"] == 4
    assert payload["reason"] == "transient"
    assert "HTTP 502" in str(payload["last_stderr"])


def test_terminal_failure_is_not_retried(tmp_path: Path) -> None:
    gh, _clock, sleeps, result = _run(tmp_path, {_ep(REPO): [(1.0, _cpe(STDERR_404))]})
    assert isinstance(result, PreflightError)
    assert len(gh.calls) == 1 and sleeps == []
    assert "HTTP 404" in str(result)  # gh's stderr is carried into the message


def test_stderr_is_capped(tmp_path: Path) -> None:
    long = "gh: Not Found (HTTP 404) " + "x" * 3000
    _gh, _clock, _sleeps, result = _run(tmp_path, {_ep(REPO): [(1.0, _cpe(long))]})
    assert isinstance(result, PreflightError)
    assert "x" * pf.PREFLIGHT_STDERR_CAP not in str(result)
    assert "x" * 400 in str(result)


# --------------------------------------------------------------------------- #
# D-2'' one shared deadline
# --------------------------------------------------------------------------- #


def test_deadline_reached_by_successes_without_any_retry(tmp_path: Path) -> None:
    """Einstein msg-9203: every call succeeds in 29s, no retry ever happens — still bounded."""
    rules = _rules({"deletion": [1], "non_fast_forward": [2], "pull_request": [3]})
    script = _repo_steps(REPO, rules, cost=29.0)
    for rs in (1, 2, 3):
        script[_ep(REPO, f"/rulesets/{rs}")] = [(29.0, _active())]
    script.update(_repo_steps(OTHER, rules, cost=29.0))
    gh, clock, _sleeps, result = _run(tmp_path, script, remotes=(URL, OTHER_URL))
    assert isinstance(result, PreflightDeferredError)
    # 1-5 succeed (145s); the 6th is called with the 5s left, times out at 150s; no 7th call.
    assert len(gh.calls) == 6
    assert gh.calls[5] == (_ep(OTHER), 5.0)
    assert clock.now == 150.0
    assert result.reason == "deadline"
    assert _ep(OTHER) in result.unchecked


def test_deadline_before_a_new_endpoint_defers_without_calling_it(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1], "non_fast_forward": [2], "pull_request": [3]})
    script = _repo_steps(REPO, rules, cost=29.0)
    script[_ep(REPO, "/rulesets/1")] = [(29.0, _active())]
    gh, clock, _sleeps, result = _run(tmp_path, script, deadline_s=87.0)
    assert clock.now == 87.0
    assert isinstance(result, PreflightDeferredError)
    assert _ep(REPO, "/rulesets/2") not in gh.endpoints()
    assert _ep(REPO, "/rulesets/2") in result.unchecked
    assert _ep(REPO, "/rulesets/3") in result.unchecked
    assert result.checked == [
        _ep(REPO),
        _ep(REPO, "/rules/branches/main"),
        _ep(REPO, "/rulesets/1"),
    ]


def test_deadline_checked_before_a_wait(tmp_path: Path) -> None:
    rules = _rules({t: [1] for t in pf._REQUIRED_RULE_TYPES})
    script = _repo_steps(REPO, rules, cost=29.0)
    script[_ep(REPO, "/rulesets/1")] = [
        (30.0, _cpe(STDERR_502)),
        (30.0, _cpe(STDERR_502)),
        (17.0, _cpe(STDERR_502)),
    ]
    _gh, clock, sleeps, result = _run(tmp_path, script)
    # 29+29 -> 58, +30 -> 88, wait 2 -> 90, +30 -> 120, wait 5 -> 125, +17 -> 142: 8s left, the
    # next wait is 10s, so it is not taken.
    assert clock.now == 142.0
    assert sleeps == [2.0, 5.0]
    assert isinstance(result, PreflightDeferredError)
    assert result.reason == "deadline"


def test_call_timeout_is_cut_to_the_time_left(tmp_path: Path) -> None:
    script = _repo_steps(REPO, [], cost=29.0)
    gh, _clock, _sleeps, _result = _run(tmp_path, script, deadline_s=40.0)
    assert gh.calls[0] == (_ep(REPO), 30.0)
    assert gh.calls[1] == (_ep(REPO, "/rules/branches/main"), 11.0)


# --------------------------------------------------------------------------- #
# D-3 / D-2''' aggregation — the same verdict at the deadline and at completion
# --------------------------------------------------------------------------- #


def test_3a_every_provider_terminal_is_confirmed_even_with_a_later_endpoint_unread(
    tmp_path: Path,
) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [3], "pull_request": [3]})
    script = _repo_steps(REPO, rules, cost=10.0)
    script[_ep(REPO, "/rulesets/1")] = [(10.0, _evaluate())]
    script[_ep(REPO, "/rulesets/2")] = [(30.0, _bypassable())]
    gh, clock, _sleeps, result = _run(tmp_path, script, deadline_s=60.0)
    assert clock.now == 60.0
    assert _ep(REPO, "/rulesets/3") not in gh.endpoints()
    assert isinstance(result, PreflightError)
    assert "'deletion'" in str(result)


def test_3b_evaluate_provider_with_an_unread_alternative_defers(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [3], "pull_request": [3]})
    script = _repo_steps(REPO, rules, cost=10.0)
    script[_ep(REPO, "/rulesets/1")] = [(40.0, _evaluate())]
    gh, _clock, _sleeps, result = _run(tmp_path, script, deadline_s=60.0)
    assert isinstance(result, PreflightDeferredError)
    assert _ep(REPO, "/rulesets/2") in result.unchecked
    assert _ep(REPO, "/rulesets/2") not in gh.endpoints()


def test_3c_alternative_exhausting_transient_retries_defers_the_same_way(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [3], "pull_request": [3]})
    script = _repo_steps(REPO, rules)
    script[_ep(REPO, "/rulesets/1")] = [(1.0, _evaluate())]
    script[_ep(REPO, "/rulesets/2")] = [(1.0, _cpe(STDERR_502))]
    script[_ep(REPO, "/rulesets/3")] = [(1.0, _active())]
    gh, _clock, _sleeps, result = _run(tmp_path, script)
    assert isinstance(result, PreflightDeferredError)
    assert result.reason == "transient"
    assert gh.endpoints().count(_ep(REPO, "/rulesets/2")) == 4
    assert result.endpoint == _ep(REPO, "/rulesets/2")


def test_3d_evaluate_plus_ok_alternative_passes(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [2], "pull_request": [2]})
    script = _repo_steps(REPO, rules)
    script[_ep(REPO, "/rulesets/1")] = [(1.0, _evaluate())]
    script[_ep(REPO, "/rulesets/2")] = [(1.0, _active())]
    _gh, _clock, _sleeps, result = _run(tmp_path, script)
    assert result == [f"{OWNER}/{REPO}"]


def test_3e_provider_404_with_ok_alternative_passes(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [2], "pull_request": [2]})
    script = _repo_steps(REPO, rules)
    script[_ep(REPO, "/rulesets/1")] = [(1.0, _cpe(STDERR_404))]
    script[_ep(REPO, "/rulesets/2")] = [(1.0, _active())]
    _gh, _clock, sleeps, result = _run(tmp_path, script)
    assert result == [f"{OWNER}/{REPO}"]
    assert sleeps == []  # the 404 was not retried


def test_3f_provider_404_with_unread_alternative_defers(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [3], "pull_request": [3]})
    script = _repo_steps(REPO, rules, cost=10.0)
    script[_ep(REPO, "/rulesets/1")] = [(40.0, _cpe(STDERR_404))]
    _gh, _clock, _sleeps, result = _run(tmp_path, script, deadline_s=60.0)
    assert isinstance(result, PreflightDeferredError)
    assert _ep(REPO, "/rulesets/2") in result.unchecked


def test_3g_provider_404_and_evaluate_alternative_is_confirmed(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1, 2], "non_fast_forward": [2], "pull_request": [2]})
    script = _repo_steps(REPO, rules)
    script[_ep(REPO, "/rulesets/1")] = [(1.0, _cpe(STDERR_404))]
    script[_ep(REPO, "/rulesets/2")] = [(1.0, _evaluate())]
    _gh, _clock, _sleeps, result = _run(tmp_path, script)
    assert isinstance(result, PreflightError)
    assert "HTTP 404" in str(result)
    assert "enforcement='evaluate'" in str(result)


def test_3h_rules_endpoint_404_is_confirmed_without_any_ruleset_call(tmp_path: Path) -> None:
    script: Script = {
        _ep(REPO): [(1.0, {"default_branch": "main"})],
        _ep(REPO, "/rules/branches/main"): [(1.0, _cpe(STDERR_404))],
    }
    gh, _clock, _sleeps, result = _run(tmp_path, script)
    assert isinstance(result, PreflightError)
    assert not [e for e in gh.endpoints() if "/rulesets/" in e]


def test_confirmed_problem_in_any_repo_beats_an_unconfirmed_one(tmp_path: Path) -> None:
    script: Script = {_ep(REPO): [(1.0, _cpe(STDERR_502))]}
    script.update(
        {
            _ep(OTHER): [(1.0, {"default_branch": "main"})],
            _ep(OTHER, "/rules/branches/main"): [(1.0, [])],  # no required rule at all
        }
    )
    _gh, _clock, _sleeps, result = _run(tmp_path, script, remotes=(URL, OTHER_URL))
    assert isinstance(result, PreflightError)
    assert OTHER in str(result)


def test_confirmed_problem_in_one_type_beats_an_unconfirmed_type(tmp_path: Path) -> None:
    rules = _rules({"deletion": [1], "non_fast_forward": [2], "pull_request": [2]})
    script = _repo_steps(REPO, rules)
    script[_ep(REPO, "/rulesets/1")] = [(1.0, _cpe(STDERR_SCHANNEL))]
    script[_ep(REPO, "/rulesets/2")] = [(1.0, _bypassable())]
    _gh, _clock, _sleeps, result = _run(tmp_path, script)
    assert isinstance(result, PreflightError)
    assert not isinstance(result, PreflightDeferredError)


def test_deferred_error_is_not_a_system_exit() -> None:
    assert not issubclass(PreflightDeferredError, SystemExit)
    assert not issubclass(PreflightDeferredError, PreflightError)


# --------------------------------------------------------------------------- #
# Default gh caller passes the timeout through
# --------------------------------------------------------------------------- #


def test_default_api_caller_passes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kw)
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout='{"default_branch": "main"}', stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert pf._default_api_caller("repos/x/y", 12.5) == {"default_branch": "main"}
    assert seen["timeout"] == 12.5
    assert seen["args"] == ["gh", "api", "repos/x/y"]


# --------------------------------------------------------------------------- #
# D-4 / D-5b — exit 11, payload row, OK row
# --------------------------------------------------------------------------- #


def _deferred() -> PreflightDeferredError:
    return PreflightDeferredError(
        owner=OWNER,
        repo=REPO,
        endpoint=_ep(REPO, "/rules/branches/main"),
        attempts=4,
        last_stderr=STDERR_SCHANNEL,
        reason="transient",
        checked=[_ep(REPO)],
        unchecked=[_ep(REPO, "/rules/branches/main")],
    )


def test_main_maps_preflight_deferral_to_exit_11(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _fake_run_conductor(_settings: MindwireSettings, **_kw: object) -> None:
        raise _deferred()

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == PREFLIGHT_DEFERRED_EXIT_CODE == 11
    out = capsys.readouterr().out
    assert "reason=adapter_error" not in out
    assert PREFLIGHT_OK_PREFIX.strip() not in out
    rows = [ln for ln in out.splitlines() if ln.startswith(PREFLIGHT_DEFERRED_PAYLOAD_PREFIX)]
    assert len(rows) == 1
    payload = json.loads(rows[0][len(PREFLIGHT_DEFERRED_PAYLOAD_PREFIX) :])
    assert payload["owner"] == OWNER and payload["repo"] == REPO
    assert payload["attempts"] == 4 and payload["reason"] == "transient"
    assert payload["unchecked"] == [_ep(REPO, "/rules/branches/main")]


def _cfg(tmp_path: Path) -> Stage3LoopConfig:
    return Stage3LoopConfig(repo_dir=tmp_path)


def test_ok_row_printed_only_when_preflight_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(loop_runner, "preflight_gate", lambda _p: [f"{OWNER}/{REPO}"])
    loop_runner._preflight(_cfg(tmp_path))
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.startswith(PREFLIGHT_OK_PREFIX)]
    assert len(rows) == 1
    assert json.loads(rows[0][len(PREFLIGHT_OK_PREFIX) :]) == {"repos": [f"{OWNER}/{REPO}"]}


@pytest.mark.parametrize(
    "exc", [_deferred(), PreflightError("preflight P1 failed: x"), RuntimeError("boom")]
)
def test_no_ok_row_when_preflight_did_not_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: BaseException,
) -> None:
    def _raise(_p: Path) -> list[str]:
        raise exc

    monkeypatch.setattr(loop_runner, "preflight_gate", _raise)
    expected: type[BaseException] = (
        PreflightDeferredError if isinstance(exc, PreflightDeferredError) else PreflightError
    )
    with pytest.raises(expected):
        loop_runner._preflight(_cfg(tmp_path))
    assert PREFLIGHT_OK_PREFIX.strip() not in capsys.readouterr().out


def test_exit_code_is_distinct_and_pinned_to_the_wrapper() -> None:
    from spirrow_mindwire.clone_guard import DIRTY_CLONE_EXIT_CODE
    from spirrow_mindwire.conductor import run_budget, stall, stand_down
    from spirrow_mindwire.git_sync import GIT_SYNC_DEFERRED_EXIT_CODE
    from spirrow_mindwire.turn_isolation.errors import SECRETS_STOP_EXIT_CODE

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
    }
    assert PREFLIGHT_DEFERRED_EXIT_CODE not in taken
    assert max(taken) + 1 == PREFLIGHT_DEFERRED_EXIT_CODE
    sweep = (_REPO_ROOT / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$PreflightDeferredExitCode\s*=\s*(\d+)\s*$", sweep, re.MULTILINE)
    assert m is not None and int(m.group(1)) == PREFLIGHT_DEFERRED_EXIT_CODE
    assert f"^{PREFLIGHT_DEFERRED_PAYLOAD_PREFIX.strip()}" in sweep
    assert f"^{PREFLIGHT_OK_PREFIX.strip()}" in sweep
