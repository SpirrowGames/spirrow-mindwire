"""Loader for the loop-readable obligations manifest (``spec/process/obligations.yaml``).

The manifest holds the prompt clauses that bind agent behaviour at runtime
(read-back at entry/exit, the naysayer verdict constraint, the "declare what
you cannot read" rule). It is loaded once at the composition root and passed
into the SDK adapters by injection — the adapters never reach for a
module-global path themselves. That injection shape exists to make canary two-prime
(rendered-prompt-contains-obligation-body) cheap: the assembled system prompt
under test is exactly the one the adapter renders in production, from a
manifest the test picks.

Fail-loud on missing or malformed input: :func:`load_manifest` raises
:class:`ObligationsError`. The composition root
(:func:`spirrow_mindwire.loop_runner._build_dispatcher`) catches that at startup
and re-raises as ``SystemExit`` so a broken manifest halts the daemon at the
door rather than silently degrading each session's prompt.

Verbatim-move discipline: an entry with ``origin.moved_from`` claims its body
was moved byte-for-byte from a repo location — a Python string literal
(``path::LITERAL_NAME``) or a documentation section (``path::§HEADING``). Canary
two-double-prime asserts ``len(body) == origin.original_length`` on every such
entry, so a paraphrase during a later edit reds the gate rather than drifting
the loop's actual instructions away from what was reviewed. The loader treats
``moved_from`` as an opaque non-empty string on purpose (no format validation):
the invariant that matters is the length equality, and enshrining a specific
format here would force a schema bump every time a new kind of source needed
representing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .value_objects import Role

# obligations.py -> spirrow_mindwire -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST_REL = Path("spec") / "process" / "obligations.yaml"

# Recognized role names inside the manifest — restricted to the two roles whose
# system prompts the manifest actually feeds today. Extend deliberately (a typo
# in role: line would otherwise silently drop an obligation from every prompt).
_MANIFEST_ROLES: dict[str, Role] = {
    Role.IMPLEMENTER.value: Role.IMPLEMENTER,
    Role.NAYSAYER.value: Role.NAYSAYER,
}


class ObligationsError(RuntimeError):
    """The obligations manifest is missing, unparseable, or violates its schema."""


@dataclass(frozen=True)
class ObligationOrigin:
    """Evidence that an obligation body was moved verbatim from a repo location.

    The presence of an ``origin`` block asserts a move-not-copy: the source at
    ``moved_from`` has been deleted, and ``original_length`` records the
    character length that source had at the time of the move so canary
    two-double-prime can detect later paraphrasing. ``moved_from`` is an
    opaque non-empty string — a Python string literal
    (``path::LITERAL_NAME``) or a documentation section (``path::§HEADING``);
    the loader intentionally does not police the format.
    """

    moved_from: str
    original_length: int


@dataclass(frozen=True)
class Obligation:
    """A single loop-readable obligation, rendered into one role's system prompt."""

    id: str
    role: Role
    body: str
    origin: ObligationOrigin | None


@dataclass(frozen=True)
class ObligationsManifest:
    """The parsed manifest — an immutable, ordered view of the on-disk YAML."""

    version: int
    obligations: tuple[Obligation, ...]

    def for_role(self, role: Role) -> tuple[Obligation, ...]:
        """Return the obligations attached to ``role``, in manifest order."""
        return tuple(o for o in self.obligations if o.role is role)

    def render_role_obligations(self, role: Role) -> str:
        """Render the obligations for ``role`` as a single injectable text block.

        Each obligation is emitted as ``[<id>]\\n<body>`` and successive
        obligations are joined by a blank line, so a reader can see both the id
        of what binds them and the verbatim body. Returns the empty string if
        ``role`` has no obligations (so the caller can concatenate without a
        conditional).
        """
        entries = self.for_role(role)
        if not entries:
            return ""
        return "\n\n".join(f"[{o.id}]\n{o.body}" for o in entries)


def default_manifest_path() -> Path:
    """Return the in-repo default path for ``spec/process/obligations.yaml``.

    Exposed so tests can compare against it — not called from adapters (the
    adapters never load the manifest themselves; the composition root does).
    """
    return _REPO_ROOT / _DEFAULT_MANIFEST_REL


def load_manifest(path: Path | None = None) -> ObligationsManifest:
    """Load and validate the obligations manifest — fail-loud on any deviation.

    ``path`` defaults to :func:`default_manifest_path`; tests point it at a
    fixture. The parse is strict: missing/duplicate ids, unknown roles, empty
    bodies, and origin blocks whose recorded length disagrees with the actual
    body length all raise :class:`ObligationsError`. That failure is caught at
    the composition root and re-raised as ``SystemExit`` (loader ← composition
    root ← daemon startup) — see :mod:`spirrow_mindwire.loop_runner`.
    """
    resolved = path if path is not None else default_manifest_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ObligationsError(
            f"obligations manifest at {resolved} could not be read: {exc}"
        ) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ObligationsError(
            f"obligations manifest at {resolved} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ObligationsError(
            f"obligations manifest at {resolved} must be a mapping at top level "
            f"(got {type(data).__name__})"
        )
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ObligationsError(
            f"obligations manifest at {resolved} is missing a positive integer 'version'"
        )
    entries = data.get("obligations")
    if not isinstance(entries, list) or not entries:
        raise ObligationsError(
            f"obligations manifest at {resolved} is missing a non-empty 'obligations' list"
        )
    parsed: list[Obligation] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        parsed_entry = _parse_entry(entry, index=index, source=resolved)
        if parsed_entry.id in seen:
            raise ObligationsError(
                f"obligations manifest at {resolved} contains duplicate id {parsed_entry.id!r}"
            )
        seen.add(parsed_entry.id)
        parsed.append(parsed_entry)
    return ObligationsManifest(version=version, obligations=tuple(parsed))


def _parse_entry(entry: Any, *, index: int, source: Path) -> Obligation:
    if not isinstance(entry, dict):
        raise ObligationsError(f"obligations manifest at {source}: entry #{index} is not a mapping")
    obligation_id = entry.get("id")
    if not isinstance(obligation_id, str) or not obligation_id.startswith("OBL-"):
        raise ObligationsError(
            f"obligations manifest at {source}: entry #{index} 'id' must be a string "
            "starting with 'OBL-'"
        )
    role_name = entry.get("role")
    role_value = _MANIFEST_ROLES.get(role_name) if isinstance(role_name, str) else None
    if role_value is None:
        raise ObligationsError(
            f"obligations manifest at {source}: obligation {obligation_id!r} 'role' "
            f"must be one of {sorted(_MANIFEST_ROLES)} (got {role_name!r})"
        )
    body = entry.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ObligationsError(
            f"obligations manifest at {source}: obligation {obligation_id!r} 'body' "
            "must be a non-empty string"
        )
    # YAML block scalars (`|` / `|-`) emit a trailing newline that is not part of
    # the semantic content; strip it so the length invariant compares like-for-like
    # with the flow-scalar bodies used for the moved entries.
    normalized_body = body.rstrip("\n")

    origin = _parse_origin(
        entry.get("origin"),
        obligation_id=obligation_id,
        body=normalized_body,
        source=source,
    )
    return Obligation(id=obligation_id, role=role_value, body=normalized_body, origin=origin)


def _parse_origin(
    origin_raw: Any,
    *,
    obligation_id: str,
    body: str,
    source: Path,
) -> ObligationOrigin | None:
    if origin_raw is None:
        return None
    if not isinstance(origin_raw, dict):
        raise ObligationsError(
            f"obligations manifest at {source}: obligation {obligation_id!r} 'origin' "
            "must be a mapping when present"
        )
    moved_from = origin_raw.get("moved_from")
    original_length = origin_raw.get("original_length")
    if not isinstance(moved_from, str) or not moved_from.strip():
        raise ObligationsError(
            f"obligations manifest at {source}: obligation {obligation_id!r} "
            "'origin.moved_from' must be a non-empty string"
        )
    if not isinstance(original_length, int) or original_length < 0:
        raise ObligationsError(
            f"obligations manifest at {source}: obligation {obligation_id!r} "
            "'origin.original_length' must be a non-negative integer"
        )
    if len(body) != original_length:
        # This is canary two-double-prime enforced at load time as well as in the test suite:
        # a moved body whose length no longer matches the recorded original is
        # exactly the paraphrase-drift the invariant is here to catch. Failing
        # in the loader means the daemon refuses to start (composition-root
        # SystemExit), not just that the tests red.
        raise ObligationsError(
            f"obligations manifest at {source}: obligation {obligation_id!r} body length "
            f"({len(body)}) does not match origin.original_length ({original_length}); "
            "either restore the verbatim text moved from "
            f"{moved_from!r} or update origin.original_length in the same commit"
        )
    return ObligationOrigin(moved_from=moved_from, original_length=original_length)


__all__ = [
    "Obligation",
    "ObligationOrigin",
    "ObligationsError",
    "ObligationsManifest",
    "default_manifest_path",
    "load_manifest",
]
