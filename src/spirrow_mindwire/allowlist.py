"""Stage 3 implementer allow-list — MINDWIRE_STAGE3_WIRING_ALLOWLIST_SPEC Part B.

An **operation-based, default-deny** allow-list that gates the implementer
adapter's ``EXECUTE_CODE`` actions (ADR-2026-05-23-07 §2.3). Every action the
implementer's SDK session attempts is classified into an :class:`Operation`
(the classifier is SDK-tool-specific and lives in
:mod:`spirrow_mindwire.adapters.implementer`); this module decides
**allow / deny BEFORE execution**. A deny is *fail-loud*: the adapter halts the
session (Stage 2 ``fail-loud no-fallback`` inherited, ADR-07 §2.3 / §2.6).

Two-axis model (spec §B.2): the implementer's world is **Tier A (allowed) +
Tier C (forbidden) only** — Tier B (naysayer review) is a post-PR gate, not an
implementer operation. So the config carries:

* **allow** rules (Tier A) with optional glob / flag constraints, and
* an explicit **forbidden** enumeration (Tier C) giving a concrete deny reason.

Anything matching neither is denied by ``default: deny`` ("unlisted operation").

Scope of guarantee (read this before trusting it): the allow-list is the
*loop-level* gate, and it is a **guardrail against accidents, not a boundary
against intent** — because ``exec.code`` is unconstrained, any invariant a rule
here purports to enforce is bypassable by a determined loop through
``python -c`` / ``bash -c`` / a script file that runs the forbidden verb.
The classifier's job is to catch a *slip* (the loop reaches for the obvious
verb in the obvious way), not to withstand a targeted evasion. The real
containment of intent lives elsewhere: the sandbox, the credential scope,
the GitHub org ruleset, the network boundary, and Takahito's manual merge to
``main``.

Its **two** hard classifier-executed guarantees against slips are
``git.merge_to_main`` and ``external.publish``, both denied unconditionally
from a name match alone (no HEAD read, no branch inspection). Every other
invariant lives outside this module:

* **"no push / force-push / ref-deletion reaches ``main``"** rides on the
  ``guard-default-branch`` GitHub org ruleset (id=21017016, active across the
  21 SpirrowGames repos as of 2026-08-19; ``current_user_can_bypass=never`` for
  the loop identity), which rejects the push at the server, and on the
  composition-root preflight in ``loop_runner`` (P0/P1/P2), which halts the
  daemon (SystemExit) if the target repo lacks that protection, if the loop is
  running against the daemon's own checkout, or if any remote's URL is not
  under ``https://github.com/SpirrowGames/`` (Tier-C decide 2026-08-19
  msg-1270, option β).

* **``fs.delete``** used to be a Tier-C forbidden here. It was REMOVED on
  2026-08-19 (T-drop-branch-prediction-from-allowlist §3) because ``exec.code``
  is unconstrained ∴ ``python -c "shutil.rmtree(...)"`` was always a way around
  the check — the classifier's ``fs.delete`` deny was theatre. The real
  containment is git's history (files are not gone until they leave a commit)
  and the sandbox filesystem itself. Same measurement retired ``fs.write``'s
  ``path_glob`` (identical bypass through ``exec.code``): ``fs.write`` is now
  unconstrained Tier A.

* **``drive.write``** was REMOVED for the opposite reason: it was unreachable.
  The implementer session runs with ``setting_sources=[]`` /
  ``strict_mcp_config=True`` and zero MCP tools; there is no Drive credential on
  this host. Keeping the entry would carry maintenance without carrying
  enforcement.

* **``external.publish``** was briefly removed on 2026-08-19 and RESTORED the
  same day (msg-1274, Takahito Tier-C decide) after PR #159's Tier-B naysayer
  measurement showed the removal's premise was false. The removal argued
  "squid egress default-deny is the actual boundary" from a probe of
  ``example.com`` / ``hooks.slack.com`` / ``api.telegram.org`` /
  ``discord.com`` — all denied at ``CONNECT``. But ``gh release create`` /
  ``gh repo archive`` reach ``api.github.com`` (endpoints
  ``POST /repos/{o}/{r}/releases`` and ``PATCH /repos/{o}/{r}``), which squid
  MUST allow through for ``gh pr create`` to work. The probe did not measure
  that path, and "only ``api.github.com`` passes" does NOT imply "publish is
  stopped" — the publish destination *is* that one allowed host. Classic PAT
  scope ``repo`` grants release-creation and repo-archive permission. So the
  classifier route is the actual guarantor here, restored to route
  ``gh release`` (all subcommands) and ``gh repo delete|archive`` to
  ``EXTERNAL_PUBLISH``; the raw-coarse mirror (direct == wrapped, T23) is
  restored alongside it.

* **``drive.write``** stays REMOVED for a reason with no such gap: the
  implementer session runs with ``setting_sources=[]`` /
  ``strict_mcp_config=True`` and zero MCP tools; there is no Drive credential
  on this host; ``api.github.com`` is not a Drive endpoint. The removal
  rationale ("unreachable, and unreachable rules skew judgement") holds; no
  ``gh release``-shaped bypass exists for it.

Two removals share one principle (msg-1272 §2, Takahito): a rule that is
not the actual guarantor of an invariant should not be kept "for insurance",
because a residual layer skews the next round of judgement — the next design
sees "we still have a check here" and does not reach for the layer that
actually holds. Where a check IS the guarantor (``git.merge_to_main`` — the
name-only classifier over ``gh pr merge`` / MCP ``merge_pull_request`` — and
``external.publish`` per the restoration above, because the network boundary
does not cover the one host it must allow), it stays.

The ``branch_glob`` rule key was also REMOVED on 2026-08-19 — the whole
branch-prediction enforcement path (``_current_branch`` / ``_push_destination`` /
``_enrich`` / the chain-guard / the destructive-branch classifier) came out of
``adapters/implementer`` at the same time. Reintroducing ``branch_glob`` as a
config key would be a silent fail-OPEN today: with no enrichment,
``_constraints_pass`` would return True for every ``branch is None`` action —
so the key would look enforcing while enforcing nothing. Removing the code that
reads the key (rather than "supporting" it in name only) is what makes that
regression path visibly break at load time instead of silently at run time. The
same rule applies to ``path_glob`` (only ``fs.write`` used it, and ``fs.write``
is now unconstrained): keeping the key alive without a consumer is dead
scaffolding, and ``_parse_allow_rule`` refuses it at load time.

The *blast-radius* containment is the **environment** (Tailscale ACL + egress
default-deny + scoped credentials + captive clones, ADR-07 §2.4 / env spec) +
the **GitHub org ruleset** — defence in depth, not this module alone.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------- #
# Operations (the SDK-agnostic abstraction the spec's allow-list is written in)
# --------------------------------------------------------------------------- #


class Operation(StrEnum):
    """An operation an implementer action maps to (spec §B.3).

    Tier A allowed operations and the Tier C forbidden operations share one
    enum so the classifier can name a forbidden action explicitly (giving a
    concrete fail-loud reason rather than a bare "unlisted").
    """

    # --- Tier A (allowed, possibly constrained) ---
    EXEC_CODE = "exec.code"
    FS_WRITE = "fs.write"
    FS_READ = "fs.read"
    SEARCH = "search"
    GIT_COMMIT = "git.commit"
    GIT_PUSH = "git.push"
    GIT_MERGE = "git.merge"
    GITHUB_PR_OPEN = "github.pr.open"
    GITHUB_READ = "github.read"
    FORCE_PUSH = "force_push"
    HISTORY_REWRITE = "history_rewrite"
    # --- Tier C (forbidden, explicit) ---
    #
    # Two Tier-C forbidden operations as of msg-1274's restoration:
    #   GIT_MERGE_TO_MAIN — the `gh pr merge` / MCP merge_pull_request name match.
    #   EXTERNAL_PUBLISH  — the `gh release` (all subcommands) / `gh repo
    #                       delete|archive` route + the `<pkgmgr> publish|push`
    #                       raw pattern. Restored on 2026-08-19 after PR #159's
    #                       naysayer showed the "squid stops publish" premise
    #                       was false for the one host squid must allow through
    #                       (`api.github.com`) — see the module docstring.
    #
    # FS_DELETE and DRIVE_WRITE stay removed from the enum (module docstring):
    # FS_DELETE was theatre against the `exec.code` bypass, DRIVE_WRITE has no
    # reachable path from this host.
    GIT_MERGE_TO_MAIN = "git.merge_to_main"
    EXTERNAL_PUBLISH = "external.publish"
    # --- fallback ---
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Decision + config rule shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AllowlistDecision:
    """The outcome of :meth:`Allowlist.check`.

    ``reason`` is human-readable; on a deny it is the fail-loud rejection
    reason surfaced to the SDK permission layer and the halt error.
    """

    allowed: bool
    operation: Operation
    reason: str


@dataclass(frozen=True)
class _AllowRule:
    """A Tier A allow rule: operation + optional constraints (spec §B.3).

    ``branch_glob`` and ``path_glob`` were removed on 2026-08-19
    (T-drop-branch-prediction-from-allowlist §3): the module docstring explains
    the "no dead scaffolding" principle. With no HEAD enrichment upstream, a
    reintroduced ``branch_glob`` would silently fail-OPEN on every ``branch is
    None`` action; with ``fs.write`` unconstrained (the sandbox is what bounds
    the filesystem, not this file), ``path_glob`` has no consumer either.
    ``_parse_allow_rule`` refuses both keys at load time (fail-loud) so a
    config-only reintroduction cannot look enforcing while enforcing nothing.

    ``target_glob`` remains — the current YAML still uses it for
    ``github.pr.open`` (out of scope for the 2026-08-19 change, msg-1265 §9,
    and a per-project deploy declaration is the permanent fix rather than a
    repo-agnostic glob here). ``source_glob`` is unused by the current YAML
    but the parser still accepts it (a peer of ``target_glob`` at zero
    marginal cost); if it stays unused across the next iteration it should
    follow ``path_glob`` out under the same "no dead scaffolding" rule.
    """

    operation: Operation
    source_glob: tuple[str, ...] = ()
    target_glob: tuple[str, ...] = ()
    force: bool | None = None  # if False, the action's force flag must be falsy


@dataclass(frozen=True)
class _ForbiddenRule:
    """A Tier C forbidden rule: operation + concrete deny reason (spec §B.3)."""

    operation: Operation
    reason: str


# --------------------------------------------------------------------------- #
# Classified action (filled by the SDK-tool classifier, consumed by check)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClassifiedAction:
    """One implementer action, mapped to an :class:`Operation` + parameters.

    Produced by the SDK-tool classifier (``adapters.implementer``); ``check``
    consumes it. ``detail`` carries the raw command / path for messages.

    The trailing four fields are *provenance*, not inputs to the verdict: ``check``
    never reads them, so setting them cannot change an allow/deny outcome. They exist
    because a denial used to report which rule fired and nothing about the act that
    tripped it (see ``spec/design/T-denial-detail-and-overdeny.md``):

    ``rule_id``
        which classifier produced the verdict — ``structural`` / ``raw_coarse`` /
        ``mcp`` / ``path``. Distinguishing these is the whole diagnostic question:
        ``raw_coarse`` matches the *raw text*, so it can fire on a command that
        merely mentions a Tier C verb, while ``structural`` means the command really
        parsed to that operation.
    ``corroborated``
        ``"yes"`` / ``"no"`` / ``"unknown"`` — whether the structural pass
        independently reached the same danger as the coarse floor. Not a bool: the
        third state is real (tokenizer degraded, or the floor never ran) and folding
        it into a bool would invent a certainty we do not have.
    ``match_offset``
        index into ``detail`` where the deciding match started; ``-1`` when the
        verdict carries no offset.
    ``indirection_gate``
        whether ``_INDIRECTION_RE`` fired, i.e. whether the coarse floor was even
        eligible to run.
    """

    operation: Operation
    path: str | None = None
    # `branch` is retained on the action even though no rule reads it: denial records
    # and log lines still surface it when the classifier extracted one from the
    # command (e.g. `git push --force origin main` — the classifier still names the
    # branch for provenance, though the enforcement no longer turns on it).
    branch: str | None = None
    source: str | None = None
    target: str | None = None
    force: bool = False
    detail: str = ""
    rule_id: str = ""
    corroborated: str = ""
    match_offset: int = -1
    indirection_gate: bool = False


class AllowlistConfigError(ValueError):
    """Raised when the allow-list YAML is malformed (fail-loud at load)."""


# --------------------------------------------------------------------------- #
# Glob helpers
# --------------------------------------------------------------------------- #


def _glob_matches(value: str | None, globs: tuple[str, ...]) -> bool:
    """True iff ``value`` matches any glob in ``globs``.

    A ``None`` value returns False here. Every call site in ``_constraints_pass``
    guards on ``is not None`` first, so that path is unreachable from there; it
    stays because the signature admits ``None``. (Tier B, #163: this used to
    point at a note in ``_constraints_pass`` that this change deleted.)
    """
    if value is None:
        return False
    return any(fnmatch.fnmatch(value, g) for g in globs)


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #


class Allowlist:
    """Default-deny, operation-based allow-list (spec §B).

    Construct via :meth:`from_yaml` / :meth:`from_mapping` / :func:`default_allowlist`
    (which read the §B.3 config) or directly with rule lists for tests.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        allow_rules: list[_AllowRule],
        forbidden_rules: list[_ForbiddenRule],
        default_deny: bool = True,
    ) -> None:
        self._repo_root = repo_root
        self._allow = list(allow_rules)
        self._forbidden = {r.operation: r for r in forbidden_rules}
        self._default_deny = default_deny

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def from_yaml(cls, path: str | Path, *, repo_root: str | Path) -> Allowlist:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return cls.from_mapping(data, repo_root=repo_root)

    @classmethod
    def from_mapping(cls, data: Any, *, repo_root: str | Path) -> Allowlist:
        if not isinstance(data, dict):
            raise AllowlistConfigError(
                f"allow-list config must be a mapping, got {type(data).__name__}"
            )
        default = str(data.get("default", "deny")).lower()
        if default not in ("deny", "allow"):
            raise AllowlistConfigError(f"unknown default policy {default!r} (expected deny/allow)")

        allow_rules: list[_AllowRule] = []
        for raw in data.get("allow", []) or []:
            allow_rules.append(cls._parse_allow_rule(raw))
        forbidden_rules: list[_ForbiddenRule] = []
        for raw in data.get("forbidden", []) or []:
            forbidden_rules.append(cls._parse_forbidden_rule(raw))
        return cls(
            repo_root=Path(repo_root),
            allow_rules=allow_rules,
            forbidden_rules=forbidden_rules,
            default_deny=(default == "deny"),
        )

    @staticmethod
    def _parse_operation(value: Any, *, context: str) -> Operation:
        try:
            return Operation(str(value))
        except ValueError as exc:
            raise AllowlistConfigError(f"unknown operation {value!r} in {context}") from exc

    @classmethod
    def _parse_allow_rule(cls, raw: Any) -> _AllowRule:
        if not isinstance(raw, dict) or "operation" not in raw:
            raise AllowlistConfigError(f"allow rule must be a mapping with 'operation': {raw!r}")
        op = cls._parse_operation(raw["operation"], context="allow")

        # Fail-loud on removed keys (T-drop-branch-prediction-from-allowlist §3,
        # 2026-08-19). Silently ignoring either would let a config-only reintroduction
        # look enforcing while the missing enrichment / constraint code makes it fail-OPEN.
        for retired in ("branch_glob", "path_glob"):
            if retired in raw:
                raise AllowlistConfigError(
                    f"{retired} was removed on 2026-08-19 "
                    f"(T-drop-branch-prediction-from-allowlist §3); "
                    f"the guarantees that used to ride here live outside this module now — "
                    f"the GitHub org ruleset + composition-root preflight (P0/P1/P2) for the "
                    f"branch predicate, and the sandbox filesystem for path scope. "
                    f"See the module docstring. Offending rule: {raw!r}"
                )

        def _tuple(key: str) -> tuple[str, ...]:
            val = raw.get(key)
            if val is None:
                return ()
            if isinstance(val, str):
                return (val,)
            if isinstance(val, list):
                return tuple(str(v) for v in val)
            raise AllowlistConfigError(f"{key} must be a string or list: {val!r}")

        return _AllowRule(
            operation=op,
            source_glob=_tuple("source_glob"),
            target_glob=_tuple("target_glob"),
            force=raw.get("force"),
        )

    @classmethod
    def _parse_forbidden_rule(cls, raw: Any) -> _ForbiddenRule:
        if not isinstance(raw, dict) or "operation" not in raw:
            raise AllowlistConfigError(
                f"forbidden rule must be a mapping with 'operation': {raw!r}"
            )
        op = cls._parse_operation(raw["operation"], context="forbidden")
        reason = str(raw.get("reason", f"{op.value} is forbidden (Tier C)"))
        return _ForbiddenRule(operation=op, reason=reason)

    # -- the gate ----------------------------------------------------------- #

    def check(self, action: ClassifiedAction) -> AllowlistDecision:
        """Decide allow / deny for a classified action (fail-loud on deny).

        Order: (1) explicit Tier C forbidden → deny; (2) a matching Tier A
        allow rule whose constraints all pass → allow; (3) an allow rule for
        the operation exists but its constraints fail → deny (constraint
        violation, naming the constraint); (4) otherwise default-deny.
        """
        op = action.operation

        # (1) Explicit Tier C forbidden.
        forbidden = self._forbidden.get(op)
        if forbidden is not None:
            return AllowlistDecision(False, op, forbidden.reason)

        # (2)/(3) Tier A allow rules for this operation.
        rules = [r for r in self._allow if r.operation == op]
        if rules:
            last_reason = ""
            for rule in rules:
                ok, reason = self._constraints_pass(rule, action)
                if ok:
                    return AllowlistDecision(True, op, "allowed (Tier A)")
                last_reason = reason
            return AllowlistDecision(False, op, last_reason)

        # (4) Unlisted operation under default-deny.
        if self._default_deny:
            return AllowlistDecision(
                False, op, f"operation {op.value!r} is not in the allow-list (default: deny)"
            )
        return AllowlistDecision(True, op, "allowed (default: allow)")

    def _constraints_pass(self, rule: _AllowRule, action: ClassifiedAction) -> tuple[bool, str]:
        """Check one allow rule's constraints against an action."""
        op = action.operation.value
        if rule.force is False and action.force:
            return False, f"{op}: force flag is not allowed (Tier C force_push)"
        if (
            rule.source_glob
            and action.source is not None
            and not _glob_matches(action.source, rule.source_glob)
        ):
            return False, f"{op}: source {action.source!r} is outside {list(rule.source_glob)}"
        if (
            rule.target_glob
            and action.target is not None
            and not _glob_matches(action.target, rule.target_glob)
        ):
            return False, f"{op}: target {action.target!r} is outside {list(rule.target_glob)}"
        return True, ""

    @property
    def repo_root(self) -> Path:
        return self._repo_root


# --------------------------------------------------------------------------- #
# Packaged default config (spec §B.3)
# --------------------------------------------------------------------------- #

_DEFAULT_CONFIG_RESOURCE = ("spirrow_mindwire.adapters", "implementer_allowlist.yaml")


def default_allowlist(repo_root: str | Path) -> Allowlist:
    """Load the packaged §B.3 allow-list config, anchored at ``repo_root``."""
    from importlib.resources import files

    pkg, name = _DEFAULT_CONFIG_RESOURCE
    text = files(pkg).joinpath(name).read_text(encoding="utf-8")
    return Allowlist.from_mapping(yaml.safe_load(text), repo_root=repo_root)


__all__ = [
    "Allowlist",
    "AllowlistConfigError",
    "AllowlistDecision",
    "ClassifiedAction",
    "Operation",
    "default_allowlist",
]
