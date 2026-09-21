"""Tests for :mod:`spirrow_mindwire.spec_pin` — SPEC-2026-09-20 I-3.

These pin the four invariants the atomic cutover PR is required to guarantee
(Bohr msg-3989 test requirements + msg-3991 objections 2 / 3):

1. A thread without a mapping entry gets a **bootstrap** pin (§3-B branch 1).
2. A thread with a mapping entry AND a readable spec file gets a **resolved**
   pin (§3-A) — Phase 1 has no wiring for this path, so the test asserts the
   fail-loud shape below.
3. A thread with a mapping entry but the spec file is missing → the dispatch
   aborts loudly via :class:`~spirrow_mindwire.spec_pin.PinDispatchAbortError` and
   **no pin file is written** (Bohr msg-3991 objection 3: no silent
   degradation to bootstrap; the mapping fault must NOT be buried under the
   BOOTSTRAP annunciator).
4. The bootstrap pin YAML follows §3-C canonical form byte-for-byte (schema
   version, mode, pinned_by, absence of the seven resolved-form fields).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

from spirrow_mindwire.spec_pin import (
    DISPATCHER_PINNED_BY,
    EMPTY_MAPPING,
    PinDispatchAbortError,
    SpecPinMapping,
    SpecPinWriter,
    build_bootstrap_pin,
    pin_yaml_bytes,
)
from spirrow_mindwire.value_objects import Role

_THREAD_ID = "01JTHREADPINHARDENING000000"

# The 7 fields SPEC-2026-09-20 §3-A forbids on a bootstrap pin — mixing any
# of them fires NO-PIN(PROHIBITED_FIELD) on the reader (verify.py V-9 pin).
_RESOLVED_FORM_FIELDS: frozenset[str] = frozenset(
    {"spec_id", "thread", "repo", "branch", "path", "blob_sha", "commit"}
)


class _StaticMapping:
    """Test :class:`SpecPinMapping` returning a fixed ``spec_id`` for one thread."""

    def __init__(self, spec_id_by_thread: dict[str, str]) -> None:
        self._by_thread = dict(spec_id_by_thread)

    def spec_id_for(self, thread_id: str) -> str | None:
        return self._by_thread.get(thread_id)


# --------------------------------------------------------------------------- #
# Invariant 1 — unmapped thread → bootstrap pin
# --------------------------------------------------------------------------- #


def test_writer_writes_bootstrap_pin_when_thread_is_unmapped(tmp_path: Path) -> None:
    """The empty mapping is the Phase 1 default; every dispatch writes bootstrap."""
    writer = SpecPinWriter(repo_root=tmp_path, mapping=EMPTY_MAPPING)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)

    pin_path = tmp_path / ".mindwire" / "pin"
    assert pin_path.is_file(), "pin file must exist after write_before_dispatch"
    parsed = yaml.safe_load(pin_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert parsed["mode"] == "bootstrap"
    assert parsed["pinned_by"] == DISPATCHER_PINNED_BY
    assert "pinned_at" in parsed and isinstance(parsed["pinned_at"], str)
    # §3-A: NONE of the 7 resolved-form fields may appear on a bootstrap pin.
    forbidden = _RESOLVED_FORM_FIELDS.intersection(parsed.keys())
    assert not forbidden, (
        f"bootstrap pin contains resolved-form fields {sorted(forbidden)} — "
        "reader would fire NO-PIN(PROHIBITED_FIELD) at halt (SPEC-2026-09-20 §3-A)"
    )


def test_writer_uses_dispatcher_as_pinned_by_for_bootstrap(tmp_path: Path) -> None:
    """`pinned_by: dispatcher` is the SPEC-2026-09-20 §3-C canonical value.

    Bohr msg-3991 rejected Einstein Objection 1 (that ADR-2026-05-29-10's
    7-role registry blocks `dispatcher` here): the landed §3-C example uses
    `dispatcher` as the canonical bootstrap-pin `pinned_by` value, and
    D-19 (landed manifest immutability) requires the implementation to
    match the canonical example. Pinning this test prevents a regression
    that silently rewrites the value on the basis of an unreadable ADR.
    """
    writer = SpecPinWriter(repo_root=tmp_path, mapping=EMPTY_MAPPING)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    parsed = yaml.safe_load((tmp_path / ".mindwire" / "pin").read_text(encoding="utf-8"))
    assert parsed["pinned_by"] == "dispatcher"


def test_writer_writes_pin_for_naysayer_role(tmp_path: Path) -> None:
    """§2.1 D-32 names implementer and naysayer explicitly; both get pins."""
    writer = SpecPinWriter(repo_root=tmp_path)
    writer.write_before_dispatch(Role.NAYSAYER, _THREAD_ID)
    assert (tmp_path / ".mindwire" / "pin").is_file()


def test_writer_noops_for_proposer_role(tmp_path: Path) -> None:
    """§2.1 D-32 targets implementer/naysayer dispatch; the proposer is out of scope.

    Widening the required-role set would be a spec change; this test guards
    against a silent widening that writes a pin the proposer's face does not
    read.
    """
    writer = SpecPinWriter(repo_root=tmp_path)
    writer.write_before_dispatch(Role.PROPOSER, _THREAD_ID)
    assert not (tmp_path / ".mindwire" / "pin").exists()


# --------------------------------------------------------------------------- #
# Invariant 2 / 3 — mapped thread + spec file absent -> PinDispatchAbortError
# (Bohr msg-3991 Objection 3: no silent degradation to bootstrap)
# --------------------------------------------------------------------------- #


def test_writer_aborts_when_mapping_points_at_missing_spec_file(tmp_path: Path) -> None:
    """A mapping-declared spec that is not on disk raises PinDispatchAbortError.

    The mapping is telling the dispatcher "this thread has a spec"; if the
    spec file is missing that is an OPERATIONAL fault (mis-configured
    mapping, deleted spec, wrong branch). Silently falling back to a
    bootstrap pin would bury that fault under the BOOTSTRAP annunciator
    (Bohr msg-3991 objection 3), so the writer refuses.

    The abort message names the SPEC-id and the expected path so an
    operator can see what was missing.
    """
    mapping = _StaticMapping({_THREAD_ID: "SPEC-2099-01-01-nonexistent"})
    writer = SpecPinWriter(repo_root=tmp_path, mapping=mapping)
    with pytest.raises(PinDispatchAbortError) as exc_info:
        writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    err = exc_info.value
    assert err.spec_id == "SPEC-2099-01-01-nonexistent"
    assert err.thread_id == _THREAD_ID
    assert "SPEC-2099-01-01-nonexistent" in str(err)
    # The mapping fault MUST NOT have written a pin (msg-3991 objection 3).
    assert not (tmp_path / ".mindwire" / "pin").exists(), (
        "PinDispatchAbortError must NOT leave a pin behind — silently degrading "
        "to bootstrap would bury the mapping fault under the BOOTSTRAP annunciator"
    )


def test_writer_aborts_when_mapped_spec_exists_but_no_git_reader_wired(tmp_path: Path) -> None:
    """Even a present spec file can't produce a resolved pin in Phase 1.

    Resolved pins need branch / commit / blob_sha (§3-A required fields) and
    Phase 1 does not wire a git reader. The current safe behaviour is
    fail-loud via PinDispatchAbortError — a successor spec that adds mapping
    infrastructure will also wire the git reader, at which point this test
    will be updated in the same PR (deliberate coupling — the shape of the
    fault message names it).
    """
    spec_id = "SPEC-2026-09-20-pin-hardening-and-id-audit"
    (tmp_path / "spec" / "design").mkdir(parents=True)
    (tmp_path / "spec" / "design" / f"{spec_id}.md").write_text(
        "---\nspec_id: " + spec_id + "\n---\ncontent\n", encoding="utf-8"
    )
    mapping = _StaticMapping({_THREAD_ID: spec_id})
    writer = SpecPinWriter(repo_root=tmp_path, mapping=mapping)
    with pytest.raises(PinDispatchAbortError):
        writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    # Still no pin file — the abort takes precedence.
    assert not (tmp_path / ".mindwire" / "pin").exists()


# --------------------------------------------------------------------------- #
# Invariant 4 — bootstrap pin YAML shape matches §3-C canonical example byte-wise
# --------------------------------------------------------------------------- #


def test_build_bootstrap_pin_shape_matches_section_3c(tmp_path: Path) -> None:
    """The dict :func:`build_bootstrap_pin` returns serialises to §3-C YAML."""
    pin = build_bootstrap_pin(thread_id=_THREAD_ID, pinned_at="2026-09-21T12:00:00Z")
    assert list(pin.keys()) == ["schema_version", "mode", "pinned_at", "pinned_by", "reason"]
    assert pin["schema_version"] == 1
    assert pin["mode"] == "bootstrap"
    assert pin["pinned_by"] == "dispatcher"
    assert pin["pinned_at"] == "2026-09-21T12:00:00Z"
    assert _THREAD_ID in cast(str, pin["reason"])
    # None of the 7 resolved-form fields:
    assert not _RESOLVED_FORM_FIELDS.intersection(pin.keys())


def test_pin_yaml_bytes_is_block_style_and_preserves_key_order() -> None:
    """The rendered YAML must be block-style (§3-C examples) and preserve key order.

    Block style keeps the pin readable by a human who edits it; preserving key
    order keeps a diff of two consecutive pin writes minimal (fields never move).
    """
    pin = build_bootstrap_pin(thread_id=_THREAD_ID, pinned_at="2026-09-21T00:00:00Z")
    rendered = pin_yaml_bytes(pin).decode("utf-8")
    # Block style: no `{...}` flow syntax, one field per line.
    assert "{" not in rendered
    lines = [line for line in rendered.splitlines() if line and not line.startswith("#")]
    keys_in_order = [line.split(":", 1)[0] for line in lines]
    assert keys_in_order == ["schema_version", "mode", "pinned_at", "pinned_by", "reason"]


# --------------------------------------------------------------------------- #
# Atomic-write invariant — a reader never sees a half-written pin
# --------------------------------------------------------------------------- #


def test_writer_overwrites_existing_pin_atomically(tmp_path: Path) -> None:
    """Two successive writes leave exactly one pin file with the latest content."""
    writer = SpecPinWriter(repo_root=tmp_path)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    first = (tmp_path / ".mindwire" / "pin").read_bytes()
    # Now write again — a new bootstrap pin (different pinned_at may or may not
    # differ; either way there must be exactly one pin file after the second write).
    writer.write_before_dispatch(Role.IMPLEMENTER, "01JOTHERTHREADID000000000000")
    pins = list((tmp_path / ".mindwire").glob("pin*"))
    assert [p.name for p in pins] == ["pin"], (
        f"expected exactly one 'pin' file (atomic write leaves no .tmp behind); "
        f"found {[p.name for p in pins]}"
    )
    second = (tmp_path / ".mindwire" / "pin").read_bytes()
    # The second reason references the second thread id
    assert b"01JOTHERTHREADID000000000000" in second
    # Sanity: the two are different
    assert first != second


# --------------------------------------------------------------------------- #
# Type-safety of the SpecPinMapping protocol
# --------------------------------------------------------------------------- #


def test_empty_mapping_reports_no_spec_for_any_thread() -> None:
    """EMPTY_MAPPING is the Phase 1 default — every thread returns None."""
    mapping: SpecPinMapping = EMPTY_MAPPING
    assert mapping.spec_id_for("any-thread") is None
    assert mapping.spec_id_for(_THREAD_ID) is None
    assert mapping.spec_id_for("") is None
