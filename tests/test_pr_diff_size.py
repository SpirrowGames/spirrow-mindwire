"""``mindwire pr-diff-size`` (T-fix-now-vs-followup-is-mechanical, Bohr msg-5241 §4).

Pins: the boundary (threshold → fix-now, threshold+1 → split), that the constants are the
gate's own, that a stale PR-metadata head never decides which commit is measured, that an
unpushed SHA fails loud without a ``decision=`` line, and that the gate's own
``fetch_pr_diff`` still measures the same bytes after the split into two methods.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from spirrow_mindwire import cli
from spirrow_mindwire.github.client import GitHubClient, PrRef
from spirrow_mindwire.naysayer import pr_diff_size, pr_review

_REPO = "spirrowgames/spirrow-mindwire"
_PR = PrRef(owner="spirrowgames", repo="spirrow-mindwire", number=7)
_STALE_HEAD = "0000000stale"
_FRESH_HEAD = "1111111fresh"


def _handler(
    *,
    diffs: dict[str, str],
    calls: list[str] | None = None,
    meta_head: str = _STALE_HEAD,
) -> Any:
    """PR metadata reports ``meta_head`` (possibly stale); compare serves ``diffs[head]``."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append(path)
        if path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(200, json={"base": {"ref": "main"}, "head": {"sha": meta_head}})
        if "/compare/" in path:
            head = path.rsplit("...", 1)[1]
            if head not in diffs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, text=diffs[head])
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    return handler


def _client(handler: Any) -> GitHubClient:
    return GitHubClient("tok", transport=httpx.MockTransport(handler))


def _run(
    handler: Any, head: str | None, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = pr_diff_size.run(_REPO, _PR.number, head, client=_client(handler))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_constants_are_the_gates_not_copies() -> None:
    size = pr_diff_size.DiffSize("main", "x", 1, pr_review._DIFF_WARN_THRESHOLD, 0)
    assert size.warn_threshold is pr_review._DIFF_WARN_THRESHOLD
    assert pr_diff_size.decide(pr_review._DIFF_WARN_THRESHOLD) == "fix-now"


def test_boundary_at_threshold_is_fix_now_and_one_over_is_split() -> None:
    # DECIDED msg-5233 rule 2 says "超える" — strictly greater. The gate's in_headroom is `>=`,
    # so at exactly the threshold the gate already warns but the rule is still fix-now.
    assert pr_review._DIFF_WARN_THRESHOLD == 120_000  # the number msg-5241 fixes the tests at
    assert pr_diff_size.decide(120_000) == "fix-now"
    assert pr_diff_size.decide(120_001) == "split"


def test_cli_exit_codes_follow_the_decision(capsys: pytest.CaptureFixture[str]) -> None:
    t = pr_review._DIFF_WARN_THRESHOLD
    handler = _handler(diffs={"at": "x" * t, "over": "x" * (t + 1)})
    code, out, _err = _run(handler, "at", capsys)
    assert code == pr_diff_size.EXIT_FIX_NOW == 0
    assert f"original_chars={t} " in out and "decision=fix-now" in out
    assert f"warn_threshold={t} limit={pr_review._MAX_DIFF_CHARS} " in out
    code, out, _err = _run(handler, "over", capsys)
    assert code == pr_diff_size.EXIT_SPLIT == 3
    assert "decision=split" in out


def test_stale_pr_metadata_head_is_ignored_for_the_explicit_sha(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Right after a push the PR metadata can still report the pre-fix head (msg-5239 §1). The
    # helper must compare against the SHA it was given, not the metadata's head.sha.
    calls: list[str] = []
    t = pr_review._DIFF_WARN_THRESHOLD
    handler = _handler(
        diffs={_STALE_HEAD: "small", _FRESH_HEAD: "x" * (t + 1)},
        calls=calls,
        meta_head=_STALE_HEAD,
    )
    code, out, _err = _run(handler, _FRESH_HEAD, capsys)
    assert code == pr_diff_size.EXIT_SPLIT
    assert f"head={_FRESH_HEAD} " in out
    compare_paths = [p for p in calls if "/compare/" in p]
    assert compare_paths == [f"/repos/{_REPO}/compare/main...{_FRESH_HEAD}"]


def test_unpushed_sha_fails_loud_without_a_decision(capsys: pytest.CaptureFixture[str]) -> None:
    handler = _handler(diffs={_STALE_HEAD: "small"})
    code, out, err = _run(handler, "deadbeefnotpushed", capsys)
    assert code == pr_diff_size.EXIT_ERROR
    assert code not in (pr_diff_size.EXIT_FIX_NOW, pr_diff_size.EXIT_SPLIT)
    assert "decision=" not in out
    assert "decision=" not in err


def test_unpushed_sha_error_names_the_cause(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(_handler(diffs={}), "deadbeef", capsys)
    assert code == pr_diff_size.EXIT_ERROR
    assert out == ""
    assert "is the commit pushed?" in err and "404" in err


def test_bad_repo_argument_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    code = pr_diff_size.run("no-slash", 1, "abc", client=_NeverCalled())
    captured = capsys.readouterr()
    assert code == pr_diff_size.EXIT_ERROR
    assert "decision=" not in captured.out
    assert "owner/repo" in captured.err


class _NeverCalled:
    async def fetch_pr_base_and_head(self, pr: PrRef) -> tuple[str, str]:
        raise AssertionError("must not be called")

    async def fetch_compare_diff(self, owner: str, repo: str, base_ref: str, head_sha: str) -> str:
        raise AssertionError("must not be called")


@pytest.mark.anyio
async def test_gate_fetch_pr_diff_measures_the_same_bytes_as_the_helper() -> None:
    # The gate still takes head.sha from metadata; for that head the helper and the gate read
    # the identical text, so len() — the gate's original_chars — agrees byte for byte.
    body = "diff --git a/x b/x\n+é\r\n"
    handler = _handler(diffs={_STALE_HEAD: body}, meta_head=_STALE_HEAD)
    async with _client(handler) as client:
        gate_diff = await client.fetch_pr_diff(_PR)
        size = await pr_diff_size.measure(client, _PR, _STALE_HEAD)
    assert gate_diff == body
    assert size.original_chars == len(gate_diff)
    assert size.original_chars == pr_review._make_diff_view(gate_diff).original_chars


def test_cli_parser_registers_pr_diff_size() -> None:
    args = cli._build_parser().parse_args(["pr-diff-size", "--repo", _REPO, "--pr", "5"])
    assert (args.command, args.repo, args.pr, args.head) == ("pr-diff-size", _REPO, 5, None)


# --- Default CLI paths (PR #396 gate, REQUEST_CHANGES: `untested` at pr_diff_size.py:114) ---
# The typical invocation `mindwire pr-diff-size --repo o/r --pr N` omits both `--head` (→
# local_head_sha) and the injected client (→ _measure_with_default_client). These tests run
# those two paths for real: a real `git` repository for the head, and the real GitHubClient
# class constructed by the default path (only its HTTP transport is swapped for a mock).


def _git(cwd: Any, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def git_head(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "c1")
    monkeypatch.chdir(tmp_path)
    return _git(tmp_path, "rev-parse", "HEAD")


def test_local_head_sha_is_git_rev_parse_head(git_head: str) -> None:
    assert pr_diff_size.local_head_sha() == git_head
    assert len(git_head) == 40


def test_omitted_head_measures_the_local_head_commit(
    git_head: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Metadata reports a stale head; the default must measure local HEAD, not metadata.
    calls: list[str] = []
    handler = _handler(diffs={git_head: "x" * 10}, calls=calls, meta_head=_STALE_HEAD)
    code, out, _err = _run(handler, None, capsys)
    assert code == pr_diff_size.EXIT_FIX_NOW
    assert f"head={git_head}" in out and "decision=fix-now" in out
    assert any(p.endswith(f"...{git_head}") for p in calls)
    assert not any(p.endswith(f"...{_STALE_HEAD}") for p in calls)


def test_omitted_head_outside_a_git_repo_fails_loud(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    code = pr_diff_size.run(_REPO, _PR.number, None, client=_NeverCalled())
    captured = capsys.readouterr()
    assert code == pr_diff_size.EXIT_ERROR
    assert "decision=" not in captured.out
    assert "pr-diff-size: error:" in captured.err
    # git's own stderr reaches the caller, not only the exit status.
    assert "not a git repository" in captured.err.lower()


def _patch_default_client(
    monkeypatch: pytest.MonkeyPatch, handler: Any, constructed: list[dict[str, Any]]
) -> None:
    """Make the default path's real GitHubClient talk to ``handler`` instead of the network."""
    from spirrow_mindwire.github import client as client_mod

    real = client_mod.GitHubClient

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append({"args": args, "kwargs": dict(kwargs)})
            kwargs.setdefault("token", "tok")
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client_mod, "GitHubClient", _Recording)


def test_omitted_client_uses_the_default_github_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    t = pr_review._DIFF_WARN_THRESHOLD
    constructed: list[dict[str, Any]] = []
    _patch_default_client(monkeypatch, _handler(diffs={_FRESH_HEAD: "x" * (t + 1)}), constructed)
    code = pr_diff_size.run(_REPO, _PR.number, _FRESH_HEAD)
    out = capsys.readouterr().out
    # Constructed exactly once, with no arguments: token and transport are the defaults.
    assert constructed == [{"args": (), "kwargs": {}}]
    assert code == pr_diff_size.EXIT_SPLIT
    assert f"original_chars={t + 1}" in out and "decision=split" in out


def test_omitted_client_http_failure_fails_loud(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    constructed: list[dict[str, Any]] = []
    _patch_default_client(monkeypatch, _handler(diffs={}), constructed)
    code = pr_diff_size.run(_REPO, _PR.number, "deadbeef")
    captured = capsys.readouterr()
    assert len(constructed) == 1
    assert code == pr_diff_size.EXIT_ERROR
    assert "decision=" not in captured.out
    assert "is the commit pushed?" in captured.err


def test_fully_default_invocation_local_head_and_default_client(
    git_head: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `mindwire pr-diff-size --repo o/r --pr N` end to end through the CLI entry point.
    constructed: list[dict[str, Any]] = []
    _patch_default_client(monkeypatch, _handler(diffs={git_head: "x" * 5}), constructed)
    args = cli._build_parser().parse_args(["pr-diff-size", "--repo", _REPO, "--pr", "7"])
    code = pr_diff_size.run(args.repo, args.pr, args.head)
    out = capsys.readouterr().out
    assert len(constructed) == 1
    assert code == pr_diff_size.EXIT_FIX_NOW
    assert f"head={git_head}" in out and "original_chars=5" in out
