"""Composition-root preflight — P0/P1/P2 (T-drop-branch-prediction-from-allowlist §3).

Called by :func:`spirrow_mindwire.loop_runner.run_loop` /
:func:`spirrow_mindwire.loop_runner.run_conductor` before the daemon spawns any
adapter session. On any failure this raises :class:`PreflightError`
(``SystemExit``) — the world's premise is broken (not this particular command),
so the right response is to halt the daemon, never to retry or degrade
silently. This is the asymmetric side of the design (msg-1265 §4, Bohr): a
prediction ("which branch would this push touch?") is genuinely hard, but a
precondition ("does this repo have the server-side protection I'm relying
on?") is 1 API call.

Three checks, in one order so the earliest failure is the loudest:

* **P0** — the implementer's ``repo_dir`` is not the daemon's own checkout.
  Two different clones. The safety story ("a chained ``git checkout main &&
  git reset --hard`` burns a throwaway clone, reflog restores") stops holding
  the moment the daemon runs against its own code.

* **P2** — every remote URL in ``repo_dir`` is under
  ``https://github.com/SpirrowGames/``. Tier-C decide 2026-08-19 msg-1270
  option β: network-boundary enforcement, not credential-scope enforcement.
  R-1 measured the loop's PAT is a classic ``gho_`` OAuth token with scope
  ``repo``, so the credential does not itself bound reach — the URL does.

* **P1** — the default branch of each remote's repo has the three server-side
  rules (``deletion`` / ``non_fast_forward`` / ``pull_request``) present, AND
  the ruleset(s) that provide them have ``current_user_can_bypass == "never"``
  from the loop identity. "Rules present" is not enough: if the loop can
  bypass, the rules do not protect it.

The three run in this order because P2 gates P1's *scope*: P1 asks GitHub
about specific repos, and asking about a repo P2 has already rejected only
adds a way for a bad-owner probe to fail slower. P0 comes first because it's
the cheapest and the one whose failure means "your daemon configuration is
broken, do not proceed", which subsumes both P1 and P2.

Test-injection: every I/O boundary (``git remote``, ``gh api``) is a
:class:`Callable` parameter with a subprocess-based default. Tests pass fakes;
the daemon uses the defaults.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SPIRROWGAMES_URL_PREFIX = "https://github.com/SpirrowGames/"

# Owner/repo extraction from an HTTPS remote URL. We accept the SpirrowGames
# prefix only (P2 has already vetoed anything else), and tolerate the trailing
# ``.git`` and/or trailing slash. A non-HTTPS URL (ssh, git://) does not match
# and P2 has already rejected it too.
_SPIRROWGAMES_REPO_RE = re.compile(
    r"^https://github\.com/(?P<owner>SpirrowGames)/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)

_REQUIRED_RULE_TYPES = frozenset({"deletion", "non_fast_forward", "pull_request"})


class PreflightError(SystemExit):
    """Halt the daemon: a composition-root precondition failed.

    Subclasses :class:`SystemExit` so an uncaught raise reaches the daemon's
    process boundary and is reported by the launcher (Task Scheduler / systemd
    log). The message text is the *reason* — that string is the only thing
    the operator sees before the daemon exits.
    """


ApiCaller = Callable[[str], Any]
"""``gh api <endpoint>`` → parsed JSON. Injectable for tests."""

RemoteReader = Callable[[Path], list[tuple[str, str]]]
"""``git -C repo remote -v`` → ``[(remote_name, url), ...]``. Injectable for tests."""


def preflight_gate(
    repo_dir: Path,
    *,
    daemon_root: Path | None = None,
    api_caller: ApiCaller | None = None,
    remote_reader: RemoteReader | None = None,
) -> None:
    """Enforce P0/P1/P2. Raise :class:`PreflightError` on any failure.

    ``daemon_root`` defaults to :func:`Path.cwd`. The composition root invokes
    this once per daemon start; there is no per-turn re-check on purpose, but
    there is no cache either — a subsequent daemon run pays the same 1 API
    call before it acts. Failure to reach the API is fail-closed (msg-1267 §4).
    """
    daemon_root = daemon_root or Path.cwd()
    api = api_caller or _default_api_caller
    remotes_of = remote_reader or _default_remote_reader

    _p0_daemon_checkout_separated(repo_dir, daemon_root)
    remotes = _p2_remote_urls_under_spirrowgames(repo_dir, remotes_of)
    _p1_default_branch_protected(remotes, api)
    logger.info(
        "preflight ok: repo_dir=%s daemon_root=%s remotes=%d",
        repo_dir,
        daemon_root,
        len(remotes),
    )


# --------------------------------------------------------------------------- #
# P0 — daemon checkout separation
# --------------------------------------------------------------------------- #


def _p0_daemon_checkout_separated(repo_dir: Path, daemon_root: Path) -> None:
    r = repo_dir.resolve()
    d = daemon_root.resolve()
    if r == d:
        raise PreflightError(
            f"preflight P0 failed: implementer's repo_dir ({r}) is the daemon's own "
            f"checkout ({d}). The captive-clone assumption (§7 of "
            f"T-drop-branch-prediction-from-allowlist) requires them to be distinct — a "
            f"chained `git checkout main && git reset --hard` in the loop's target repo "
            f"burns a throwaway clone (reflog restores), but the same in the daemon's "
            f"own checkout is a self-inflicted wound the loop cannot recover from. "
            f"Point [loop].repo_dir at a dedicated clone (the current production shape: "
            f"C:/workspace/sandbox/<project>-impl)."
        )


# --------------------------------------------------------------------------- #
# P2 — every remote URL under https://github.com/SpirrowGames/
# --------------------------------------------------------------------------- #


def _p2_remote_urls_under_spirrowgames(
    repo_dir: Path, remote_reader: RemoteReader
) -> list[tuple[str, str]]:
    try:
        remotes = remote_reader(repo_dir)
    except Exception as exc:
        raise PreflightError(
            f"preflight P2 failed: could not read remotes for {repo_dir}: {exc}. "
            f"P2-2 (msg-1270): unresolved destination → deny."
        ) from exc
    if not remotes:
        raise PreflightError(
            f"preflight P2 failed: {repo_dir} has no remotes configured. The GitHub org "
            f"ruleset can only protect a repo whose remote URL is under "
            f"{_SPIRROWGAMES_URL_PREFIX} — an empty remote list means nothing to check, "
            f"and P2-2 (msg-1270) forbids assuming."
        )
    offenders = [(n, u) for n, u in remotes if not u.startswith(_SPIRROWGAMES_URL_PREFIX)]
    if offenders:
        raise PreflightError(
            f"preflight P2 failed: {repo_dir} has remotes NOT under "
            f"{_SPIRROWGAMES_URL_PREFIX}: {offenders}. "
            f"(Tier-C decide 2026-08-19 msg-1270 option β: push-boundary check by URL, "
            f"not by credential scope; positive assertion (P2-1), not a deny-list.)"
        )
    return remotes


# --------------------------------------------------------------------------- #
# P1 — default-branch server-side protection with bypass=never
# --------------------------------------------------------------------------- #


def _p1_default_branch_protected(remotes: list[tuple[str, str]], api_caller: ApiCaller) -> None:
    # Deduplicate: several named remotes may point at the same repo (fetch/push
    # split), and asking GitHub 4x for the same rules is only wasted quota.
    unique_repos: dict[tuple[str, str], str] = {}
    for name, url in remotes:
        m = _SPIRROWGAMES_REPO_RE.match(url)
        if not m:
            # P2 vetted the SpirrowGames prefix, but if the tail did not parse
            # into an owner/repo shape we cannot ask GitHub about it — fail-closed.
            raise PreflightError(
                f"preflight P1 failed: remote {name!r} URL {url!r} passed the "
                f"SpirrowGames prefix check but did not parse into an owner/repo. "
                f"Cannot query server-side protection without an addressable repo."
            )
        unique_repos[(m["owner"], m["repo"])] = name

    for (owner, repo), name in unique_repos.items():
        default_branch = _fetch_default_branch(owner, repo, api_caller)
        rules = _fetch_effective_rules(owner, repo, default_branch, api_caller)

        present_types = {r.get("type") for r in rules if isinstance(r, dict)}
        missing = _REQUIRED_RULE_TYPES - present_types
        if missing:
            raise PreflightError(
                f"preflight P1 failed: {owner}/{repo}@{default_branch} (remote {name!r}) "
                f"is missing required rule types {sorted(missing)}. "
                f"GitHub server-side protection is what executes 'no push / force-push / "
                f"ref-deletion reaches main' after the local prediction machinery was "
                f"retired on 2026-08-19; without these rules the invariant does not hold."
            )

        ruleset_ids = {r["ruleset_id"] for r in rules if isinstance(r, dict) and "ruleset_id" in r}
        for rs_id in sorted(ruleset_ids):
            _assert_bypass_never(owner, repo, rs_id, api_caller)


def _fetch_default_branch(owner: str, repo: str, api_caller: ApiCaller) -> str:
    try:
        meta = api_caller(f"repos/{owner}/{repo}")
    except Exception as exc:
        raise PreflightError(
            f"preflight P1 failed: could not fetch repos/{owner}/{repo}: {exc}. "
            f"API unreachable / unauthorised → fail-closed (msg-1267 §4)."
        ) from exc
    default_branch = meta.get("default_branch") if isinstance(meta, dict) else None
    if not isinstance(default_branch, str) or not default_branch:
        raise PreflightError(
            f"preflight P1 failed: repos/{owner}/{repo} did not return a default_branch "
            f"(got {default_branch!r})."
        )
    return default_branch


def _fetch_effective_rules(
    owner: str, repo: str, default_branch: str, api_caller: ApiCaller
) -> list[dict[str, Any]]:
    try:
        rules = api_caller(f"repos/{owner}/{repo}/rules/branches/{default_branch}")
    except Exception as exc:
        raise PreflightError(
            f"preflight P1 failed: could not fetch rules for {owner}/{repo}@"
            f"{default_branch}: {exc}. Fail-closed (msg-1267 §4)."
        ) from exc
    if not isinstance(rules, list):
        raise PreflightError(
            f"preflight P1 failed: rules endpoint for {owner}/{repo}@{default_branch} "
            f"returned non-list {type(rules).__name__}."
        )
    return rules


def _assert_bypass_never(owner: str, repo: str, ruleset_id: int, api_caller: ApiCaller) -> None:
    try:
        rs = api_caller(f"repos/{owner}/{repo}/rulesets/{ruleset_id}")
    except Exception as exc:
        raise PreflightError(
            f"preflight P1 failed: could not fetch ruleset {ruleset_id} for "
            f"{owner}/{repo}: {exc}. Fail-closed (msg-1267 §4)."
        ) from exc
    bypass = rs.get("current_user_can_bypass") if isinstance(rs, dict) else None
    if bypass != "never":
        raise PreflightError(
            f"preflight P1 failed: ruleset {ruleset_id} on {owner}/{repo} has "
            f"current_user_can_bypass={bypass!r}; required 'never'. The rules exist but "
            f"the loop's identity can bypass them → the server-side protection is "
            f"unreliable, and the daemon must not proceed on an unreliable premise "
            f"(msg-1265 §5 I-1: '執行者はサーバであってよい' → but only if the server "
            f"actually executes)."
        )


# --------------------------------------------------------------------------- #
# Default I/O — subprocess-based; injectable for tests
# --------------------------------------------------------------------------- #


def _default_remote_reader(repo_dir: Path) -> list[tuple[str, str]]:
    """Return ``[(remote_name, url), ...]`` from ``git -C repo_dir remote -v``.

    Both fetch and push entries are surfaced — a push URL differing from a fetch
    URL still has to pass P2. Sort order is git's own (insertion order), not
    guaranteed to be stable, but P2 iterates all entries so order does not
    change the verdict.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "-v"],
        capture_output=True,
        text=True,
        check=True,
    )
    entries: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        # Each line: `<name>\t<url> (fetch|push)` — split() collapses the tab.
        if len(parts) >= 2:
            entries.append((parts[0], parts[1]))
    return entries


def _default_api_caller(endpoint: str) -> Any:
    """Call ``gh api <endpoint>`` and return the parsed JSON body.

    Any non-zero exit from ``gh`` becomes :class:`subprocess.CalledProcessError`,
    which the P1 helpers catch and re-raise as :class:`PreflightError` — an
    unreachable API is fail-closed by construction.
    """
    proc = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


__all__ = [
    "ApiCaller",
    "PreflightError",
    "RemoteReader",
    "preflight_gate",
]
