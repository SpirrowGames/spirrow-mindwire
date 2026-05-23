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
*loop-level* gate. Its hard guarantee is that the six statically-detectable
Tier C operations (``git.merge_to_main`` / ``force_push`` / ``history_rewrite``
/ ``fs.delete`` / ``drive.write`` / ``external.publish``) are denied. Branch /
path constraints on commit/push are best-effort over a parsed command line. The
*blast-radius* containment is the **environment** (Tailscale ACL + egress
default-deny + scoped credentials, ADR-07 §2.4 / env spec) — defence in depth,
not this module alone.
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
    # --- Tier C (forbidden, explicit) ---
    GIT_MERGE_TO_MAIN = "git.merge_to_main"
    FORCE_PUSH = "force_push"
    HISTORY_REWRITE = "history_rewrite"
    FS_DELETE = "fs.delete"
    DRIVE_WRITE = "drive.write"
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
    """A Tier A allow rule: operation + optional constraints (spec §B.3)."""

    operation: Operation
    path_glob: str | None = None
    branch_glob: tuple[str, ...] = ()
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
    """

    operation: Operation
    path: str | None = None
    branch: str | None = None
    source: str | None = None
    target: str | None = None
    force: bool = False
    detail: str = ""


class AllowlistConfigError(ValueError):
    """Raised when the allow-list YAML is malformed (fail-loud at load)."""


# --------------------------------------------------------------------------- #
# Glob helpers
# --------------------------------------------------------------------------- #


def _branch_matches(branch: str | None, globs: tuple[str, ...]) -> bool:
    if branch is None:
        return False
    return any(fnmatch.fnmatch(branch, g) for g in globs)


def _path_within_repo(path: str, repo_root: Path) -> bool:
    """True iff ``path`` resolves to a location inside ``repo_root``.

    SDK-reported paths may be absolute (cwd == repo_root) or relative; a
    relative path is anchored at ``repo_root``. ``resolve`` collapses ``..``
    so an escape (``<repo>/../etc``) lands outside and is rejected. ``..``
    escapes therefore fail closed.
    """
    root = repo_root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    return resolved == root or root in resolved.parents


def _match_path_glob(glob: str, path: str, repo_root: Path) -> bool:
    """Match ``path`` against a config ``path_glob`` (``<repo>`` substituted).

    ``<repo>/**`` (the spec's fs.write constraint) is interpreted as
    repo-containment rather than literal fnmatch, so any depth under the repo
    matches and ``..`` escapes fail closed. Other globs fall back to fnmatch.
    """
    substituted = glob.replace("<repo>", str(repo_root.resolve()))
    if substituted.endswith("/**"):
        return _path_within_repo(path, repo_root)
    return fnmatch.fnmatch(str(Path(path)), substituted)


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
            path_glob=raw.get("path_glob"),
            branch_glob=_tuple("branch_glob"),
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
        if rule.path_glob is not None:
            if action.path is None:
                return False, f"{op}: no path to check against {rule.path_glob!r}"
            if not _match_path_glob(rule.path_glob, action.path, self._repo_root):
                return (
                    False,
                    f"{op}: path {action.path!r} is outside the allowed "
                    f"glob {rule.path_glob!r} (fs.write is <repo>/** only)",
                )
        if rule.force is False and action.force:
            return False, f"{op}: force flag is not allowed (Tier C force_push)"
        if rule.branch_glob and not _branch_matches(action.branch, rule.branch_glob):
            # A None branch (could not be parsed) is allowed here: the classifier
            # upstream emits the explicit forbidden Operation for a main target.
            # An *explicitly named* branch outside the glob is denied.
            if action.branch is None:
                return True, ""
            return False, f"{op}: branch {action.branch!r} is outside {list(rule.branch_glob)}"
        if (
            rule.source_glob
            and action.source is not None
            and not _branch_matches(action.source, rule.source_glob)
        ):
            return False, f"{op}: source {action.source!r} is outside {list(rule.source_glob)}"
        if (
            rule.target_glob
            and action.target is not None
            and not _branch_matches(action.target, rule.target_glob)
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
