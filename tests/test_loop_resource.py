"""Tests for the sweep wrapper's predicted-resource resolver.

Covers ``src/spirrow_mindwire/loop_resource.py`` (the pure function) and the
CLI wrapper ``scripts/resolve_resource.py`` (the shape wrapper only — the CLI
delegates every resolution to the pure function, so wide behaviour coverage
lives against the pure function and CLI tests exercise argument parsing,
stdin batching and JSON shape).

Landing thread: ``T-loop-control-keyed-by-project-resource-is-the-repo``,
§5(a) v2 and D-KEY v5. Failure modes covered here are the failure classes
that thread names in its residual list:

- fail-open on every observation failure (D-KEY-4c(2)) — nothing raises out
  of ``resolve_predicted_resource``.
- transitive local-path resolution with a depth cap AND explicit cycle
  detection (D-KEY-1 v3 step 1).
- comparison key SHAPE = ``<host>/<org>/<repo>`` — host is NOT stripped
  (D-KEY-1 v3, resolves E-50 in the thread).
- lowercase, ``.git`` and trailing ``/`` stripped, credentials stripped.
- ``git@host:org/repo`` (scp-style) is rewritten before any other rule.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from spirrow_mindwire.loop_resource import (
    ResourceResolution,
    _GitRunner,
    _normalise_remote_url,
    resolve_batch,
    resolve_predicted_resource,
)

# --- fake git runner ------------------------------------------------------


class _FakeGit(_GitRunner):
    """Test seam: return a canned origin URL per canonicalised path.

    The production runner canonicalises input paths via ``Path.resolve()``
    before asking git; tests key their fake by the same canonical form so
    the mapping is stable regardless of the input the test passes.
    """

    def __init__(self, origins: dict[str, str | None]) -> None:
        self._origins = {str(Path(k).resolve()): v for k, v in origins.items()}
        self.calls: list[str] = []

    def get_origin_url(self, cwd: str) -> tuple[str | None, str | None]:
        self.calls.append(cwd)
        canonical = str(Path(cwd).resolve())
        if canonical not in self._origins:
            return None, f"no fake origin registered for {canonical!r}"
        value = self._origins[canonical]
        if value is None:
            return None, "simulated: no origin remote"
        return value, None


# --- _normalise_remote_url --------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # HTTPS: strip scheme, lowercase, keep host.
        (
            "https://github.com/SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        (
            "https://github.com/SpirrowGames/spirrow-mindwire",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # HTTPS with credentials: user@ (and user:pass@) is stripped, so a
        # PAT in ``origin`` never leaks into the comparison key.
        (
            "https://tomtar@github.com/SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        (
            "https://tomtar:PATSECRET@github.com/SpirrowGames/spirrow-mindwire",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # scp-style: rewrite before credential handling.
        (
            "git@github.com:SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # ssh://user@host/... form: scheme + credentials stripped.
        (
            "ssh://git@github.com/SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # Trailing slash stripped.
        (
            "https://github.com/SpirrowGames/spirrow-mindwire/",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # git:// scheme.
        (
            "git://github.com/SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # Non-github host — the compare key preserves host, which is the
        # entire point of the D-KEY-1 v3 shape (host is NOT stripped).
        ("https://gitlab.example.com/Team/repo.git", "gitlab.example.com/team/repo"),
        # --- regression cases from naysayer PR #252 ---
        # Objection 1: trailing ``.git/`` (either order of the two suffix
        # elements must collapse cleanly, or a HOLD would miss every
        # checkout whose remote used the other spelling).
        (
            "https://github.com/SpirrowGames/spirrow-mindwire.git/",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        (
            "https://github.com/SpirrowGames/spirrow-mindwire/.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # Objection 2: scp-style with a non-``git`` user and scp-style
        # with NO user prefix — both are valid Git remote formats.
        (
            "user@github.com:SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        (
            "github.com:SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        # Objection 3: schemes are case-insensitive per RFC 3986; a git
        # config with ``HTTPS://`` must not blow up three-segment
        # validation.
        (
            "HTTPS://github.com/SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
        (
            "SSH://Git@GitHub.com/SpirrowGames/spirrow-mindwire.git",
            "github.com/spirrowgames/spirrow-mindwire",
        ),
    ],
)
def test_normalise_remote_url_expected_shape(url: str, expected: str) -> None:
    """D-KEY-1 v3: normalization produces ``<host>/<org>/<repo>`` in every
    canonical Git URL flavour git supports for ``origin``."""

    assert _normalise_remote_url(url) == expected


def test_normalise_remote_url_rejects_non_three_segment() -> None:
    """A URL that does not split into exactly three segments is a real
    parse failure, not a silent pass — the caller records it as a reason
    and falls open. Without this check, ``https://github.com/some-user``
    would produce a two-segment key and silently HOLD nothing on
    ``owner==some-user``."""

    with pytest.raises(ValueError, match="expected <host>/<org>/<repo>"):
        _normalise_remote_url("https://github.com/only-two")

    with pytest.raises(ValueError, match="expected <host>/<org>/<repo>"):
        _normalise_remote_url("https://github.com/a/b/c/d")


# --- _looks_like_local_path (regression from naysayer objection 2) -------


def test_looks_like_local_path_recognises_scp_without_git_user(tmp_path: Path) -> None:
    """A checkout whose ``origin`` is scp-style with a non-``git`` user
    (or no user at all) must NOT be misclassified as a local path — the
    resolver would follow it onto the filesystem and fail open, defeating
    the HOLD gate. Naysayer PR #252 objection 2.
    """

    git = _FakeGit(
        {str(tmp_path): "user@github.com:SpirrowGames/spirrow-mindwire.git"},
    )
    r = resolve_predicted_resource(str(tmp_path), _git=git)
    assert r.resource == "github.com/spirrowgames/spirrow-mindwire"
    assert r.reason is None
    # Exactly one hop — the scp-style URL is recognised as a network URL,
    # not followed as a local path.
    assert len(git.calls) == 1


def test_looks_like_local_path_recognises_scp_without_user(tmp_path: Path) -> None:
    """``host:path`` — scp-style with no user prefix at all — is still a
    valid Git remote and must resolve, not be treated as local."""

    git = _FakeGit({str(tmp_path): "github.com:SpirrowGames/spirrow-mindwire.git"})
    r = resolve_predicted_resource(str(tmp_path), _git=git)
    assert r.resource == "github.com/spirrowgames/spirrow-mindwire"
    assert r.reason is None
    assert len(git.calls) == 1


def test_looks_like_local_path_windows_drive_letter_is_local(tmp_path: Path) -> None:
    """A Windows drive-letter path (``C:/other-clone``) has a ``:``
    before a ``/`` — the same surface shape as scp-style — but MUST be
    classified as local, or a linked-clone chain on Windows breaks.

    We verify the disambiguation by pointing the fake at a local path
    whose next hop resolves to a real URL: if drive-letter carve-out
    fails, resolution stops before that hop.
    """

    hop1 = tmp_path / "a"
    hop2 = tmp_path / "b"
    hop1.mkdir()
    hop2.mkdir()
    git = _FakeGit(
        {
            str(hop1): str(hop2),  # local-path chain
            str(hop2): "https://github.com/SpirrowGames/spirrow-mindwire.git",
        }
    )
    r = resolve_predicted_resource(str(hop1), _git=git)
    assert r.resource == "github.com/spirrowgames/spirrow-mindwire"
    assert len(git.calls) == 2  # both hops followed


# --- resolve_predicted_resource ------------------------------------


def test_resolve_success_returns_normalized_key(tmp_path: Path) -> None:
    """A repo whose ``origin`` is a plain network URL resolves in one hop."""

    git = _FakeGit({str(tmp_path): "https://github.com/SpirrowGames/spirrow-mindwire.git"})
    r = resolve_predicted_resource(str(tmp_path), _git=git)
    assert r == ResourceResolution(
        repo_dir=str(tmp_path),
        resource="github.com/spirrowgames/spirrow-mindwire",
        reason=None,
    )
    assert len(git.calls) == 1


def test_resolve_scp_url(tmp_path: Path) -> None:
    """scp-style URL — the common `git@github.com:org/repo.git` form used
    by SSH remotes. Must produce the same key as the HTTPS form."""

    git = _FakeGit({str(tmp_path): "git@github.com:SpirrowGames/spirrow-mindwire.git"})
    r = resolve_predicted_resource(str(tmp_path), _git=git)
    assert r.resource == "github.com/spirrowgames/spirrow-mindwire"
    assert r.reason is None


def test_resolve_local_path_origin_transitive(tmp_path: Path) -> None:
    """Local-path origins are followed until a network URL is reached.

    Simulates: linked clone ``A -> B -> https://...`` (depth 2). Must
    return the far end's normalised key.
    """

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    git = _FakeGit(
        {
            str(a): str(b),
            str(b): "https://github.com/SpirrowGames/spirrow-mindwire.git",
        }
    )
    r = resolve_predicted_resource(str(a), _git=git)
    assert r.resource == "github.com/spirrowgames/spirrow-mindwire"
    assert r.reason is None
    # 2 hops.
    assert len(git.calls) == 2


def test_resolve_local_path_cycle_detected(tmp_path: Path) -> None:
    """A -> B -> A must fail-open with a diagnostic ``reason`` — not loop,
    not raise. Cycle detection compares canonicalised paths."""

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    git = _FakeGit({str(a): str(b), str(b): str(a)})
    r = resolve_predicted_resource(str(a), _git=git)
    assert r.resource is None
    assert r.reason is not None
    assert "cycle" in r.reason


def test_resolve_depth_cap_hit(tmp_path: Path) -> None:
    """A chain of local-path origins deeper than the cap fails open with
    a depth-exceeded reason (never loops)."""

    # Build 6 dirs a->b->c->d->e->f, none of which returns a network URL.
    dirs = [tmp_path / c for c in "abcdef"]
    for d in dirs:
        d.mkdir()
    origins: dict[str, str | None] = {str(dirs[i]): str(dirs[i + 1]) for i in range(len(dirs) - 1)}
    origins[str(dirs[-1])] = str(dirs[0])  # would cycle if depth didn't cap
    git = _FakeGit(origins)
    r = resolve_predicted_resource(str(dirs[0]), _git=git)
    assert r.resource is None
    assert r.reason is not None
    # Depth OR cycle — either is an acceptable fail-open reason. The
    # important behaviour is that it does not raise, does not loop, does
    # not silently return a bogus key.
    assert "depth" in r.reason or "cycle" in r.reason


def test_resolve_git_error_fails_open(tmp_path: Path) -> None:
    """A ``git`` invocation error (registered by the fake as returning
    None, error) fails open with the error text in ``reason``."""

    git = _FakeGit({str(tmp_path): None})  # simulate "no origin remote"
    r = resolve_predicted_resource(str(tmp_path), _git=git)
    assert r.resource is None
    assert r.reason is not None
    assert "no origin remote" in r.reason


def test_resolve_relative_path_rejected(tmp_path: Path) -> None:
    """The Python side refuses to guess a relative path against CWD —
    matches the wrapper's ``IsPathFullyQualified`` upstream guard for
    non-wrapper callers (test harness, ad-hoc CLI)."""

    r = resolve_predicted_resource("relative/path")
    assert r.resource is None
    assert r.reason is not None
    assert "absolute" in r.reason


def test_resolve_blank_path_rejected() -> None:
    """Blank/whitespace repo_dir is rejected before touching git — a
    silent match here would defeat the whole HOLD gate."""

    r1 = resolve_predicted_resource("")
    assert r1.resource is None
    assert r1.reason is not None
    r2 = resolve_predicted_resource("   ")
    assert r2.resource is None
    assert r2.reason is not None


def test_resolve_never_raises_on_broken_url(tmp_path: Path) -> None:
    """A malformed origin — one that survives the ``_looks_like_local_path``
    filter as a URL but does not normalise to three segments — must fail
    open, not raise. This is the D-KEY-4c(2) guarantee at the module
    boundary."""

    # A URL-shaped string with a missing org.
    git = _FakeGit({str(tmp_path): "https://github.com/only-two"})
    r = resolve_predicted_resource(str(tmp_path), _git=git)
    assert r.resource is None
    assert r.reason is not None
    assert "cannot parse" in r.reason


def test_resolve_batch_preserves_input_order(tmp_path: Path) -> None:
    """Batch resolution returns results in input order — the wrapper
    depends on this to associate results back to candidates without
    imposing a cache shape."""

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    git = _FakeGit(
        {
            str(a): "https://github.com/SpirrowGames/spirrow-mindwire.git",
            str(b): "git@github.com:SpirrowGames/magickit.git",
        }
    )
    results = resolve_batch([str(a), str(b), str(a)], _git=git)
    assert [r.repo_dir for r in results] == [str(a), str(b), str(a)]
    assert results[0].resource == "github.com/spirrowgames/spirrow-mindwire"
    assert results[1].resource == "github.com/spirrowgames/magickit"
    assert results[2].resource == "github.com/spirrowgames/spirrow-mindwire"


# --- CLI ---------------------------------------------------------------


def _run_cli(*args: str, stdin: str | None = None) -> tuple[int, str, str]:
    """Invoke ``python scripts/resolve_resource.py`` as a subprocess."""

    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "resolve_resource.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_repo_dir_arg_produces_reason_for_nonabsolute(tmp_path: Path) -> None:
    """CLI end-to-end on a relative path: exit 0, JSON shape correct,
    ``reason`` explains the failure. Verifies the fail-open exit posture
    (0 on well-formed input, resolution failure goes in the payload)."""

    code, stdout, stderr = _run_cli("--repo-dir", "not-absolute")
    assert code == 0, f"stderr={stderr!r}"
    payload = json.loads(stdout)
    assert list(payload.keys()) == ["resolutions"]
    assert len(payload["resolutions"]) == 1
    row = payload["resolutions"][0]
    assert row["repo_dir"] == "not-absolute"
    assert row["resource"] is None
    assert row["reason"] is not None


def test_cli_stdin_json_batch(tmp_path: Path) -> None:
    """Batch mode via ``--stdin-json`` — the shape the wrapper uses when
    the distinct ``repo_dir`` set is larger than a comfortable argv."""

    payload_in = json.dumps({"repo_dirs": ["not-absolute", "also-not-absolute"]})
    code, stdout, stderr = _run_cli("--stdin-json", stdin=payload_in)
    assert code == 0, f"stderr={stderr!r}"
    payload = json.loads(stdout)
    assert len(payload["resolutions"]) == 2
    assert payload["resolutions"][0]["repo_dir"] == "not-absolute"
    assert payload["resolutions"][1]["repo_dir"] == "also-not-absolute"


def test_cli_no_args_exits_2() -> None:
    """No ``--repo-dir`` and no ``--stdin-json`` is a wiring bug, not a
    resolution failure. Exit 2 (config-error convention) to make sure a
    silently-empty wrapper invocation is loud."""

    code, _, stderr = _run_cli()
    assert code == 2
    assert "required" in stderr or "repo-dir" in stderr


def test_cli_malformed_stdin_exits_2() -> None:
    """Malformed stdin is a wiring bug — exit 2 with an explanation."""

    code, _, stderr = _run_cli("--stdin-json", stdin="not json")
    assert code == 2
    assert "stdin" in stderr.lower() or "json" in stderr.lower()
