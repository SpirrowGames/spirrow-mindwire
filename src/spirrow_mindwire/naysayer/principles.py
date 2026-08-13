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
import re
from functools import lru_cache
from pathlib import Path

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

_VERSION_RE = re.compile(r"^version:\s*(\d+)\s*$", re.MULTILINE)


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


def principles_version() -> int:
    """Parse ``version:`` from the frontmatter. Fail-loud if absent/malformed.

    Recorded in every naysayer output so a later revision (which bumps the
    version) stays auditable (D-1 traceability).
    """
    match = _VERSION_RE.search(load_principles())
    if match is None:
        raise PrinciplesError(
            f"no 'version:' in frontmatter of {principles_path()} (cannot tag output)"
        )
    return int(match.group(1))


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
    "NAYSAYER_EXPECTED_BACKEND",
    "NAYSAYER_MODEL_TIER",
    "NAYSAYER_UPSTREAM_MODEL",
    "PrinciplesError",
    "build_preamble",
    "load_principles",
    "principles_path",
    "principles_version",
]
