"""Thread branch names and the push-target guard (ADR-2026-10-09-23 D7 / D8, design v1.2).

Two pure pieces, kept apart from the hook so they can be tested without git:

- :func:`normalize_stem` / :func:`generation_name` — the mechanical name ``loop/<thread_id>``
  (D8). Lowercase; every character outside ``[a-z0-9._-]`` becomes ``-``; runs of ``-`` collapse
  to one; runs of ``.`` collapse to one (git forbids ``..`` anywhere in a ref name, so an id
  carrying one would otherwise park its thread on every turn); leading / trailing ``-`` and ``.``
  are stripped. A stem longer than
  :data:`STEM_MAX_LEN` keeps its first :data:`STEM_KEEP_LEN` characters (minus any trailing ``-``,
  so the single-``-`` rule still holds), then ``-`` and the first
  :data:`HASH_LEN` hex digits of the sha256 of the thread id (the original, so two ids that
  normalise to the same long prefix still get different names). Generation ``n >= 2`` appends
  ``-n``. Whether the result is a legal ref is decided by ``git check-ref-format``, by the hook,
  not re-implemented here.
- :class:`ThreadBranch` — the only type the hook's push accepts (D7: "main / develop へは構造上
  push できないようにする", design B-5). Its constructor refuses any name outside ``loop/`` unless
  the name came from ``[turn.git.branch_override]``, and refuses the default branch and the
  protected names below **even then**. The GitHub ruleset covers only ``~DEFAULT_BRANCH``
  (preflight.py P2-2(2)), not develop, so this guarantee lives in code.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

BRANCH_PREFIX = "loop/"
STEM_MAX_LEN = 64
STEM_KEEP_LEN = 55
HASH_LEN = 8

#: Refused as a push target whatever their origin (design v1.2 B-5).
PROTECTED_BRANCH_NAMES: frozenset[str] = frozenset(
    {"main", "master", "develop", "production", "gh-pages"}
)
PROTECTED_BRANCH_PREFIXES: tuple[str, ...] = ("release/",)

_DISALLOWED = re.compile(r"[^a-z0-9._-]")
_DASH_RUN = re.compile(r"-{2,}")
_DOT_RUN = re.compile(r"\.{2,}")


def normalize_stem(thread_id: str) -> str:
    """The name part of ``loop/<...>`` for ``thread_id`` (D8). May be empty or ref-illegal."""
    stem = _DISALLOWED.sub("-", thread_id.lower())
    # Collapse ``.`` runs before the strip: ``..`` is ref-illegal (PR #484 review advisory).
    stem = _DOT_RUN.sub(".", _DASH_RUN.sub("-", stem)).strip("-.")
    if len(stem) > STEM_MAX_LEN:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:HASH_LEN]
        # rstrip: a cut that lands on a ``-`` must not leave ``--`` before the digest.
        stem = f"{stem[:STEM_KEEP_LEN].rstrip('-')}-{digest}"
    return stem


def generation_name(stem: str, n: int) -> str:
    """``loop/<stem>`` for ``n == 1``, ``loop/<stem>-<n>`` for ``n >= 2``."""
    if n < 1:
        raise ValueError(f"generation must be >= 1, got {n}")
    return f"{BRANCH_PREFIX}{stem}" if n == 1 else f"{BRANCH_PREFIX}{stem}-{n}"


class ProtectedBranchError(ValueError):
    """A :class:`ThreadBranch` was asked to name a branch the loop must never push to."""


def is_protected(name: str, *, default_branch: str) -> bool:
    lowered = name.lower()
    return (
        lowered == default_branch.lower()
        or lowered in PROTECTED_BRANCH_NAMES
        or any(lowered.startswith(p) for p in PROTECTED_BRANCH_PREFIXES)
    )


@dataclass(frozen=True)
class ThreadBranch:
    """A branch name the hook may switch to and push. Validated at construction (B-5).

    ``overridden`` is ``True`` only for a name taken from ``[turn.git.branch_override]``: such a
    name may lie outside ``loop/`` (an in-flight thread keeps its current branch, D8), but it is
    still refused when it is the default branch or a protected name.
    """

    name: str
    default_branch: str
    overridden: bool = False

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ProtectedBranchError(f"invalid thread branch name {self.name!r}")
        if self.name.startswith(("refs/", "-")):
            raise ProtectedBranchError(f"thread branch must be a short branch name: {self.name!r}")
        if not self.overridden and not self.name.startswith(BRANCH_PREFIX):
            raise ProtectedBranchError(
                f"thread branch {self.name!r} is outside {BRANCH_PREFIX!r} and is not an override"
            )
        if is_protected(self.name, default_branch=self.default_branch):
            raise ProtectedBranchError(
                f"refusing {self.name!r} as a thread branch: it is the default branch "
                f"({self.default_branch!r}) or a protected name"
            )

    @property
    def refspec(self) -> str:
        """The explicit push refspec — never left to ``push.default`` (B-5)."""
        return f"refs/heads/{self.name}:refs/heads/{self.name}"


__all__ = [
    "BRANCH_PREFIX",
    "HASH_LEN",
    "PROTECTED_BRANCH_NAMES",
    "PROTECTED_BRANCH_PREFIXES",
    "STEM_KEEP_LEN",
    "STEM_MAX_LEN",
    "ProtectedBranchError",
    "ThreadBranch",
    "generation_name",
    "is_protected",
    "normalize_stem",
]
