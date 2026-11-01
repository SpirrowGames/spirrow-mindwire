"""Which naysayer tier answers a request — the ONE decision point (ADR-14 §7.3 / §7.5 / §7.6).

A naysayer request goes either to the codex tier (``naysayer``: codex primary,
gemini-fallback backup) or to the Gemini-only tier (``naysayer-gemini``). The codex
path is a personal ChatGPT Pro subscription and has **no ZDR**; ADR-15 C-2 keeps
"ZDR is mandatory" for the N-3 subset, so §7.3 requires that this function never
returns the codex tier for a request it can identify as N-3.

:func:`route_tier` is the only code that makes that choice, and the attestation's
accepted backends are derived from the same :class:`TierDecision` (§7.6: two
places deciding would be dual management). It is pure; the I/O that feeds it is
:func:`load_n3_globs` (config) and the caller's own reads (labels / tags /
changed or referenced files / prompt size).

How the N-3 subset is identified (thread T-D8-codex-backend-adr14-15-amendment,
Bohr msg-6471 → msg-6479, Einstein msg-6472 → msg-6480). Any ONE of these routes
to Gemini:

1. **Marker** — :data:`~.principles.N3_SENSITIVE_MARKER` as a PR label or a thread
   tag. A marker the caller *could not read* (``None``) also routes to Gemini.
2. **The config file itself** — any input path equal to
   :data:`~.principles.N3_CONFIG_PATH`, unconditionally and before any glob is
   evaluated: the file that defines the boundary is security configuration.
3. **Path globs** — :data:`~.principles.N3_SENSITIVE_PATHS_KEY` from that file.
   The globs are the UNION of a *trusted* side (PR base / remote default branch:
   always enforced, so a change cannot loosen the rules it is judged by) and a
   *working* side (PR head / the prompt builder's source: may only ADD globs).
4. **Unresolvable config** — trusted side unreadable, file or key missing there,
   either side malformed, or (design time) no repository root. Forgetting the key
   never sends anything to codex; only an explicitly written ``[]`` means "no
   sensitive paths".
5. **Path boundary** (msg-6575 → msg-6579) — a path that cannot be placed inside
   the repository. The PR-gate checks GitHub's file list *lexically*
   (:func:`lexical_path_problem`, no disk: a deleted file is just a path) and counts
   ``previous_filename`` for renames and copies; design time resolves each reference
   against the realpath of the git top level (``n3_design_time``, wired in the PR
   stacked on this one: ``~``, relative-to-summon-dir, symlinks followed, must exist,
   must be under the root). Either way the matcher sees root-relative POSIX paths.
6. **Incomplete file list** (msg-6573 (b)) — the PR-gate could not be sure it saw
   every changed file.

Plus §7.5: a prompt over the codex tier's limit goes to Gemini.

Residual, authorised (Takahito msg-6563, TIER-C goal, answering Einstein
msg-6482): a vulnerability that the review itself discovers, on a path nobody
listed and without a marker, cannot be classified in advance and reaches the codex
tier.

Paths are normalised before ANY comparison — ``\\`` → ``/``, leading ``./`` and
``/`` dropped, case-folded — and so are the globs (Einstein msg-6482 advisory).
Folding case can only widen a match, i.e. push toward Gemini, which is the safe
direction. Globs use :func:`fnmatch.fnmatchcase` semantics, where ``*`` also
crosses ``/``; again the over-matching direction.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .principles import (
    CODEX_TIER_PROMPT_CHAR_LIMIT,
    N3_CONFIG_PATH,
    N3_SENSITIVE_MARKER,
    N3_SENSITIVE_PATHS_KEY,
    NAYSAYER_GEMINI_TIER,
    NAYSAYER_MODEL_TIER,
    allowed_backends,
)

logger = logging.getLogger(__name__)

ConfigReader = Callable[[], Awaitable[str | None]]
"""Reads :data:`N3_CONFIG_PATH` at one side: its text, ``None`` if absent; raises if unreadable."""


def normalize_path(path: str) -> str:
    """Normalise a repo-relative path (or glob) for comparison. See the module docstring."""
    text = path.strip().replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/").casefold()


_NORMALIZED_CONFIG_PATH = normalize_path(N3_CONFIG_PATH)


@dataclass(frozen=True)
class GlobLoad:
    """The union of both sides' globs, or why it could not be formed.

    ``globs is None`` means "unresolvable" — :func:`route_tier` sends the request to
    Gemini and ``failure`` says why (``trusted_unresolved`` marks the case a caller
    must also notify about, msg-6477: a persistent resolution failure must not
    quietly become "always Gemini").
    """

    globs: frozenset[str] | None
    failure: str | None = None
    trusted_unresolved: bool = False


@dataclass(frozen=True)
class _Side:
    globs: tuple[str, ...] | None  # None: file or key absent
    malformed: str | None = None


def _parse(text: str | None) -> _Side:
    if text is None:
        return _Side(globs=None)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return _Side(globs=None, malformed=f"not valid TOML: {exc}")
    if N3_SENSITIVE_PATHS_KEY not in data:
        return _Side(globs=None)
    raw = data[N3_SENSITIVE_PATHS_KEY]
    if not isinstance(raw, list) or not all(isinstance(g, str) and g.strip() for g in raw):
        return _Side(
            globs=None,
            malformed=f"{N3_SENSITIVE_PATHS_KEY} is not a list of non-empty strings: {raw!r}",
        )
    return _Side(globs=tuple(normalize_path(g) for g in raw))


async def load_n3_globs(*, trusted: ConfigReader, working: ConfigReader | None) -> GlobLoad:
    """Return the union of the trusted and working globs, failing safe per side (msg-6479).

    The ONE loader for both request types; they differ only in what the readers read:

    ============  =========================  ====================================
    request       trusted (always enforced)  working (may only add)
    ============  =========================  ====================================
    PR-gate       PR base ref                PR head
    design-time   remote default branch      the prompt builder's source (§7.5)
    ============  =========================  ====================================

    - trusted unreadable / file absent / key absent / malformed → unresolvable
      (``trusted_unresolved``);
    - working unreadable / malformed → unresolvable (it can only push toward Gemini);
    - working file or key absent → contributes nothing; the trusted side decides;
    - ``working=None`` → no working side.
    """
    try:
        trusted_side = _parse(await trusted())
    except Exception as exc:  # any read failure is "could not resolve", never "no globs"
        return GlobLoad(None, f"trusted config unreadable: {exc}", trusted_unresolved=True)
    if trusted_side.malformed:
        return GlobLoad(
            None, f"trusted config malformed: {trusted_side.malformed}", trusted_unresolved=True
        )
    if trusted_side.globs is None:
        return GlobLoad(
            None,
            f"trusted config has no {N3_CONFIG_PATH} or no {N3_SENSITIVE_PATHS_KEY} key",
            trusted_unresolved=True,
        )
    globs = set(trusted_side.globs)
    if working is not None:
        try:
            working_side = _parse(await working())
        except Exception as exc:
            return GlobLoad(None, f"working config unreadable: {exc}")
        if working_side.malformed:
            return GlobLoad(None, f"working config malformed: {working_side.malformed}")
        globs.update(working_side.globs or ())
    return GlobLoad(frozenset(globs))


@dataclass(frozen=True)
class N3Request:
    """Everything :func:`route_tier` decides on.

    ``marker``: the label/tag was read and present (``True``), read and absent
    (``False``), or not read (``None`` → Gemini). ``paths``: the changed files
    (PR-gate) or the referenced files (design-time), unnormalised.
    ``prompt_chars``: the size of the prompt built for the codex tier.
    """

    marker: bool | None
    paths: tuple[str, ...]
    globs: GlobLoad
    prompt_chars: int
    # ``False``: the caller cannot be sure ``paths`` is every changed file (GitHub's PR files
    # endpoint stops at 3000) → Gemini (msg-6573 (b), test 19).
    paths_complete: bool = True
    # Set by the design-time resolver when a referenced path is out of tree or unresolvable
    # (msg-6577) → Gemini, before anything is matched.
    path_problem: str | None = None


@dataclass(frozen=True)
class TierDecision:
    """Where a request goes, who may answer it there, and why."""

    tier: str
    allowed_backends: frozenset[str]
    reason: str


def _gemini(reason: str) -> TierDecision:
    return TierDecision(NAYSAYER_GEMINI_TIER, allowed_backends(NAYSAYER_GEMINI_TIER), reason)


def lexical_path_problem(path: str) -> str | None:
    """Why ``path`` is not a plain repo-relative path, or ``None`` (msg-6579, PR-gate branch).

    No disk access: a PR's file list describes the PR, not this host's tree (a deleted file
    does not exist on disk and must not fail closed for that). A path that is absolute,
    carries a drive letter, or has a ``..`` segment cannot be placed inside the repository,
    so its request goes to Gemini. :func:`route_tier` applies it to EVERY path, so it is also
    a no-op guard on design-time paths the resolver already made root-relative.
    """
    text = path.strip().replace("\\", "/")
    if not text:
        return "empty path"
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return f"{path!r} is absolute"
    if ".." in text.split("/"):
        return f"{path!r} has a '..' segment"
    return None


def route_tier(request: N3Request) -> TierDecision:
    """Decide the tier for one request. The only decision point (§7.6).

    Order: marker → path boundary (the design-time resolver's problem, then the lexical
    check on every path) → incomplete file list → the config file itself → unresolvable
    config → globs → prompt size. Every path is normalised before BOTH the config-file
    match and the glob match (Einstein msg-6482).
    """
    if request.marker is None:
        return _gemini(f"{N3_SENSITIVE_MARKER} marker not read")
    if request.marker:
        return _gemini(f"{N3_SENSITIVE_MARKER} marker")
    if request.path_problem is not None:
        return _gemini(f"path outside the repository boundary: {request.path_problem}")
    for raw in request.paths:
        problem = lexical_path_problem(raw)
        if problem is not None:
            return _gemini(f"path outside the repository boundary: {problem}")
    if not request.paths_complete:
        return _gemini("changed-file list may be incomplete")
    paths = [normalize_path(p) for p in request.paths]
    if _NORMALIZED_CONFIG_PATH in paths:
        return _gemini(f"touches {N3_CONFIG_PATH}")
    if request.globs.globs is None:
        return _gemini(f"n3 config unresolved: {request.globs.failure}")
    for path in paths:
        for glob in sorted(request.globs.globs):
            if fnmatchcase(path, glob):
                return _gemini(f"{path} matches {N3_SENSITIVE_PATHS_KEY} glob {glob!r}")
    if request.prompt_chars > CODEX_TIER_PROMPT_CHAR_LIMIT:
        return _gemini(
            f"prompt {request.prompt_chars} chars > codex tier limit {CODEX_TIER_PROMPT_CHAR_LIMIT}"
        )
    return TierDecision(NAYSAYER_MODEL_TIER, allowed_backends(NAYSAYER_MODEL_TIER), "codex tier")


UnresolvedNotifier = Callable[[str], Awaitable[None]]
"""Receives a one-line description when the TRUSTED side cannot be resolved (msg-6477)."""


async def notify_unresolved(
    globs: GlobLoad, where: str, notifier: UnresolvedNotifier | None
) -> None:
    """Log at WARNING with the stable token ``n3-routing-unresolved``, then call ``notifier``.

    Fires only for a trusted-side failure. A persistent one would otherwise turn every
    request into a Gemini request without anyone noticing (msg-6477). A failing notifier is
    logged and swallowed: the routing decision (Gemini) is already safe.
    """
    if not globs.trusted_unresolved:
        return
    line = f"n3-routing-unresolved: {where}: {globs.failure}; routing to the Gemini tier"
    logger.warning("%s", line)
    if notifier is not None:
        try:
            await notifier(line)
        except Exception:
            logger.exception("n3-routing-unresolved notifier failed")


def has_marker(names: Iterable[str]) -> bool:
    """True iff ``names`` (PR labels or thread tags) carries :data:`N3_SENSITIVE_MARKER`."""
    return any(name.strip().casefold() == N3_SENSITIVE_MARKER for name in names)


__all__ = [
    "ConfigReader",
    "GlobLoad",
    "N3Request",
    "TierDecision",
    "UnresolvedNotifier",
    "has_marker",
    "lexical_path_problem",
    "load_n3_globs",
    "normalize_path",
    "notify_unresolved",
    "route_tier",
]
