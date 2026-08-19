"""Tests for the composition-root preflight (T-drop-branch-prediction-from-allowlist §3).

These pin the invariants that replaced the retired branch-prediction machinery
(N-1..N-3 of msg-1267 §5):

* **N-1** (P1 — server-side protection): a target repo whose default branch is
  missing any of the three rules (``deletion`` / ``non_fast_forward`` /
  ``pull_request``), or whose ruleset lets the loop identity bypass, or whose
  rules the API cannot fetch, halts the daemon (``PreflightError``).

* **N-2** (P0 — daemon checkout separation): a target repo whose resolved path
  equals the daemon's own checkout halts the daemon. The captive-clone
  assumption (§7 of the spec) requires them to be distinct. The msg-1296
  "N-2 addendum" block extends this: a repo_dir that is a *subdirectory* of
  daemon_root with no ``.git`` of its own also halts, because git commands
  there walk upward and find the daemon's ``.git``. Literal path inequality
  is not sufficient; shared git-toplevel is the boundary that matters.

* **N-3** (P2 — remote URL boundary): a target repo whose remote URLs are not
  all under ``https://github.com/SpirrowGames/`` halts the daemon. Positive
  assertion (P2-1); unresolved destination at start denies (P2-2 partial —
  see below); no shell-future prediction (P2-3) — this is a pure URL boundary
  check on the checkout as it stands right now.

**P2-2 partial coverage (msg-1274 §1)**: P2 reads ``.git/config`` at daemon
start and rejects a bad remote there, but it does NOT re-check a remote added
mid-session (``git remote add …``) nor a URL-direct push (``git push
https://…``). That gap was accepted by Takahito Tier-C rather than reintroduce
shell-argument prediction (P2-3 trigger). These tests therefore pin the
start-time snapshot behaviour and NOT dynamic destination coverage.

**N-1 endpoint-choice pins (msg-1286 naysayer follow-up)**: the "N-1 addendum"
block near the bottom pins that P1 queries the *repository* ruleset endpoint
(``repos/{owner}/{repo}/rulesets/{id}``) for both Organization- and
Repository-source rulesets, and that its error messages surface
``ruleset_source_type``/``ruleset_source`` so an operator can tell which
guardrail failed. The naysayer proposed routing by ``source_type`` to
``orgs/{owner}/rulesets/{id}``; direct API measurement 2026-08-19 shows the
loop's classic PAT (scopes ``gist, read:org, repo, workflow``) lacks
``admin:org`` and would 404 on that endpoint, while the repo endpoint returns
org-source rulesets under ``repo`` scope. The tests below make that choice
(and its mitigation-message hint for future GitHub tightening) load-bearing.

**N-1 addendum-2 coupling pins (msg-1291 naysayer follow-up)**: the
"N-1 addendum-2" block pins that P1 couples each required rule type to the
specific ruleset(s) providing it, not to "some ruleset on the branch". Fixes
two independent bugs the earlier draft had: (a) a required type provided
only by Classic Branch Protection (no ``ruleset_id`` in the API response)
would pass because "type present" was satisfied while the un-checkable
classic rule was skipped when collecting rulesets to bypass-check — now
fail-closed with a migration message; (b) an unrelated bypassable ruleset
(e.g. informational ``commit_message_pattern``) on the same branch would
halt the daemon even though no required type depended on it — now ignored.
Both bugs are proved by unit tests using the injected ``_fake_api_caller``.

**N-1 addendum-3 enforcement pin (msg-1300 naysayer follow-up)**: the
"N-1 addendum-3" block pins that P1 verifies ``enforcement == "active"``
in addition to ``current_user_can_bypass == "never"``. An ``evaluate``-mode
ruleset perfectly satisfies the bypass check but only logs violations
without actually blocking pushes on the server — the fail-open bug the
naysayer identified. The parametrized ``test_n1_ruleset_not_active_halts``
covers ``evaluate`` / ``disabled`` / field-absent / arbitrary-future-value,
so anything short of literal ``"active"`` fails-closed by construction.

**N-1 addendum-4 memoization pin (msg-1305 naysayer follow-up)**: the
"N-1 addendum-4" block pins that P1 memoizes the per-``ruleset_id`` verdict
inside one ``preflight_gate`` call. A single ruleset commonly provides
multiple required rule types (the production ``guard-default-branch``
provides all three), and the outer loop over ``_REQUIRED_RULE_TYPES``
would otherwise fetch ``repos/{o}/{r}/rulesets/{id}`` up to three times
per repo per daemon start. The tests use a ``_counting_api_caller`` that
records endpoint hits and asserts exactly one fetch per distinct
``ruleset_id``. The cache is per-call, not global —
``test_n1_cache_is_per_call_not_global`` pins that a second
``preflight_gate`` invocation re-fetches, so a ruleset quietly disabled
between daemon starts is still caught on the next start (msg-1267 §4).

Every I/O boundary (``git remote`` / ``gh api``) is a Callable injected here,
so no test touches the real network or the real GitHub API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.preflight import PreflightError, preflight_gate

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fake_remote_reader(*urls: str) -> Any:
    """Return a remote_reader that yields ``[("origin", urls[0]), ...]``."""
    pairs = [(f"remote-{i}", u) for i, u in enumerate(urls)]

    def _read(_repo: Path) -> list[tuple[str, str]]:
        return pairs

    return _read


def _fake_git_toplevel(mapping: dict[Path, Path | None]) -> Any:
    """Return a git_toplevel fake that looks each resolved path up in ``mapping``.

    A path not present in the mapping resolves to None — which the P0 code
    treats as "no git tree, no shared-.git concern applies". This shape lets a
    test describe exactly which directory shares which git tree without
    touching the filesystem git state.
    """

    def _(p: Path) -> Path | None:
        return mapping.get(p.resolve())

    return _


def _fake_api_caller(responses: dict[str, Any]) -> Any:
    """Return an api_caller that looks each endpoint up in ``responses``.

    A missing endpoint raises RuntimeError (the P1 helpers translate that to
    PreflightError, so an "API unreachable" scenario is expressed as a missing
    key rather than a special sentinel).
    """

    def _call(endpoint: str) -> Any:
        if endpoint not in responses:
            raise RuntimeError(f"no fake for {endpoint}")
        return responses[endpoint]

    return _call


def _healthy_api(
    owner: str = "SpirrowGames",
    repo: str = "spirrow-mindwire",
    ruleset_source_type: str = "Organization",
    ruleset_source: str = "SpirrowGames",
) -> dict[str, Any]:
    """API responses that represent a fully-protected default branch.

    Rules default to the ``guard-default-branch`` org-level ruleset
    (id=21017016, source_type=Organization, source=SpirrowGames) as measured
    against the live API on 2026-08-19. This shape is what production actually
    sees; a per-repo test can override the two source_* params to represent
    a repo-level ruleset instead.
    """
    return {
        f"repos/{owner}/{repo}": {"default_branch": "main"},
        f"repos/{owner}/{repo}/rules/branches/main": [
            {
                "type": "deletion",
                "ruleset_id": 21017016,
                "ruleset_source_type": ruleset_source_type,
                "ruleset_source": ruleset_source,
            },
            {
                "type": "non_fast_forward",
                "ruleset_id": 21017016,
                "ruleset_source_type": ruleset_source_type,
                "ruleset_source": ruleset_source,
            },
            {
                "type": "pull_request",
                "ruleset_id": 21017016,
                "ruleset_source_type": ruleset_source_type,
                "ruleset_source": ruleset_source,
            },
        ],
        f"repos/{owner}/{repo}/rulesets/21017016": {
            "id": 21017016,
            "name": "guard-default-branch",
            "source_type": ruleset_source_type,
            "source": ruleset_source,
            "enforcement": "active",
            "current_user_can_bypass": "never",
        },
    }


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_preflight_passes_when_all_three_hold(tmp_path: Path) -> None:
    """A fully-protected SpirrowGames repo in a separate directory passes clean."""
    target = tmp_path / "target"
    target.mkdir()
    daemon = tmp_path / "daemon"
    daemon.mkdir()
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(_healthy_api()),
    )


# --------------------------------------------------------------------------- #
# N-2 — P0: daemon checkout separation
# --------------------------------------------------------------------------- #


def test_n2_repo_root_equal_to_daemon_root_halts(tmp_path: Path) -> None:
    """P0: the loop's target must be a different clone from the daemon's own."""
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            tmp_path,
            daemon_root=tmp_path,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(_healthy_api()),
        )
    assert "P0" in str(exc.value)
    assert "daemon" in str(exc.value)


def test_n2_repo_root_equal_to_daemon_root_via_resolve_halts(tmp_path: Path) -> None:
    """Both paths resolve to the same directory: still fails (P0 uses .resolve())."""
    target = tmp_path / "sub"
    target.mkdir()
    with pytest.raises(PreflightError):
        preflight_gate(
            target,
            daemon_root=target.resolve(),
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(_healthy_api()),
        )


# --- N-2 addendum — msg-1296 subdirectory bypass fix ---------------------- #
#
# Literal path equality does NOT prove two clones are separate. If repo_dir is
# nested inside daemon_root's git tree without its own .git, git -C repo_dir
# walks upward and finds the daemon's .git. A `git reset --hard` there
# rewrites the daemon's own code. P0 now compares BOTH literal paths (the
# original check, unchanged above) AND the resolved git toplevels (new).


def test_n2_repo_root_inside_daemon_git_tree_no_own_git_halts(tmp_path: Path) -> None:
    """repo_dir is a subdirectory of daemon_root with no .git of its own —
    git commands there walk up to daemon's .git. P0 must halt.

    This is the exact bypass shape msg-1296 named: `[loop].repo_dir` set to
    `/opt/mindwire/sandbox`, daemon at `/opt/mindwire`. Literal equality
    passes (different directories); shared git toplevel does not.
    """
    daemon = tmp_path / "opt-mindwire"
    daemon.mkdir()
    repo = daemon / "sandbox"
    repo.mkdir()
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            repo,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(_healthy_api()),
            # repo has no .git → walks up → toplevel is daemon.
            # daemon is its own .git owner → toplevel is daemon.
            git_toplevel=_fake_git_toplevel(
                {repo.resolve(): daemon.resolve(), daemon.resolve(): daemon.resolve()}
            ),
        )
    msg = str(exc.value)
    assert "P0" in msg
    assert "same" in msg.lower() and "git toplevel" in msg
    assert str(daemon.resolve()) in msg


def test_n2_repo_root_inside_daemon_dir_but_has_own_git_passes(tmp_path: Path) -> None:
    """If the subdirectory owns its OWN .git, git commands don't walk up —
    safe. This is the normal case where an operator legitimately places a
    dedicated clone under the daemon's directory tree (unusual but not
    dangerous when the .git boundary is respected).
    """
    daemon = tmp_path / "opt-mindwire"
    daemon.mkdir()
    repo = daemon / "sandbox"
    repo.mkdir()
    preflight_gate(
        repo,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(_healthy_api()),
        # repo owns its own .git → toplevel is repo itself.
        # daemon is a different git tree → toplevel is daemon.
        git_toplevel=_fake_git_toplevel(
            {repo.resolve(): repo.resolve(), daemon.resolve(): daemon.resolve()}
        ),
    )


def test_n2_daemon_root_is_not_a_git_repo_at_all_still_ok(tmp_path: Path) -> None:
    """If daemon_root has no git tree at all (e.g. daemon is pip-installed),
    the shared-.git concern does not apply — no walk-up can hit a `.git`
    that isn't there. P0 must pass on the toplevel check.
    """
    daemon = tmp_path / "opt-mindwire"
    daemon.mkdir()
    repo = tmp_path / "workspace-sandbox-impl"
    repo.mkdir()
    preflight_gate(
        repo,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(_healthy_api()),
        # repo owns its own .git → toplevel is repo. daemon has no git at
        # all → toplevel None. Different toplevels (None vs repo), no halt.
        git_toplevel=_fake_git_toplevel({repo.resolve(): repo.resolve(), daemon.resolve(): None}),
    )


def test_n2_repo_root_has_no_git_tree_at_all_still_ok(tmp_path: Path) -> None:
    """If repo_dir has no git tree in ANY ancestor, no walk-up can happen
    and P0's toplevel check is satisfied trivially (None left-hand side).
    A subsequent step (P2 remote read) will fail-closed on its own, so
    the "not a git repo" case still halts — just not at P0 line-of-code.
    """
    daemon = tmp_path / "opt-mindwire"
    daemon.mkdir()
    repo = tmp_path / "workspace-sandbox-impl"
    repo.mkdir()

    # P2 fails-closed via the remote_reader that raises RuntimeError.
    def _boom(_: Path) -> list[tuple[str, str]]:
        raise RuntimeError("not a git repo")

    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            repo,
            daemon_root=daemon,
            remote_reader=_boom,
            api_caller=_fake_api_caller(_healthy_api()),
            # Both toplevels are None (neither is a git tree). P0 does NOT
            # halt on this — the follow-on P2 does, on the reader failure.
            git_toplevel=_fake_git_toplevel({repo.resolve(): None, daemon.resolve(): None}),
        )
    assert "P2" in str(exc.value)


def test_n2_deeply_nested_subdir_of_daemon_git_tree_halts(tmp_path: Path) -> None:
    """The bypass fires at any depth — a repo_dir several levels down still
    shares the daemon's git toplevel because git walks all the way up.
    """
    daemon = tmp_path / "opt-mindwire"
    daemon.mkdir()
    nested = daemon / "src" / "spirrow_mindwire" / "adapters"
    nested.mkdir(parents=True)
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            nested,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(_healthy_api()),
            git_toplevel=_fake_git_toplevel(
                {nested.resolve(): daemon.resolve(), daemon.resolve(): daemon.resolve()}
            ),
        )
    assert "P0" in str(exc.value)
    assert "git toplevel" in str(exc.value)


# --------------------------------------------------------------------------- #
# N-3 — P2: remote URL boundary
# --------------------------------------------------------------------------- #


def test_n3_missing_remotes_halts(tmp_path: Path) -> None:
    """P2-2: no remotes → nothing to check → deny."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()

    def _no_remotes(_: Path) -> list[tuple[str, str]]:
        return []

    with pytest.raises(PreflightError) as exc:
        preflight_gate(target, daemon_root=daemon, remote_reader=_no_remotes)
    assert "P2" in str(exc.value)


def test_n3_remote_reader_failure_halts(tmp_path: Path) -> None:
    """P2-2: git remote read raises → fail-closed, not silent."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()

    def _boom(_: Path) -> list[tuple[str, str]]:
        raise RuntimeError("git not found")

    with pytest.raises(PreflightError) as exc:
        preflight_gate(target, daemon_root=daemon, remote_reader=_boom)
    assert "P2" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/takahito-spirrowgames/personal-repo.git",
        "https://github.com/other-org/some-repo.git",
        "git@github.com:SpirrowGames/spirrow-mindwire.git",  # ssh, not HTTPS
        "https://gitlab.com/SpirrowGames/mirror.git",
        "https://example.com/anything",
    ],
)
def test_n3_off_org_remote_halts(tmp_path: Path, url: str) -> None:
    """P2-1: positive assertion — must be under https://github.com/SpirrowGames/.

    Anything else — a personal fork, a different org, an ssh URL, another
    forge — is rejected. This is a boundary check, not a deny-list.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(url),
            api_caller=_fake_api_caller(_healthy_api()),
        )
    assert "P2" in str(exc.value)


def test_n3_one_good_one_bad_still_halts(tmp_path: Path) -> None:
    """Every remote must be under SpirrowGames — a single off-org URL fails."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git",
                "https://github.com/takahito-spirrowgames/other.git",
            ),
            api_caller=_fake_api_caller(_healthy_api()),
        )
    assert "P2" in str(exc.value)


# --------------------------------------------------------------------------- #
# N-1 — P1: server-side protection with bypass=never
# --------------------------------------------------------------------------- #


def test_n1_missing_rule_type_halts(tmp_path: Path) -> None:
    """A default branch missing any of the three required rules → halt."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    # Drop `non_fast_forward` from the effective rules.
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "pull_request",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
    ]
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    assert "P1" in str(exc.value)
    assert "non_fast_forward" in str(exc.value)


def test_n1_bypass_not_never_halts(tmp_path: Path) -> None:
    """Rules present but the loop identity can bypass → server-side protection
    is unreliable, and the daemon must not proceed on an unreliable premise.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"] = {
        "enforcement": "active",
        "current_user_can_bypass": "always",
    }
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    assert "P1" in str(exc.value)
    assert "bypass" in str(exc.value)


def test_n1_api_unreachable_halts(tmp_path: Path) -> None:
    """API cannot answer → unknown → fail-closed (msg-1267 §4)."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller({}),  # every endpoint raises
        )
    assert "P1" in str(exc.value)


def test_n1_ruleset_endpoint_unreachable_halts(tmp_path: Path) -> None:
    """Rules present but ruleset detail cannot be fetched → fail-closed."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    del api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"]
    with pytest.raises(PreflightError):
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )


def test_n1_default_branch_name_missing_halts(tmp_path: Path) -> None:
    """A repo whose meta lacks default_branch cannot be checked → fail-closed."""
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire"] = {}  # no default_branch key
    with pytest.raises(PreflightError):
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )


def test_n1_non_default_branch_name_still_works(tmp_path: Path) -> None:
    """A repo whose default branch is not `main` (e.g. `master`) still passes if
    the three rules exist against that branch with bypass=never.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = {
        "repos/SpirrowGames/thirdy": {"default_branch": "master"},
        "repos/SpirrowGames/thirdy/rules/branches/master": [
            {
                "type": "deletion",
                "ruleset_id": 21017016,
                "ruleset_source_type": "Organization",
                "ruleset_source": "SpirrowGames",
            },
            {
                "type": "non_fast_forward",
                "ruleset_id": 21017016,
                "ruleset_source_type": "Organization",
                "ruleset_source": "SpirrowGames",
            },
            {
                "type": "pull_request",
                "ruleset_id": 21017016,
                "ruleset_source_type": "Organization",
                "ruleset_source": "SpirrowGames",
            },
        ],
        "repos/SpirrowGames/thirdy/rulesets/21017016": {
            "source_type": "Organization",
            "source": "SpirrowGames",
            "enforcement": "active",
            "current_user_can_bypass": "never",
        },
    }
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/thirdy.git"),
        api_caller=_fake_api_caller(api),
    )


# --------------------------------------------------------------------------- #
# N-1 addendum — ruleset endpoint choice (msg-1286 naysayer follow-up)
# --------------------------------------------------------------------------- #
#
# Pinned by direct API measurement 2026-08-19:
#   * repos/SpirrowGames/spirrow-mindwire/rulesets/21017016 → HTTP 200 OK
#     (X-Accepted-Oauth-Scopes: repo), returns the org-source ruleset with
#     current_user_can_bypass=never.
#   * orgs/SpirrowGames/rulesets/21017016 → HTTP 404
#     (X-Accepted-Oauth-Scopes: admin:org — token lacks it).
# So the repo endpoint is the ONLY one usable by the loop's classic PAT, and
# it does cover org-source rulesets. The naysayer proposed routing by
# source_type to `orgs/...`; that would break every P1 call on today's token.
# The correct fail-closed for a future GitHub tightening is
# `test_n1_ruleset_endpoint_unreachable_halts` above — the daemon halts, and
# the operator is told about the source_type in the message so they know a
# token upgrade is what fixes it.


def test_n1_org_source_ruleset_via_repo_endpoint_passes(tmp_path: Path) -> None:
    """Happy path with the shape production actually sees: the only ruleset
    covering the default branch is org-source (SpirrowGames/guard-default-branch),
    and its detail is fetched from `repos/{owner}/{repo}/rulesets/{id}`.
    This is the shape ~all 21 SpirrowGames repos have (msg-1265 §2).
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    # _healthy_api already defaults to Organization / SpirrowGames — this test
    # is the load-bearing pin: renaming, dropping, or wrongly re-routing the
    # source_type handling makes THIS red first.
    api = _healthy_api()
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(api),
    )


def test_n1_repo_source_ruleset_via_repo_endpoint_passes(tmp_path: Path) -> None:
    """Same endpoint, source_type=Repository. Repo-scope tokens can always
    read their own rulesets, so this case is uncontroversial — but pinning it
    makes the "both source_types share the same endpoint" invariant explicit.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api(ruleset_source_type="Repository", ruleset_source="spirrow-mindwire")
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(api),
    )


def test_n1_bypass_never_error_message_names_source_type(tmp_path: Path) -> None:
    """When bypass != never, the error message must include source_type +
    source so the operator can distinguish an org-guardrail failure from a
    repo-local ruleset failure without re-tracing the code.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"] = {
        "enforcement": "active",
        "current_user_can_bypass": "always",
    }
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "source_type='Organization'" in msg
    assert "source='SpirrowGames'" in msg


def test_n1_ruleset_endpoint_unreachable_error_hints_token_upgrade(tmp_path: Path) -> None:
    """When the ruleset detail endpoint fails, the error message must hint at
    the token-upgrade path (admin:org + source_type routing) so the operator
    knows what mitigation to reach for. This is what makes future GitHub
    tightening operator-diagnosable rather than a mystery halt.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    del api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"]
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "admin:org" in msg
    assert "source_type" in msg
    assert "source_type='Organization'" in msg  # the specific origin surfaced


def test_n1_required_type_only_ruleset_bypassable_halts(tmp_path: Path) -> None:
    """If the sole provider of a required rule type is bypassable, halt.

    Here `pull_request` comes only from a repo-local ruleset (id=99,
    bypass=always). The other required types come from the org guardrail
    (bypass=never), so THEY are fine — but the invariant fails because
    `pull_request` has no unbypassable provider.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "pull_request",
            "ruleset_id": 99,
            "ruleset_source_type": "Repository",
            "ruleset_source": "spirrow-mindwire",
        },
    ]
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/99"] = {
        "source_type": "Repository",
        "enforcement": "active",
        "current_user_can_bypass": "always",
    }
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "99" in msg
    assert "always" in msg
    assert "source_type='Repository'" in msg


# --------------------------------------------------------------------------- #
# N-1 addendum-2 — msg-1291 naysayer follow-up
# --------------------------------------------------------------------------- #
#
# Two decoupling bugs the naysayer flagged in the earlier draft, both fixed
# by coupling required-type presence to the specific ruleset(s) providing them:
#
#   (a) FAIL-OPEN on Classic Branch Protection — a rule provided only via
#       Classic BP is returned by rules/branches/{b} WITHOUT a `ruleset_id`.
#       The earlier code counted it as "type present" (satisfying the
#       required-types check) but skipped it when collecting rulesets to
#       bypass-check (needs `ruleset_id`). Result: an admin-bypassable
#       Classic BP could satisfy P1 with bypass never verified.
#
#   (b) OVER-DENY on unrelated bypassable ruleset — if the branch has ANY
#       ruleset that is bypassable (e.g. an informational
#       `commit_message_pattern`), the earlier code called
#       _assert_bypass_never on it and halted, even though it does not
#       provide any required rule type.
#
# Both are provable by unit test; live measurement cannot show them because
# our target repos only have the org guardrail's ruleset-sourced rules.


def test_n1_required_type_from_classic_bp_only_halts(tmp_path: Path) -> None:
    """A required type provided only via Classic Branch Protection (no
    ruleset_id in the effective-rules response) is REJECTED — the daemon
    cannot verify its bypass status via this endpoint, and "cannot verify"
    must not be silently upgraded to "protected" (msg-1265 §5 I-3).

    This test pins the fix for the FAIL-OPEN bug the naysayer named.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    # `deletion` and `non_fast_forward` come from the org guardrail; but
    # `pull_request` is provided only by a rule dict without any ruleset_id
    # (the shape GitHub returns for Classic Branch Protection). Under the
    # earlier code this passed (type present, no ruleset to bypass-check);
    # under the fix it must halt.
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        # Classic BP shape: no ruleset_id field. The invariant demands
        # explicit rejection because bypass cannot be verified.
        {"type": "pull_request"},
    ]
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "pull_request" in msg
    assert "Classic Branch Protection" in msg
    assert "Migrate" in msg or "migrate" in msg


def test_n1_required_type_from_classic_bp_only_null_ruleset_id_halts(tmp_path: Path) -> None:
    """Same as above but the field is explicitly ``null`` instead of absent.
    Both shapes must fail-closed; ``.get("ruleset_id")`` returns None for
    both, and the fix must not distinguish them.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {"type": "pull_request", "ruleset_id": None},
    ]
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "pull_request" in msg
    assert "Classic Branch Protection" in msg


def test_n1_extra_bypassable_unrelated_ruleset_does_not_halt(tmp_path: Path) -> None:
    """An unrelated bypassable ruleset on the same branch must NOT halt.

    If a repo has, say, an informational `commit_message_pattern` rule from
    a repo-local ruleset (id=42, bypass=always), that ruleset does not
    provide any required rule type. The required types are all covered by
    the unbypassable org guardrail (id=21017016). Preflight must pass.

    This test pins the fix for the OVER-DENY bug the naysayer named. Under
    the earlier code, `ruleset_meta` collected id=42 and bypass-checked it,
    seeing "always" and halting.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "pull_request",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        # Unrelated informational ruleset on the same branch, bypassable
        # for the loop identity. Must NOT be consulted — it does not
        # provide any required rule type.
        {
            "type": "commit_message_pattern",
            "ruleset_id": 42,
            "ruleset_source_type": "Repository",
            "ruleset_source": "spirrow-mindwire",
        },
    ]
    # Deliberately DO NOT add an entry for ruleset 42 — under the fix, we
    # must never query it. If the fix regresses and calls it, the fake will
    # raise RuntimeError, which _bypass_never converts to fetch-failure,
    # which propagates to a P1 halt. Either way the test would red.
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(api),
    )


def test_n1_required_type_with_mixed_providers_passes_when_one_strict(tmp_path: Path) -> None:
    """If a required type has TWO providers — one bypassable, one strict —
    the strict one satisfies the invariant. GitHub layers rulesets, so the
    stricter one wins at enforcement time. "At least one unbypassable" is
    the invariant, not "all providers unbypassable".
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        # `deletion` provided by TWO rulesets: strict org guardrail AND
        # a lax repo-local ruleset. The strict one satisfies the invariant.
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "deletion",
            "ruleset_id": 77,
            "ruleset_source_type": "Repository",
            "ruleset_source": "spirrow-mindwire",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "pull_request",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
    ]
    # Ruleset 77 is bypassable — but our search stops at the first
    # unbypassable provider (21017016), so 77 never gets queried.
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/77"] = {
        "source_type": "Repository",
        "enforcement": "active",
        "current_user_can_bypass": "always",
    }
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(api),
    )


def test_n1_required_type_from_both_classic_and_strict_ruleset_passes(tmp_path: Path) -> None:
    """A required type provided by BOTH Classic BP (no ruleset_id) AND a
    strict ruleset must pass. The strict ruleset provides an unbypassable
    verifiable provider; the Classic BP is irrelevant to the invariant
    once the strict one satisfies it.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {"type": "deletion"},  # Classic BP
        {
            "type": "deletion",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "pull_request",
            "ruleset_id": 21017016,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
    ]
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(api),
    )


def test_n1_error_bullets_all_problems_at_once(tmp_path: Path) -> None:
    """If multiple required types fail for different reasons, the error
    message must name ALL of them at once — operator sees the full picture
    on the first halt, not one problem per redeploy.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        # `deletion` missing entirely
        # `non_fast_forward` from Classic BP only
        {"type": "non_fast_forward"},
        # `pull_request` from a bypassable ruleset only
        {
            "type": "pull_request",
            "ruleset_id": 88,
            "ruleset_source_type": "Repository",
            "ruleset_source": "spirrow-mindwire",
        },
    ]
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/88"] = {
        "source_type": "Repository",
        "enforcement": "active",
        "current_user_can_bypass": "always",
    }
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    # Every required type gets its own line — no early-return
    assert "deletion" in msg
    assert "non_fast_forward" in msg
    assert "pull_request" in msg
    # And each gets its own reason
    assert "not present" in msg  # deletion missing
    assert "Classic Branch Protection" in msg  # non_fast_forward
    assert "88" in msg  # pull_request bypassable ruleset id
    assert "always" in msg  # bypass value


# --------------------------------------------------------------------------- #
# N-1 addendum-3 — msg-1300 naysayer: enforcement=active must be verified
# --------------------------------------------------------------------------- #
#
# GitHub rulesets have three enforcement levels: `active` (blocks), `evaluate`
# (logs violations without blocking), `disabled` (does nothing). The
# `rules/branches/{b}` endpoint returns effective rules from BOTH `active`
# AND `evaluate` rulesets. So a ruleset in `evaluate` mode:
#   * appears in the effective-rules response — the required-type check passes
#   * has current_user_can_bypass="never" — the earlier bypass check passed
#   * does NOT block server-side — the loop's push succeeds anyway
# The msg-1300 naysayer finding is exactly this fail-open. The fix requires
# enforcement="active" alongside bypass="never".


@pytest.mark.parametrize("enforcement", ["evaluate", "disabled", None, "unknown-future-value"])
def test_n1_ruleset_not_active_halts(tmp_path: Path, enforcement: str | None) -> None:
    """A ruleset in any non-active enforcement mode must halt P1 even when
    bypass=never — evaluate/disabled rulesets do not actually block pushes
    on the server. `None` covers the "field absent" case; the arbitrary
    future value ensures we default-deny anything GitHub might add.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    ruleset: dict[str, Any] = {
        "source_type": "Organization",
        "source": "SpirrowGames",
        "current_user_can_bypass": "never",
    }
    if enforcement is not None:
        ruleset["enforcement"] = enforcement
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"] = ruleset
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "P1" in msg
    assert "enforcement" in msg
    assert repr(enforcement) in msg  # names the actual value so operator can act
    assert "'active'" in msg  # names the expected value


def test_n1_ruleset_active_bypass_never_passes(tmp_path: Path) -> None:
    """Positive counterpart: a ruleset that is BOTH active AND unbypassable
    passes. This is the production shape measured 2026-08-19 on the org
    guardrail (see msg-1265 §2).
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    # _healthy_api now defaults to enforcement=active + bypass=never — the
    # load-bearing pin that a regression flipping either half reddens.
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=_fake_api_caller(_healthy_api()),
    )


def test_n1_bypass_check_only_runs_after_enforcement_check(tmp_path: Path) -> None:
    """If a ruleset is both non-active AND bypassable, the error message
    names the enforcement problem (which is checked first) — the operator's
    remediation is to activate the ruleset, not to tighten bypass on an
    inactive rule that isn't blocking anything anyway.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"] = {
        "source_type": "Organization",
        "source": "SpirrowGames",
        "enforcement": "evaluate",
        "current_user_can_bypass": "always",  # also broken, but enforcement wins
    }
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=_fake_api_caller(api),
        )
    msg = str(exc.value)
    assert "enforcement='evaluate'" in msg
    # The bypass value should NOT be surfaced when enforcement wins — the
    # operator has one thing to fix, not two.
    assert "current_user_can_bypass='always'" not in msg


# --------------------------------------------------------------------------- #
# N-1 addendum-4 — msg-1305 naysayer: per-ruleset memoization
# --------------------------------------------------------------------------- #
#
# The production `guard-default-branch` ruleset (id=21017016) provides all
# three required rule types (`deletion` / `non_fast_forward` /
# `pull_request`) in a single ruleset. Without memoization, the outer loop
# over `_REQUIRED_RULE_TYPES` calls `_ruleset_actively_enforces` — and
# therefore `gh api repos/{o}/{r}/rulesets/{id}` — three times for the same
# ruleset_id per repo per daemon start. Msg-1305 naysayer flagged this as
# a 3x quota-burn against the existing dedup-on-remotes optimization.
#
# The fix is a per-call `evaluated: dict[int, (ok, reason)]` cache inside
# `_p1_required_types_covered_by_unbypassable_rulesets`. These tests pin
# the memoization by counting endpoint calls in the injected api_caller.


def _counting_api_caller(responses: dict[str, Any]) -> tuple[Any, dict[str, int]]:
    """Like ``_fake_api_caller`` but also returns a call-count dict keyed by
    endpoint. Regressions that lose memoization show up as counts > 1 for
    the same ``rulesets/{id}`` endpoint.
    """
    counts: dict[str, int] = {}

    def _call(endpoint: str) -> Any:
        counts[endpoint] = counts.get(endpoint, 0) + 1
        if endpoint not in responses:
            raise RuntimeError(f"no fake for {endpoint}")
        return responses[endpoint]

    return _call, counts


def test_n1_single_ruleset_providing_all_required_types_fetched_once(
    tmp_path: Path,
) -> None:
    """Production shape: one org ruleset provides all three required rule
    types. Without memoization, the ruleset endpoint is fetched 3x per repo
    per daemon start; with memoization, 1x. Pins the fetch count.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api, counts = _counting_api_caller(_healthy_api())
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=api,
    )
    # The load-bearing assertion: exactly ONE fetch of the ruleset detail
    # endpoint, not three (one per required rule type).
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rulesets/21017016") == 1
    # Sanity: repo meta and rules-branches endpoints each fetched once too
    # (they were already deduplicated by construction — not the target of
    # this test, but a regression in either would surface here).
    assert counts.get("repos/SpirrowGames/spirrow-mindwire") == 1
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rules/branches/main") == 1


def test_n1_multiple_rulesets_fetched_once_each(tmp_path: Path) -> None:
    """When required types come from DIFFERENT ruleset_ids, each ruleset is
    fetched exactly once — not N times per type it provides.

    Scenario: `deletion` + `non_fast_forward` from ruleset 100 (both types),
    `pull_request` from ruleset 200. Without memoization, ruleset 100 would
    be fetched twice (once per type it provides).
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    api["repos/SpirrowGames/spirrow-mindwire/rules/branches/main"] = [
        {
            "type": "deletion",
            "ruleset_id": 100,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "non_fast_forward",
            "ruleset_id": 100,
            "ruleset_source_type": "Organization",
            "ruleset_source": "SpirrowGames",
        },
        {
            "type": "pull_request",
            "ruleset_id": 200,
            "ruleset_source_type": "Repository",
            "ruleset_source": "spirrow-mindwire",
        },
    ]
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/100"] = {
        "source_type": "Organization",
        "source": "SpirrowGames",
        "enforcement": "active",
        "current_user_can_bypass": "never",
    }
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/200"] = {
        "source_type": "Repository",
        "source": "spirrow-mindwire",
        "enforcement": "active",
        "current_user_can_bypass": "never",
    }
    # Remove the default 21017016 entry (not present in the effective rules
    # here) so a stray query would raise via the fake.
    del api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"]
    counting_api, counts = _counting_api_caller(api)
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=counting_api,
    )
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rulesets/100") == 1
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rulesets/200") == 1


def test_n1_memoization_preserves_failure_verdict(tmp_path: Path) -> None:
    """When a ruleset fails the enforcement check, the cached failure
    verdict must be reused across required types — not silently re-tried
    (which could mask a transient success and let a broken ruleset satisfy
    the check by accident). Same failure, same reason, same halt.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    # Ruleset 21017016 provides all 3 required types but is bypassable.
    api["repos/SpirrowGames/spirrow-mindwire/rulesets/21017016"] = {
        "source_type": "Organization",
        "source": "SpirrowGames",
        "enforcement": "active",
        "current_user_can_bypass": "always",
    }
    counting_api, counts = _counting_api_caller(api)
    with pytest.raises(PreflightError) as exc:
        preflight_gate(
            target,
            daemon_root=daemon,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
            api_caller=counting_api,
        )
    # ONE fetch even in the failure path — the cached (False, reason) is
    # reused for the second and third required types.
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rulesets/21017016") == 1
    # All three required types surface in the error (each names ruleset
    # 21017016 as its bypassable-only provider).
    msg = str(exc.value)
    assert "deletion" in msg
    assert "non_fast_forward" in msg
    assert "pull_request" in msg
    assert "21017016" in msg


def test_n1_cache_is_per_call_not_global(tmp_path: Path) -> None:
    """The cache lives inside one preflight_gate call — a subsequent call
    (i.e. next daemon start) must re-fetch. Msg-1267 §4: "no cache" —
    a ruleset quietly disabled between daemon starts must be caught on
    the next start.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api, counts = _counting_api_caller(_healthy_api())
    # First preflight run
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=api,
    )
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rulesets/21017016") == 1
    # Second preflight run must fetch again (fresh cache).
    preflight_gate(
        target,
        daemon_root=daemon,
        remote_reader=_fake_remote_reader("https://github.com/SpirrowGames/spirrow-mindwire.git"),
        api_caller=api,
    )
    assert counts.get("repos/SpirrowGames/spirrow-mindwire/rulesets/21017016") == 2


# --------------------------------------------------------------------------- #
# preflight_gate raises PreflightError (SystemExit) — not a plain exception
# --------------------------------------------------------------------------- #


def test_preflight_error_is_a_system_exit(tmp_path: Path) -> None:
    """PreflightError subclasses SystemExit so an uncaught raise reaches the
    process boundary and the daemon launcher surfaces the reason.
    """
    with pytest.raises(SystemExit):
        preflight_gate(
            tmp_path,
            daemon_root=tmp_path,
            remote_reader=_fake_remote_reader(
                "https://github.com/SpirrowGames/spirrow-mindwire.git"
            ),
        )
