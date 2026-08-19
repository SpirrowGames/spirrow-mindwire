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

**Scope of guarantee — READ THIS BEFORE TRUSTING IT** (msg-1274 §1).
This module is a **start-time precondition check on the checkout as it stands
right now**, not a per-turn wire-level enforcement:

* **P2 is measured against the ``.git/config`` snapshot at daemon start.** A
  remote added or repointed *during a session* (``git remote add attacker
  https://…`` / ``git remote set-url origin …``) is NOT re-checked here. Nor
  is a raw ``git push https://…/other-repo main`` — the destination URL comes
  from the command line, not from ``.git/config``. **P2-2 (msg-1270) explicitly
  demanded coverage of both**; PR #159 does NOT deliver it. See "Explicitly
  out of scope" below for why.

* **P1 reads GitHub's rules for exactly the repos P2 saw at start.** A remote
  added later — even one under ``https://github.com/SpirrowGames/`` — is not
  vetted for ``guard-default-branch`` presence or ``bypass=never``. The
  "server-side protection" guarantee this module offers is "the repos this
  checkout is pointed at right now are protected", not "any repo a push might
  reach is protected".

Three checks, in one order so the earliest failure is the loudest:

* **P0** — the implementer's ``repo_dir`` is not the daemon's own checkout.
  Two different clones. The safety story ("a chained ``git checkout main &&
  git reset --hard`` burns a throwaway clone, reflog restores") stops holding
  the moment the daemon runs against its own code. The check compares BOTH
  (a) the resolved absolute paths (literal equality) AND (b) the git
  toplevels (msg-1296 naysayer follow-up): a ``repo_dir`` that is a
  subdirectory of ``daemon_root`` with no ``.git`` of its own would satisfy
  (a) — the paths differ — while ``git -C repo_dir <cmd>`` still walks
  upward and finds the daemon's ``.git``. (b) closes that bypass.

* **P2** — every remote URL in ``repo_dir``'s ``.git/config`` at start is
  under ``https://github.com/SpirrowGames/``. Tier-C decide 2026-08-19
  msg-1270 option β: network-boundary enforcement, not credential-scope
  enforcement. R-1 measured the loop's PAT is a classic ``gho_`` OAuth token
  with scope ``repo``, so the credential does not itself bound reach — the
  URL does, and only for the remotes that existed at start.

* **P1** — the default branch of each remote's repo has the three required
  rule types (``deletion`` / ``non_fast_forward`` / ``pull_request``), AND
  for each required type at least one ruleset that provides it is BOTH
  ``enforcement == "active"`` (not ``evaluate`` / ``disabled``) AND
  ``current_user_can_bypass == "never"`` from the loop identity. The
  enforcement check is load-bearing (msg-1300 naysayer finding): the
  ``rules/branches/{b}`` endpoint returns rules from both ``active`` and
  ``evaluate`` rulesets, so a rule appearing in the response does NOT mean
  the server will block the write — an ``evaluate``-mode ruleset logs
  violations without blocking, and would satisfy the earlier bypass-only
  check while providing no actual protection. The
  provider-to-required-type coupling is load-bearing (msg-1291 naysayer
  finding): checking "types present" and "some ruleset on the branch is
  unbypassable" as independent conditions fails-open when a required type
  is provided only by Classic Branch Protection (which the API returns
  without ``ruleset_id``, so it satisfies "types present" but is invisible
  to the ruleset bypass-check), and over-denies when an unrelated
  bypassable ruleset (e.g. an informational ``commit_message_pattern``) is
  present on the same branch. "Rules present" is not enough: if the loop
  can bypass the ruleset that provides the rule, the rule does not protect
  it — and if a ruleset the loop can bypass provides an entirely different
  rule, halting on it is over-deny theatre.

The three run in this order because P2 gates P1's *scope*: P1 asks GitHub
about specific repos, and asking about a repo P2 has already rejected only
adds a way for a bad-owner probe to fail slower. P0 comes first because it's
the cheapest and the one whose failure means "your daemon configuration is
broken, do not proceed", which subsumes both P1 and P2.

**Explicitly out of scope — the P2-2 gap** (msg-1274 §1, Tier-C decide option
(gamma) = accept-and-document). Full P2-2 coverage would require the
classifier to parse every ``git push`` destination and every ``git remote
add|set-url`` mid-session and reject a non-SpirrowGames URL. That
reintroduces exactly the shell-argument-prediction machinery this whole task
removed (P2-3: "境界検査が予測に化けるなら option-β は失敗"). Given today's
measurement — org ruleset covers all 21 SpirrowGames default branches, no
personal repo on ``takahito-spirrowgames`` — the residual exposure has TWO
distinct shapes (msg-1296 naysayer follow-up corrected a blind spot in the
earlier phrasing that named only the first):

  (1) **Dynamic-destination bypass to any branch on an org-external repo.**
      A ``git remote add`` / ``set-url`` / URL-direct ``git push`` inside a
      session lands wherever the ``repo`` scope reaches. Today the token
      cannot reach any external repo it does not already have push access
      to — but ``takahito-spirrowgames`` could gain a personal repo, or a
      collaboration invite could arrive, and those would become reachable.

  (2) **Native push to a non-default branch on an ORG-INTERNAL repo.**
      Removing ``branch_glob`` means the allow-list no longer restricts
      which branch the loop pushes to. The ``guard-default-branch``
      GitHub ruleset covers ``~DEFAULT_BRANCH`` only. So the loop can
      natively push (or force-push) to ``release/*`` / ``production`` /
      ``gh-pages`` / any non-default branch on a SpirrowGames repo. This
      is by design of the Tier-C decide (msg-1265 §5 I-1: "main への push
      ... が拒否されること"): the server-side guarantee is scoped to
      ``main``, and other branches are governed by PR review workflow and
      any repo-local rulesets, not by this preflight. If a project needs
      the loop kept off a non-default branch, add a ruleset (org- or
      repo-level) covering that branch pattern — a ruleset-addition task,
      not an implementer-code task.

Takahito accepted both gaps on 2026-08-19 rather than restart the prediction
problem (would resurrect the machinery this whole task removed — P2-3 explicit
trigger) or block the PR on a fine-grained-PAT rollout / ruleset expansion.
If (1) changes shape (personal repo appears, ruleset weakens) the mitigation
is option-alpha (a scope-restricted ``MINDWIRE_GITHUB_TOKEN``). If (2) needs
to shrink for a specific repo, add a branch-pattern ruleset covering the
protected refs; neither mitigation belongs in this file.

**Ruleset endpoint choice — measured 2026-08-19, pinned by tests below.**
GitHub exposes two endpoints for a ruleset detail:

* ``GET /repos/{owner}/{repo}/rulesets/{id}`` — ``X-Accepted-Oauth-Scopes: repo``
* ``GET /orgs/{org}/rulesets/{id}`` — ``X-Accepted-Oauth-Scopes: admin:org``

The loop's token (R-1, msg-1269 §2) is a classic OAuth PAT with scopes
``gist, read:org, repo, workflow`` — it does NOT hold ``admin:org``, so the
``orgs/`` endpoint returns HTTP 404. However, the ``repos/`` endpoint
**returns org-source rulesets too** (verified against ``guard-default-branch``
id=21017016 on ``spirrow-mindwire`` and ``Spirrow-VoxelWorld``, both HTTP 200
with ``source_type: "Organization"`` and the correct
``current_user_can_bypass`` field for the calling identity). So P1 uses the
``repos/`` endpoint for every ruleset regardless of ``ruleset_source_type``.

Routing by ``ruleset_source_type`` to the ``orgs/`` endpoint would break the
daemon today (every P1 call would 404 on missing ``admin:org``). If GitHub
tightens the ``repos/`` endpoint later to reject org-source rulesets, P1
already fails closed at ``test_n1_ruleset_endpoint_unreachable_halts`` — the
right response would be a token upgrade (grant ``admin:org`` and route by
source_type), which is a Tier-C operation outside this file's scope. The
``ruleset_source_type`` value is surfaced in log lines and error messages so
the operator can distinguish an org-source vs repo-source failure without
re-tracing the code.

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

GitToplevel = Callable[[Path], Path | None]
"""``git -C path rev-parse --show-toplevel`` → resolved toplevel Path, or None
if the path is not inside any git working tree / git is unavailable / the path
does not exist. Injectable for tests. Used by P0 to catch the subdirectory
bypass (msg-1296 naysayer finding): if ``repo_dir`` is nested inside
``daemon_root``'s git tree without its own ``.git``, git commands executed in
``repo_dir`` walk upward and find the daemon's ``.git`` — literal path
inequality is not sufficient to prove the two clones are actually separate."""


def preflight_gate(
    repo_dir: Path,
    *,
    daemon_root: Path | None = None,
    api_caller: ApiCaller | None = None,
    remote_reader: RemoteReader | None = None,
    git_toplevel: GitToplevel | None = None,
) -> None:
    """Enforce P0/P1/P2. Raise :class:`PreflightError` on any failure.

    ``daemon_root`` defaults to :func:`Path.cwd`. The composition root invokes
    this once per daemon start; there is no per-turn re-check and no cache —
    a subsequent daemon run pays the same 1 API call before it acts. Failure
    to reach the API is fail-closed (msg-1267 §4).

    What this call guarantees, precisely (msg-1274 §1):

    * At this instant, ``repo_dir/.git/config`` lists only remotes under
      ``https://github.com/SpirrowGames/`` (P2 snapshot), and each of those
      repos has ``guard-default-branch`` active with the loop's identity
      unable to bypass (P1).

    What this call does NOT guarantee:

    * That a subsequent ``git remote add`` / ``git remote set-url`` / raw
      ``git push https://…`` inside a session cannot send commits to a
      different repo. The P2-2 requirement from msg-1270 (dynamic destination
      coverage) is not implemented; the exposure was accepted on 2026-08-19
      (msg-1274 §1) rather than resurrect the branch-prediction machinery.
    * That a native push to a **non-default branch on an org-internal repo**
      (``release/*``, ``production``, ``gh-pages``, etc.) is blocked — the
      allow-list no longer restricts push branches, and the
      ``guard-default-branch`` GitHub ruleset covers only ``~DEFAULT_BRANCH``.
      The loop CAN push (and force-push) to those branches through the
      legitimate origin remote; the design intent (msg-1265 §5 I-1) scopes
      the server guarantee to ``main``. See the module docstring "the
      residual exposure has TWO distinct shapes" block for the msg-1296
      correction of an earlier phrasing that named only the first shape.
    * That a ``main`` merge via ``gh pr merge`` is blocked here — that
      guarantee lives in the allow-list's ``git.merge_to_main`` forbidden
      route, not in this preflight.
    """
    daemon_root = daemon_root or Path.cwd()
    api = api_caller or _default_api_caller
    remotes_of = remote_reader or _default_remote_reader
    toplevel_of = git_toplevel or _default_git_toplevel

    _p0_daemon_checkout_separated(repo_dir, daemon_root, toplevel_of)
    remotes = _p2_remote_urls_under_spirrowgames(repo_dir, remotes_of)
    _p1_default_branch_protected(remotes, api)
    logger.info(
        "preflight ok (start-time snapshot; no per-turn re-check for dynamic "
        "remotes / URL-direct pushes — msg-1274 §1): "
        "repo_dir=%s daemon_root=%s remotes=%d",
        repo_dir,
        daemon_root,
        len(remotes),
    )


# --------------------------------------------------------------------------- #
# P0 — daemon checkout separation
# --------------------------------------------------------------------------- #


def _p0_daemon_checkout_separated(
    repo_dir: Path, daemon_root: Path, git_toplevel: GitToplevel
) -> None:
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
    # msg-1296 naysayer follow-up: literal path inequality is not enough. If
    # repo_dir is nested inside daemon_root's git tree without its own .git,
    # `git -C repo_dir <cmd>` walks up and finds daemon's .git — a
    # `git reset --hard` or `git clean -fdx` there rewrites the daemon's own
    # code. Compare the resolved git toplevels so a subdirectory that shares
    # the daemon's .git still fails P0.
    #
    # We ONLY halt when both toplevels are non-None AND equal. Non-None means
    # git could actually resolve a toplevel for that path; None means either
    # git is unavailable, the path is not in any git tree, or the path does
    # not exist. If repo_dir has no toplevel at all, no walk-up is possible
    # (git in repo_dir would just error), so the shared-.git concern does not
    # apply — safe to let P1/P2 catch the "not a git repo" case in their own
    # error messages instead.
    r_top = git_toplevel(repo_dir)
    d_top = git_toplevel(daemon_root)
    if r_top is not None and d_top is not None and r_top == d_top:
        raise PreflightError(
            f"preflight P0 failed: implementer's repo_dir ({r}) and daemon's "
            f"checkout ({d}) are different directories but resolve to the SAME "
            f"git toplevel ({r_top}). Because repo_dir has no .git of its own, "
            f"git commands executed there walk upward and find the daemon's "
            f".git — a `git reset --hard` or `git clean -fdx` in the loop's "
            f"target repo would rewrite the daemon's own code. Literal path "
            f"inequality is not a sufficient boundary; the shared git tree is. "
            f"Point [loop].repo_dir at a directory that has its own `.git` "
            f"(the current production shape: C:/workspace/sandbox/<project>-impl "
            f"is a distinct git clone, not a subdirectory of the daemon's tree)."
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
        _p1_required_types_covered_by_unbypassable_rulesets(
            owner, repo, default_branch, name, rules, api_caller
        )


def _p1_required_types_covered_by_unbypassable_rulesets(
    owner: str,
    repo: str,
    default_branch: str,
    remote_name: str,
    rules: list[dict[str, Any]],
    api_caller: ApiCaller,
) -> None:
    """Enforce the P1 invariant on the effective-rules response.

    The invariant (msg-1265 §5 I-1): for the loop's identity, each required
    rule type on the default branch must be enforced by AT LEAST ONE ruleset
    that is unbypassable for that identity. This function couples the two
    halves of the check that used to be independent (msg-1291 naysayer
    finding):

    1. **Per-required-type provider search** — an earlier draft counted
       "required type present in the rules list" and "some ruleset on the
       branch is unbypassable" as separate conditions. That fails-open when
       a required rule is provided ONLY by Classic Branch Protection (the
       API returns those without a ``ruleset_id``): "present" was satisfied,
       but the classic-source rule was skipped when collecting rulesets to
       bypass-check, so bypass was never verified for it. It also
       over-denied: an unrelated bypassable ruleset on the branch (e.g.
       ``commit_message_pattern`` from a repo-local informational ruleset)
       halted the daemon even though no required rule depended on it. Fixed
       here by walking ``_REQUIRED_RULE_TYPES`` and, for each, finding the
       specific ruleset(s) providing it.

    2. **Verifiability rejection** — if a required rule type is provided
       only by rules lacking ``ruleset_id`` (Classic Branch Protection is
       the shape the naysayer named), we cannot verify its bypass status
       via the ``rulesets/{id}`` endpoint. Fail-closed (msg-1265 §5 I-3):
       the operator is told to migrate to a Repository/Organization
       Ruleset. We do NOT try to consult
       ``repos/{o}/{r}/branches/{b}/protection`` here — that endpoint has
       its own permission requirements and shape, and adding a second
       verification path would double the failure modes for no gain: the
       migration is the target state anyway (Repository Rulesets and Org
       Rulesets supersede Classic Branch Protection in GitHub's roadmap).

    3. **"At least one" not "all"** — GitHub layers rulesets, so if two
       rulesets both restrict the same operation, the ruleset with the
       stricter bypass wins at enforcement time. We only need to find ONE
       verifiable, unbypassable provider per required type. This also
       prevents over-deny when a strict ruleset coexists with a laxer one:
       the strict one satisfies the check, the lax one is not consulted.

    4. **Per-ruleset memoization** — msg-1305 naysayer finding. A single
       ruleset commonly provides multiple required rule types at once
       (the production ``guard-default-branch`` ruleset id=21017016
       provides all three: ``deletion`` / ``non_fast_forward`` /
       ``pull_request``). Without memoization, the outer loop over
       ``_REQUIRED_RULE_TYPES`` would call ``_ruleset_actively_enforces``
       — and therefore ``gh api repos/{o}/{r}/rulesets/{id}`` — up to
       three times for the same ``ruleset_id`` per repo per daemon
       start. The local ``evaluated`` dict caches the ``(ok, reason)``
       verdict per ``ruleset_id`` for the duration of one call to this
       function, cutting the redundant fetches to zero. The cache is
       intentionally NOT persisted across daemon starts (msg-1267 §4:
       "no cache" — a subsequent daemon run pays the same 1 API call
       before it acts, so a rule quietly disabled since the last run is
       caught on the next start).
    """
    # Group rules by type. Skip malformed entries defensively (a str type on
    # a non-string field would break `.get("type")` further down).
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        if not isinstance(r, dict):
            continue
        rtype = r.get("type")
        if isinstance(rtype, str):
            by_type.setdefault(rtype, []).append(r)

    # Per-call ruleset verdict cache (msg-1305 naysayer follow-up). Keyed
    # by ruleset_id — source_type / source are provenance metadata that ride
    # along with the (ok, reason) tuple only for error-message context; two
    # rules with the same ruleset_id necessarily have the same source
    # metadata, so keying on the id is sufficient.
    evaluated: dict[int, tuple[bool, str]] = {}

    problems: list[str] = []
    for rtype in sorted(_REQUIRED_RULE_TYPES):
        providers = by_type.get(rtype, [])
        if not providers:
            problems.append(
                f"required rule type {rtype!r} is not present in the effective "
                f"rules for the default branch — the invariant that 'no push / "
                f"force-push / ref-deletion reaches main' relies on this rule"
            )
            continue

        # Split verifiable (has ruleset_id) from Classic-BP-shaped (no
        # ruleset_id). `.get("ruleset_id")` returns None for both "field
        # absent" and "field is null", covering both API shapes.
        verifiable = [p for p in providers if p.get("ruleset_id") is not None]
        if not verifiable:
            problems.append(
                f"required rule type {rtype!r} has {len(providers)} provider(s), "
                f"none of which carries a ruleset_id — the shape GitHub returns "
                f"for Classic Branch Protection. This endpoint cannot verify "
                f"their bypass status, so the loop identity might still hold "
                f"admin rights that override them. Migrate the protection for "
                f"this rule to a Repository Ruleset or an Organization Ruleset "
                f"with current_user_can_bypass=never — see the module docstring "
                f"'Ruleset endpoint choice' block for endpoint mechanics"
            )
            continue

        reasons: list[str] = []
        satisfied = False
        for p in verifiable:
            rs_id = p["ruleset_id"]
            source_type = p.get("ruleset_source_type")
            source = p.get("ruleset_source")
            cached = evaluated.get(rs_id)
            if cached is None:
                cached = _ruleset_actively_enforces(
                    owner, repo, rs_id, source_type, source, api_caller
                )
                evaluated[rs_id] = cached
            ok, reason = cached
            if ok:
                satisfied = True
                break
            reasons.append(reason)
        if not satisfied:
            provider_ids = sorted({p["ruleset_id"] for p in verifiable})
            problems.append(
                f"required rule type {rtype!r} has verifiable provider ruleset(s) "
                f"{provider_ids} but none is unbypassable for the loop identity. "
                f"Reasons: {reasons}"
            )

    if problems:
        # One bulleted PreflightError per repo; each bullet names a distinct
        # problem so the operator can see all of them at once instead of
        # fixing one and rediscovering the next on the next daemon start.
        bullets = "\n  - ".join(problems)
        raise PreflightError(
            f"preflight P1 failed: {owner}/{repo}@{default_branch} "
            f"(remote {remote_name!r}) is not adequately server-protected:\n  - "
            f"{bullets}\n"
            f"GitHub server-side protection is what executes 'no push / "
            f"force-push / ref-deletion reaches main' after the local prediction "
            f"machinery was retired on 2026-08-19."
        )


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


def _ruleset_actively_enforces(
    owner: str,
    repo: str,
    ruleset_id: int,
    source_type: str | None,
    source: str | None,
    api_caller: ApiCaller,
) -> tuple[bool, str]:
    """Check whether one ruleset actively protects the branch for this identity.

    Returns ``(True, "")`` when BOTH invariants hold, ``(False, reason)``
    otherwise (fetch failure, non-active enforcement, or bypassable):

    * ``enforcement == "active"`` — GitHub rulesets have three enforcement
      levels (``active`` / ``evaluate`` / ``disabled``). Only ``active``
      actually blocks pushes on the server. ``evaluate`` logs violations
      without blocking; ``disabled`` does nothing. The ``rules/branches/{b}``
      endpoint returns effective rules from BOTH ``active`` AND ``evaluate``
      rulesets, so "the rule shows up in effective rules" does NOT mean "the
      server will block". This check was added msg-1300 after the naysayer
      pointed out an ``evaluate``-mode ruleset would pass the earlier
      bypass-only check while providing no actual protection.

    * ``current_user_can_bypass == "never"`` — for the calling identity, the
      ruleset cannot be overridden. A ruleset with ``bypass="always"`` or
      ``"pull_requests"`` or the like is bypassable and does not enforce for
      us.

    The caller aggregates per-required-type — msg-1291 naysayer finding fixed
    the earlier one-shot ``raise`` shape, which halted on the first
    bypassable ruleset even when a strict ruleset on the same required type
    also existed.

    Always queries the repository endpoint
    (``repos/{owner}/{repo}/rulesets/{ruleset_id}``) regardless of
    ``source_type`` — the module docstring "Ruleset endpoint choice" block
    explains why (repo endpoint returns org-source rulesets under ``repo``
    scope; the ``orgs/`` endpoint would require ``admin:org`` which the loop
    token lacks). ``source_type`` and ``source`` come from the
    ``rules/branches/{b}`` response and are surfaced in the returned reason
    string so an operator can distinguish "the org guardrail is broken" from
    "a repo-local ruleset is misconfigured" without re-tracing the code.
    """
    origin = f"source_type={source_type!r} source={source!r}"
    try:
        rs = api_caller(f"repos/{owner}/{repo}/rulesets/{ruleset_id}")
    except Exception as exc:
        return False, (
            f"could not fetch ruleset {ruleset_id} ({origin}): {exc}. "
            f"Fail-closed (msg-1267 §4). If GitHub has tightened the "
            f"repos/rulesets endpoint to reject Organization-source rulesets, "
            f"the mitigation is a token upgrade to include admin:org scope + "
            f"routing by source_type — see the module docstring 'Ruleset "
            f"endpoint choice' block."
        )
    if not isinstance(rs, dict):
        return False, (
            f"ruleset {ruleset_id} ({origin}) response was not an object "
            f"(got {type(rs).__name__}); cannot verify enforcement/bypass."
        )
    enforcement = rs.get("enforcement")
    if enforcement != "active":
        return False, (
            f"ruleset {ruleset_id} ({origin}) has enforcement={enforcement!r}; "
            f"required 'active'. An 'evaluate'-mode ruleset only logs "
            f"violations — the server does NOT block pushes — so the loop "
            f"identity would be free to force-push despite the rule appearing "
            f"in the effective-rules response (msg-1300 naysayer finding)."
        )
    bypass = rs.get("current_user_can_bypass")
    if bypass != "never":
        return False, (
            f"ruleset {ruleset_id} ({origin}) has current_user_can_bypass="
            f"{bypass!r}; required 'never'. The loop's identity can bypass this "
            f"ruleset → the server-side protection it provides is unreliable "
            f"(msg-1265 §5 I-1: '執行者はサーバであってよい' → but only if the "
            f"server actually executes)."
        )
    return True, ""


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


def _default_git_toplevel(path: Path) -> Path | None:
    """Return the resolved git toplevel for ``path``, or ``None``.

    ``None`` covers every case where a toplevel cannot be established: the
    path is not inside any git working tree, ``git`` is not on PATH, the path
    does not exist, or the subprocess fails for any other reason. Callers
    treat ``None`` as "no shared-``.git`` concern applies" — a path with no
    git tree cannot walk upward into another repo's ``.git``. See the
    ``GitToplevel`` type alias for the msg-1296 rationale.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    top = proc.stdout.strip()
    if not top:
        return None
    try:
        return Path(top).resolve()
    except OSError:
        return None


__all__ = [
    "ApiCaller",
    "GitToplevel",
    "PreflightError",
    "RemoteReader",
    "preflight_gate",
]
