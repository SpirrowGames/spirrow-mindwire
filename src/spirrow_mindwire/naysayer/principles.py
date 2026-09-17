"""Naysayer principles SOT loader + preamble builder (ADR-2026-06-03-17 D-1).

The 5 principles are defined **once**, in ``spec/NAYSAYER_PRINCIPLES.md``
(versioned). This module is the single programmatic entry point that reads that
file **verbatim** and assembles the preamble injected into every naysayer
invocation (design-time relay and the PR-gate alike). Principles are never
restated as Python string literals here — a one-place edit to the markdown
propagates to all injections (D-1 "常時注入").

The independent model identity is pinned here too (N-4: SOT = Gemini), so the
adapters/driver import the tier name from one place instead of hardcoding it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# N-4 (ADR-17): the independent naysayer runs on **Gemini** (SOT). ``naysayer``
# is the Lexora *tier* name that routes to it; the upstream model id is recorded
# for traceability/observability. Pinned here = the one place to change it.
NAYSAYER_MODEL_TIER = "naysayer"
NAYSAYER_UPSTREAM_MODEL = "gemini-3.1-pro-preview"

# The value P-2's preflight requires to find in the gateway's own accounting row
# for a ``naysayer``-tier request (msg-953 §3: "成功条件: ``backend ==
# expected_backend`` (config 単一 SOT)"). Pinned beside the tier for the same
# reason N-4 pinned that: one place to change when the independent distribution
# changes.
#
# **Not** the same string as :data:`NAYSAYER_UPSTREAM_MODEL`, and the difference
# is load-bearing. The cost row's ``backend`` field names the *backend* Lexora
# routed to (``"gemini"``); the upstream model id (``"gemini-3.1-pro-preview"``)
# is a finer-grained fact the row does not carry. Comparing against the model id
# would fail every attestation; comparing against the backend is what the row
# can actually answer. Measured against the live gateway, row 6032, 2026-08-13.
NAYSAYER_EXPECTED_BACKEND = "gemini"

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
        evidence = entry.get("evidence")
        if blocks:
            # A blocking class with no evidence obligation is the escape hatch this design
            # deliberately does not have: it would let an objection block while stating
            # nothing a reader could check.
            if not isinstance(evidence, str) or not evidence.strip():
                raise PrinciplesError(f"{where} is blocking but carries no 'evidence' obligation")
        elif evidence is not None and not isinstance(evidence, str):
            raise PrinciplesError(f"{where} has a non-string 'evidence' (got {evidence!r})")
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
    "EXPECTED_PRINCIPLES_VERSION",
    "NAYSAYER_EXPECTED_BACKEND",
    "NAYSAYER_MODEL_TIER",
    "NAYSAYER_UPSTREAM_MODEL",
    "ObjectionClass",
    "PrinciplesError",
    "build_preamble",
    "load_principles",
    "objection_classes",
    "principles_path",
    "principles_version",
]
