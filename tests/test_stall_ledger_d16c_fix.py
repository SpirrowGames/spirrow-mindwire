"""D-16c-fix: a tick with no GitHub token fails loud, and the task launches a resolvable pwsh.

Spec: T-stalled-pr-has-no-detector msg-6415 (Bohr), Einstein's advisory on it (blank token),
measurements in msg-6414. The registered tick ran with no token, read GitHub unauthenticated
into the 60/h per-IP limit, and showed only ``unrecognized == examined``. The fix:

* Q2 -- no (or a blank) token: the github source makes NO request and reports
  ``auth_missing``; the heartbeat goes ``ingest_failure`` with the cause on the source line.
* Q1 -- the scheduled wrapper hands ``MINDWIRE_STALL_LEDGER_GITHUB_TOKEN`` to the child only,
  as ``MINDWIRE_GITHUB_TOKEN``.
* msg-6410 §2(b)3 -- ``Register-StallLedgerTask.ps1`` registers an absolute ``pwsh`` path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.github.client import (
    CheckRollup,
    OpenPr,
    OpenPrListing,
    PrRef,
    PrResolution,
    PrState,
    ReviewInfo,
)
from spirrow_mindwire.stall_ledger.adapters import (
    ADAPTER_FORMAT_VERSION,
    GitHubOpenPrAdapter,
    MotionEvent,
    MotionType,
    Unverifiable,
    github_credential_present,
)
from spirrow_mindwire.stall_ledger.heartbeat import FetchOutcome
from spirrow_mindwire.stall_ledger.model import Unit, UnitKind

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class _RecordingGitHub:
    """Answers like a healthy repo with one open PR and records every call it gets."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_open_prs(self, owner: str, repo: str) -> OpenPrListing:
        self.calls.append("list_open_prs")
        pr = OpenPr(ref=PrRef(owner, repo, 1), draft=False, head_sha="s1", created_at=T0)
        return OpenPrListing(prs=(pr,), examined=1, unrecognized=0)

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]:
        self.calls.append("fetch_pr_reviews_strict")
        return []

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        self.calls.append("fetch_pr_state")
        return PrState(ref=pr, resolution=PrResolution.OPEN, head_sha="s1", mergeable_state="clean")

    async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup:
        self.calls.append("fetch_check_rollup")
        return CheckRollup(head_sha="s1", head_committed_date=T0, head_pushed_at=T0, rows=())

    async def fetch_review_body(self, pr: PrRef, review_id: str) -> str | None:
        self.calls.append("fetch_review_body")
        return None


# ── the credential predicate (Einstein's advisory: blank must count as missing) ──────


@pytest.mark.parametrize(
    ("token", "present"),
    [(None, False), ("", False), ("   ", False), ("\t\n", False), ("github_pat_x", True)],
)
def test_github_credential_present_treats_blank_as_missing(
    token: str | None, present: bool
) -> None:
    assert github_credential_present(token) is present


# ── Q2: the github source fails as auth_missing without a single request ────────────


def test_no_credential_fails_auth_missing_and_makes_no_request() -> None:
    gh = _RecordingGitHub()
    adapter = GitHubOpenPrAdapter(gh, "o", "r", credential_present=False)  # type: ignore[arg-type]
    result = asyncio.run(adapter.fetch())
    assert gh.calls == []
    report = result.report
    assert report.fetch_outcome == FetchOutcome.AUTH_MISSING
    assert report.fetch_outcome.value == "auth_missing"
    assert (report.examined, report.recognized, report.unrecognized) == (0, 0, 0)
    assert result.complete is False
    # ingest_failure: the cause is on the source line, not inferred from counts.
    assert report.is_failure(ADAPTER_FORMAT_VERSION) is True


def test_no_credential_marker_check_makes_no_request() -> None:
    gh = _RecordingGitHub()
    adapter = GitHubOpenPrAdapter(gh, "o", "r", credential_present=False)  # type: ignore[arg-type]
    event = MotionEvent(type=MotionType.REVIEW, id="123", at=T0)
    check = asyncio.run(adapter.check_marker(Unit(UnitKind.PR, "o/r#1"), event))
    assert isinstance(check, Unverifiable)
    assert gh.calls == []


def test_with_credential_the_existing_path_runs() -> None:
    gh = _RecordingGitHub()
    adapter = GitHubOpenPrAdapter(gh, "o", "r", credential_present=True)  # type: ignore[arg-type]
    result = asyncio.run(adapter.fetch())
    assert gh.calls[0] == "list_open_prs"
    assert "fetch_check_rollup" in gh.calls
    assert result.report.fetch_outcome == FetchOutcome.OK
    assert (result.report.examined, result.report.recognized) == (1, 1)


def test_adapter_default_keeps_the_existing_contract() -> None:
    # Callers that do not say anything keep today's behaviour (the request is made).
    gh = _RecordingGitHub()
    adapter = GitHubOpenPrAdapter(gh, "o", "r")  # type: ignore[arg-type]
    asyncio.run(adapter.fetch())
    assert gh.calls[0] == "list_open_prs"


# ── the tick decides it at its entry point and says so on stderr ─────────────────────


def _load_cli() -> Any:
    path = REPO / "scripts" / "stall_ledger_tick.py"
    spec = importlib.util.spec_from_file_location("_d16c_fix_stall_ledger_tick", path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    return cli


def _run_cli_capturing_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> list[Any]:
    from spirrow_mindwire.github import client as gh_client

    for name in ("MINDWIRE_GITHUB_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cli = _load_cli()
    seen: dict[str, Any] = {}

    class FakeGitHub:
        def __init__(self, **_: Any) -> None:
            pass

        async def aclose(self) -> None:
            pass

    async def spy(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(gh_client, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(cli, "run_tick", spy)
    assert cli.main(["--data-dir", str(tmp_path), "--repo", "o/r"]) == 0
    return [a for a in seen["adapters"] if isinstance(a, GitHubOpenPrAdapter)]


@pytest.mark.parametrize("env", [{}, {"MINDWIRE_GITHUB_TOKEN": ""}, {"GITHUB_TOKEN": "  "}])
def test_cli_without_a_token_wires_auth_missing_and_says_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
) -> None:
    adapters = _run_cli_capturing_adapters(tmp_path, monkeypatch, env)
    assert [a._credential_present for a in adapters] == [False]
    err = capsys.readouterr().err
    assert "no GitHub token" in err
    assert "auth_missing" in err
    assert "MINDWIRE_STALL_LEDGER_GITHUB_TOKEN" in err


def test_cli_with_a_token_wires_the_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapters = _run_cli_capturing_adapters(
        tmp_path, monkeypatch, {"MINDWIRE_GITHUB_TOKEN": "github_pat_x"}
    )
    assert [a._credential_present for a in adapters] == [True]
    assert "no GitHub token" not in capsys.readouterr().err


# ── Q1: the wrapper hands the dedicated variable to the child only ───────────────────


def _ps1(name: str) -> str:
    return (REPO / "deploy" / name).read_text(encoding="utf-8")


def _code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_wrapper_passes_the_stall_ledger_token_as_mindwire_github_token() -> None:
    code = _code_lines(_ps1("run-stall-ledger-tick.ps1"))
    assert re.search(
        r"\$env:MINDWIRE_GITHUB_TOKEN\s*=\s*\$env:MINDWIRE_STALL_LEDGER_GITHUB_TOKEN", code
    )
    # An inherited identity is dropped first, so the ledger reads only under the token
    # chosen for it (or under none, which fails loud as auth_missing).
    assert re.search(r"Remove-Item\s+Env:MINDWIRE_GITHUB_TOKEN", code)
    assert re.search(r"Remove-Item\s+Env:GITHUB_TOKEN", code)
    drop = code.index("Remove-Item Env:MINDWIRE_GITHUB_TOKEN")
    assign = code.index("$env:MINDWIRE_GITHUB_TOKEN =")
    run = code.index("& uv")
    assert drop < assign < run
    # A blank value is not copied over (it would read as a token-shaped nothing).
    assert ".Trim()" in code
    # Never the naysayer identity.
    assert "NAYSAYER" not in code


def test_register_task_does_not_put_the_token_in_the_task() -> None:
    code = _code_lines(_ps1("Register-StallLedgerTask.ps1"))
    assert "GITHUB_TOKEN" not in code


# ── msg-6410 §2(b)3: no bare pwsh in the registered action ───────────────────────────


def test_register_task_resolves_an_absolute_pwsh() -> None:
    code = _code_lines(_ps1("Register-StallLedgerTask.ps1"))
    assert not re.search(r"-Execute\s+['\"]pwsh(\.exe)?['\"]", code)
    assert not re.search(r"Execute\s*=\s*['\"]pwsh(\.exe)?['\"]", code)
    assert re.search(r"Get-Command\s+pwsh\b[^\n]*-ErrorAction\s+Stop", code)
    assert re.search(r"IsPathRooted\(\$pwshPath\)", code)
    assert re.search(r"-Execute\s+\$pwshPath", code)
    # -DryRun shows the path that would be registered.
    assert re.search(r"Execute\s*=\s*\$pwshPath", code)
