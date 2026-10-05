"""Naysayer principles SOT loader + preamble builder (ADR-2026-06-03-17 D-1).

The 5 principles are defined **once**, in ``spec/NAYSAYER_PRINCIPLES.md``
(versioned). This module is the single programmatic entry point that reads that
file **verbatim** and assembles the preamble injected into every naysayer
invocation (design-time relay and the PR-gate alike). Principles are never
restated as Python string literals here — a one-place edit to the markdown
propagates to all injections (D-1 "常時注入").

The naysayer tier names, the backends each may be answered by, and the N-3 routing names
(ADR-14 §7.3 / §7.6) are pinned here too, so the adapters/driver import them from one place
instead of hardcoding them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

# ADR-14 §7.6: the independent naysayer runs behind one of TWO Lexora tiers, and which one a
# request goes to is decided per request by
# :func:`spirrow_mindwire.naysayer.n3_routing.route_tier` — never here, never at a call site.
#
# - ``naysayer`` — Lexora's fallback wrapper: ``codex`` primary, ``gemini-fallback`` backup.
#   The name is the pre-existing one on purpose, so an existing caller lands on the codex tier.
# - ``naysayer-gemini`` — Gemini only. Serves the N-3 subset (ADR-14 §7.3, ADR-15 C-2: ZDR is
#   mandatory there and the ChatGPT Pro codex path has none) and requests over the codex
#   tier's prompt limit (§7.5).
NAYSAYER_MODEL_TIER = "naysayer"
NAYSAYER_GEMINI_TIER = "naysayer-gemini"
NAYSAYER_UPSTREAM_MODEL = "gemini-3.1-pro-preview"

# The values a gateway accounting row's ``backend`` column may carry (msg-953 §3: the row's
# ``backend`` names the backend Lexora routed to, not the upstream model id — comparing against
# :data:`NAYSAYER_UPSTREAM_MODEL` would fail every attestation; measured against the live
# gateway, row 6032, 2026-08-13).
#
# ``gemini-fallback`` is a CONSTANT, not configuration (thread msg-6471 gap (d), Einstein
# msg-6469 advisory 1): the Lexora fallback wrapper must write exactly this string, and a value
# configurable on either side would be a second copy free to drift from this one.
CODEX_BACKEND = "codex"
GEMINI_FALLBACK_BACKEND = "gemini-fallback"
GEMINI_BACKEND = "gemini"

# ADR-14 §7.6: the backends an attestation accepts, per tier. The ONE table both the routing
# decision and the attestation read, so "where did we send it" and "who may answer it" cannot
# be decided in two places. A row whose ``backend`` is outside its tier's set fails closed.
TIER_ALLOWED_BACKENDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        NAYSAYER_MODEL_TIER: frozenset({CODEX_BACKEND, GEMINI_FALLBACK_BACKEND}),
        NAYSAYER_GEMINI_TIER: frozenset({GEMINI_BACKEND}),
    }
)

# ADR-14 §7.3 — how the N-3 subset is identified (thread msg-6471..6479, Einstein msg-6480).
# Each name is defined here ONCE; ``tests/test_n3_routing.py`` pins that the routing module,
# the config loader and docs/deploy.md use these constants rather than restating them.
#
# The opt-in marker: a PR label (PR-gate) or a thread tag (design-time) with this name.
N3_SENSITIVE_MARKER = "n3-sensitive"
# The per-HOST_REPO config file. Any request touching it is N-3 unconditionally (it defines the
# boundary itself). Thread msg-6475 named ``.mindwire/n3.toml``; that directory is the loop's
# own git-ignored state (the dispatcher's ``.mindwire/pin``; ``info/exclude``) and may not carry
# a tracked file, so the file sits beside ``.mindwire-gate`` at the repo root instead.
N3_CONFIG_PATH = ".mindwire-n3.toml"
# The key inside :data:`N3_CONFIG_PATH`: a list of globs over repo-relative POSIX paths.
N3_SENSITIVE_PATHS_KEY = "n3_sensitive_paths"

# ADR-14 §7.5: the codex tier's prompt limit, in characters, counted as one character per token
# (conservative for Japanese, the worst case for ordinary text). The rule (thread msg-6471 §3) is
# ``context_window - max_tokens`` of the model entry Lexora pins in ``model_catalog_json``.
# PROVISIONAL: Lexora develop (fbbfdea, 2026-10-04) pins no catalog yet, so this is the
# Gemini-tier value (pr_review._MAX_DIFF_CHARS) until the shadow comparison measures codex.
# Heuristic: dense hex/emoji payloads can overflow the token window first; codex then errors
# and the Lexora fallback wrapper answers with gemini-fallback (§7.4) — tracked as part of the
# fallback-frequency observation (§7.3).
CODEX_TIER_PROMPT_CHAR_LIMIT = 150_000


def allowed_backends(tier: str) -> frozenset[str]:
    """Return the backends an attestation accepts for ``tier`` (ADR-14 §7.6), fail-loud.

    An unknown tier is a :class:`PrinciplesError`, never an empty or permissive set: a tier
    with no row in :data:`TIER_ALLOWED_BACKENDS` has no defined answerer, so nothing can attest
    it.
    """
    try:
        return TIER_ALLOWED_BACKENDS[tier]
    except KeyError:
        raise PrinciplesError(
            f"naysayer tier {tier!r} has no row in TIER_ALLOWED_BACKENDS "
            f"(known: {sorted(TIER_ALLOWED_BACKENDS)!r}); refusing to attest it"
        ) from None


_ENV_PRINCIPLES_PATH = "MINDWIRE_NAYSAYER_PRINCIPLES_PATH"
# principles.py -> naysayer -> spirrow_mindwire -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PRINCIPLES_PATH = _REPO_ROOT / "spec" / "NAYSAYER_PRINCIPLES.md"

# The frontmatter ``version:`` this build of the code is written against. A mismatch is a
# FAIL-LOUD startup error, not a warning: the loader below exposes ``objection_classes()``,
# whose shape (``blocks:`` / ``evidence:``) exists only from v2 on. Reading a v1 document with
# v2 code would produce an empty class map and a silently permissive derivation, which is
# exactly the failure mode this thread exists to remove.
EXPECTED_PRINCIPLES_VERSION = 2

# Frontmatter is delimited by a ``---`` line at the very start of the file and the next ``---``
# line on its own. Parsed with PyYAML (already a dependency) rather than one regex per field:
# ``version:`` used to be read by its own ``^version: (\d+)$`` pattern applied to the WHOLE
# document, which is a second, weaker reader of the same fact. One document, one parse.
_FRONTMATTER_DELIM = "---"


class PrinciplesError(RuntimeError):
    """The principles SOT is missing or malformed (fail-loud — never inject blank)."""


def principles_path() -> Path:
    """Resolve the principles SOT path (``MINDWIRE_NAYSAYER_PRINCIPLES_PATH`` or default)."""
    override = os.environ.get(_ENV_PRINCIPLES_PATH)
    return Path(override) if override else _DEFAULT_PRINCIPLES_PATH


@lru_cache(maxsize=8)
def _read(path_str: str) -> str:
    path = Path(path_str)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrinciplesError(f"cannot read naysayer principles SOT at {path}: {exc}") from exc
    if not text.strip():
        raise PrinciplesError(f"naysayer principles SOT at {path} is empty")
    return text


def load_principles() -> str:
    """Return the full principles markdown **verbatim** (frontmatter included)."""
    return _read(str(principles_path()))


@dataclass(frozen=True)
class ObjectionClass:
    """One entry of the ``objection_classes`` map in the principles frontmatter.

    ``blocks`` is the whole point of the class system: it is the ONE place that says
    whether an objection of this kind forces REQUEST_CHANGES. ``evidence`` is the
    obligation the naysayer must be able to discharge to raise it — required for every
    blocking class, absent for advisory ones (there is nothing to discharge).
    """

    name: str
    blocks: bool
    evidence: str | None = None


def _parse_frontmatter(text: str, path: Path) -> dict[str, Any]:
    """Return the YAML frontmatter block of ``text`` as a mapping (fail-loud)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise PrinciplesError(f"naysayer principles SOT at {path} has no frontmatter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == _FRONTMATTER_DELIM)
    except StopIteration:
        raise PrinciplesError(
            f"naysayer principles SOT at {path} has an unterminated frontmatter block"
        ) from None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise PrinciplesError(f"frontmatter of {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PrinciplesError(f"frontmatter of {path} is not a mapping (got {type(data).__name__})")
    return data


@lru_cache(maxsize=8)
def _frontmatter(path_str: str) -> dict[str, Any]:
    return _parse_frontmatter(_read(path_str), Path(path_str))


def principles_version() -> int:
    """Return the frontmatter ``version:``, pinned to :data:`EXPECTED_PRINCIPLES_VERSION`.

    Recorded in every naysayer output so a later revision (which bumps the version)
    stays auditable (D-1 traceability). Read out of the parsed frontmatter — there is
    no second reader of this fact anymore.

    A document version this build was not written against is a startup error, not a
    fallback: see the note above :data:`EXPECTED_PRINCIPLES_VERSION`.
    """
    path = principles_path()
    raw = _frontmatter(str(path)).get("version")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PrinciplesError(
            f"no integer 'version:' in frontmatter of {path} (cannot tag output): {raw!r}"
        )
    if raw != EXPECTED_PRINCIPLES_VERSION:
        raise PrinciplesError(
            f"naysayer principles SOT at {path} is version {raw}, but this build expects "
            f"{EXPECTED_PRINCIPLES_VERSION}. The class vocabulary read by objection_classes() "
            f"is version-specific; refusing to run against a document it was not written for."
        )
    return raw


def objection_classes() -> Mapping[str, ObjectionClass]:
    """Return the ``objection_classes`` map from the frontmatter (fail-loud).

    This is the ONLY place the class vocabulary exists in the running system. The PR-review
    prompt does not enumerate it (it refers the model to the frontmatter it is already given
    verbatim) and no Python literal restates it —
    ``test_no_src_file_duplicates_the_objection_class_vocabulary`` pins that absence. A second
    copy is precisely the dual-management defect the class system was introduced to remove.

    Every failure is a :class:`PrinciplesError`, never a permissive default. An empty or
    unreadable map would make "does this objection block?" unanswerable, and the only safe
    answer to an unanswerable gate question is to refuse to start.
    """
    path = principles_path()
    raw = _frontmatter(str(path)).get("objection_classes")
    if not isinstance(raw, dict) or not raw:
        raise PrinciplesError(
            f"frontmatter of {path} has no non-empty 'objection_classes' mapping: {raw!r}"
        )
    classes: dict[str, ObjectionClass] = {}
    for name, entry in raw.items():
        where = f"objection_classes.{name} in {path}"
        if not isinstance(entry, dict):
            raise PrinciplesError(f"{where} is not a mapping (got {type(entry).__name__})")
        blocks = entry.get("blocks")
        if not isinstance(blocks, bool):
            raise PrinciplesError(f"{where} has no boolean 'blocks' (got {blocks!r})")
        # T-naysayer-blocking-bar-undefined promotion pin 2 (msg-3520): the ``evidence:`` line
        # is validated for two syntactic failure modes only — empty value (``空値``) and
        # class↔evidence type mismatch (``class↔evidence 型不一致``). No heuristic /
        # boilerplate detection (msg-3521 obj-2 is that YAGNI); the linter is a shape check,
        # not a judgement of the string's *content*.
        #
        # The shape is asymmetric on purpose, and mirrors the design intent stated in the SOT
        # itself. A blocking class carries an ``evidence:`` obligation (what a reader can
        # check to accept the objection); an advisory class carries *none* — the SOT states
        # "no advisory class needs one" (spec/NAYSAYER_PRINCIPLES.md §"Objection classes",
        # v2). So the two failure modes are:
        #
        #   (i)  blocking + missing / empty / whitespace-only / non-string evidence
        #        → the escape hatch this design deliberately does not have. Reachable as
        #        ``evidence:`` (bare, YAML → None), ``evidence: null``, ``evidence: ""``,
        #        ``evidence: "   "``, ``evidence: 42``, or an absent key.
        #   (ii) advisory + evidence field present with a non-null value
        #        → the class↔evidence type mismatch: assigning an obligation to a class the
        #        SOT says has none. Silently accepting it lets a future editor drift the
        #        vocabulary into a hybrid state where "advisory" sometimes carries an
        #        obligation, which is the exact dual-management complexity Principle 2
        #        exists to remove.
        #
        # ``evidence: null`` on an advisory class parses to ``None`` and is accepted — it is
        # the "explicitly no obligation" spelling, indistinguishable from an absent key. The
        # linter refuses to sharpen that distinction: making the two spellings mean different
        # things is a design change, not a shape check.
        evidence = entry.get("evidence")
        if blocks:
            if not isinstance(evidence, str) or not evidence.strip():
                raise PrinciplesError(f"{where} is blocking but carries no 'evidence' obligation")
        elif evidence is not None:
            raise PrinciplesError(
                f"{where} is advisory (blocks: false) but carries an 'evidence' field "
                f"({evidence!r}); advisory classes have no evidence obligation to state, so a "
                f"non-null 'evidence' is a class-to-evidence type mismatch. Remove the field, "
                f"or set it to ``null`` if the explicit spelling is preferred."
            )
        classes[str(name)] = ObjectionClass(
            name=str(name),
            blocks=blocks,
            evidence=evidence.strip() if isinstance(evidence, str) else None,
        )
    return classes


def build_preamble() -> str:
    """Assemble the naysayer preamble: the principles SOT injected verbatim.

    The whole markdown (5 principles + adversarial mandate) is the preamble; the
    only addition is a one-line version banner so the reader (and the relayed
    output) carries the ``principles_version`` it is judging under. Returned as
    the *system* message of the Lexora call.
    """
    return (
        f"[naysayer principles_version={principles_version()} — "
        f"injected verbatim from the canonical SOT; reason under every principle below]\n\n"
        f"{load_principles()}"
    )


__all__ = [
    "CODEX_BACKEND",
    "CODEX_TIER_PROMPT_CHAR_LIMIT",
    "EXPECTED_PRINCIPLES_VERSION",
    "GEMINI_BACKEND",
    "GEMINI_FALLBACK_BACKEND",
    "N3_CONFIG_PATH",
    "N3_SENSITIVE_MARKER",
    "N3_SENSITIVE_PATHS_KEY",
    "NAYSAYER_GEMINI_TIER",
    "NAYSAYER_MODEL_TIER",
    "NAYSAYER_UPSTREAM_MODEL",
    "TIER_ALLOWED_BACKENDS",
    "ObjectionClass",
    "PrinciplesError",
    "allowed_backends",
    "build_preamble",
    "load_principles",
    "objection_classes",
    "principles_path",
    "principles_version",
]
