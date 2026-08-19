"""Tests for the composition-root preflight (T-drop-branch-prediction-from-allowlist §3).

These pin the invariants that replaced the retired branch-prediction machinery
(N-1..N-3 of msg-1267 §5):

* **N-1** (P1 — server-side protection): a target repo whose default branch is
  missing any of the three rules (``deletion`` / ``non_fast_forward`` /
  ``pull_request``), or whose ruleset lets the loop identity bypass, or whose
  rules the API cannot fetch, halts the daemon (``PreflightError``).

* **N-2** (P0 — daemon checkout separation): a target repo whose resolved path
  equals the daemon's own checkout halts the daemon. The captive-clone
  assumption (§7 of the spec) requires them to be distinct.

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


def test_n1_multiple_rulesets_each_checked(tmp_path: Path) -> None:
    """If the effective rules come from more than one ruleset (org + repo, or
    two repo rulesets), each distinct ruleset_id must be bypass-checked. If
    any single one has bypass != never the daemon halts, regardless of how the
    others fare.
    """
    target, daemon = tmp_path / "t", tmp_path / "d"
    target.mkdir()
    daemon.mkdir()
    api = _healthy_api()
    # Two rulesets covering the branch: the org guardrail (id=21017016, bypass=never)
    # and an additional repo-local ruleset (id=99, bypass=always) that must be
    # detected as unreliable.
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
