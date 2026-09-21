"""Tests for :mod:`spirrow_mindwire.spec_pin` — SPEC-2026-09-20 I-3.

These pin the four invariants the atomic cutover PR is required to guarantee
(Bohr msg-3989 test requirements + msg-3991 objections 2 / 3, plus PR-gate
#335 round-2 finding on pin_target_dir vs spec_source_root):

1. A thread without a mapping entry gets a **bootstrap** pin (§3-B branch 1).
2. A thread with a mapping entry AND a spec_source_root, but the spec file
   missing on disk, aborts loudly (Bohr msg-3991 objection 3).
3. A thread with a mapping entry on a writer that has NO spec_source_root
   aborts loudly with a message naming the missing configuration (PR-review
   #335 round-2 finding: pin_target_dir and spec_source_root must not be
   conflated — the Phase 0/1 ThreadDispatcher writes into a per-thread
   scratch dir that does not contain the source tree, so its writer has
   no spec_source_root).
4. The bootstrap pin YAML follows §3-C canonical form byte-for-byte (schema
   version, mode, pinned_by, absence of the seven resolved-form fields).
"""

from __future__ import annotations

import os
import threading
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
    writer = SpecPinWriter(pin_target_dir=tmp_path, mapping=EMPTY_MAPPING)
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
    writer = SpecPinWriter(pin_target_dir=tmp_path, mapping=EMPTY_MAPPING)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    parsed = yaml.safe_load((tmp_path / ".mindwire" / "pin").read_text(encoding="utf-8"))
    assert parsed["pinned_by"] == "dispatcher"


def test_writer_writes_pin_for_naysayer_role(tmp_path: Path) -> None:
    """§2.1 D-32 names implementer and naysayer explicitly; both get pins."""
    writer = SpecPinWriter(pin_target_dir=tmp_path)
    writer.write_before_dispatch(Role.NAYSAYER, _THREAD_ID)
    assert (tmp_path / ".mindwire" / "pin").is_file()


def test_writer_noops_for_proposer_role(tmp_path: Path) -> None:
    """§2.1 D-32 targets implementer/naysayer dispatch; the proposer is out of scope.

    Widening the required-role set would be a spec change; this test guards
    against a silent widening that writes a pin the proposer's face does not
    read.
    """
    writer = SpecPinWriter(pin_target_dir=tmp_path)
    writer.write_before_dispatch(Role.PROPOSER, _THREAD_ID)
    assert not (tmp_path / ".mindwire" / "pin").exists()


# --------------------------------------------------------------------------- #
# pin_target_dir vs spec_source_root — the two must not be conflated
# (PR-review #335 round-2 BLOCKING finding)
# --------------------------------------------------------------------------- #


def test_pin_target_dir_is_the_only_directory_the_pin_write_touches(tmp_path: Path) -> None:
    """The pin lands under ``pin_target_dir``; ``spec_source_root`` is not touched.

    PR-review #335 round-2: the old ``repo_root`` parameter conflated
    "where the pin file goes" with "where spec files live", which broke
    the Phase 0/1 ThreadDispatcher path (pin goes to per-thread scratch,
    spec source lives in the git checkout). Split enforced by giving the
    two parameters distinct directories in this test and asserting the
    pin lands under ``pin_target_dir`` — never under ``spec_source_root``.
    """
    pin_dir = tmp_path / "pin_target"
    source_dir = tmp_path / "spec_source"
    pin_dir.mkdir()
    source_dir.mkdir()
    writer = SpecPinWriter(pin_target_dir=pin_dir, spec_source_root=source_dir)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    assert (pin_dir / ".mindwire" / "pin").is_file()
    assert not (source_dir / ".mindwire" / "pin").exists(), (
        "pin escaped pin_target_dir into spec_source_root — the two must remain "
        "distinct concerns (PR-review #335 round-2 BLOCKING)"
    )


def test_writer_without_spec_source_root_aborts_on_mapped_thread(tmp_path: Path) -> None:
    """A writer without ``spec_source_root`` cannot honour a mapped ``spec_id``.

    This is the shape the Phase 0/1 ThreadDispatcher uses: pin_target_dir
    is per-thread scratch, spec_source_root is ``None``. A mapping that
    returns a non-``None`` ``spec_id`` on such a writer is a configuration
    error, and the writer must abort loudly (msg-3991 objection 3: never
    silent bootstrap degradation) with a message that names the missing
    ``spec_source_root`` as the actual cause — not the spec file's state.
    """
    mapping = _StaticMapping({_THREAD_ID: "SPEC-2099-01-01-anything"})
    writer = SpecPinWriter(
        pin_target_dir=tmp_path,
        spec_source_root=None,
        mapping=mapping,
    )
    with pytest.raises(PinDispatchAbortError) as exc_info:
        writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    message = str(exc_info.value)
    assert "spec_source_root" in message, (
        f"exception message does not name the missing spec_source_root: {message!r}"
    )
    # The message must not mislead by naming a file's on-disk state.
    assert "not on disk" not in message
    assert "git reader" not in message
    assert not (tmp_path / ".mindwire" / "pin").exists()


# --------------------------------------------------------------------------- #
# Mapped thread + spec file absent -> PinDispatchAbortError
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
    writer = SpecPinWriter(
        pin_target_dir=tmp_path,
        spec_source_root=tmp_path,
        mapping=mapping,
    )
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

    Also pins PR-review #335 round-1 BLOCKING #1: the exception message
    must NOT claim the file is unreadable when it is on disk. A hardcoded
    "is unreadable" prefix contradicts the actual cause on this path (the
    file exists and is perfectly readable; the fault is the missing git
    reader), so the message derives from the caller-supplied `cause`
    string and the `cause` on this path says the file exists.
    """
    spec_id = "SPEC-2026-09-20-pin-hardening-and-id-audit"
    (tmp_path / "spec" / "design").mkdir(parents=True)
    (tmp_path / "spec" / "design" / f"{spec_id}.md").write_text(
        "---\nspec_id: " + spec_id + "\n---\ncontent\n", encoding="utf-8"
    )
    mapping = _StaticMapping({_THREAD_ID: spec_id})
    writer = SpecPinWriter(
        pin_target_dir=tmp_path,
        spec_source_root=tmp_path,
        mapping=mapping,
    )
    with pytest.raises(PinDispatchAbortError) as exc_info:
        writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    message = str(exc_info.value)
    # The message must NOT falsely claim the file is unreadable — the file
    # exists on disk on this path (PR-review #335 round-1 finding).
    assert "is unreadable" not in message, (
        "PinDispatchAbortError message on the no-git-reader path claims the "
        f"file 'is unreadable' but the file exists on disk. Message: {message!r}"
    )
    # It MUST name the actual cause (Phase 1 missing git reader).
    assert "git reader" in message, (
        f"exception message does not name the actual cause (no git reader): {message!r}"
    )
    # Still no pin file — the abort takes precedence.
    assert not (tmp_path / ".mindwire" / "pin").exists()


def test_abort_message_names_the_missing_file_cause_when_file_is_absent(
    tmp_path: Path,
) -> None:
    """Symmetric assertion for the path-missing branch (PR-review #335 round-1 BLOCKING #1).

    The three abort branches (no spec_source_root / file missing / no git
    reader) must produce mutually distinguishable messages — an operator
    reading the exception must be able to tell them apart without guessing.
    """
    mapping = _StaticMapping({_THREAD_ID: "SPEC-2099-01-01-nonexistent"})
    writer = SpecPinWriter(
        pin_target_dir=tmp_path,
        spec_source_root=tmp_path,
        mapping=mapping,
    )
    with pytest.raises(PinDispatchAbortError) as exc_info:
        writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    message = str(exc_info.value)
    assert "not on disk" in message, (
        f"path-missing exception message does not name the actual cause: {message!r}"
    )
    # And by symmetry MUST NOT be confusable with the git-reader path or
    # the no-spec_source_root path.
    assert "git reader" not in message, (
        f"path-missing message names the wrong branch's cause (git reader): {message!r}"
    )
    assert "spec_source_root" not in message, (
        f"path-missing message names the wrong branch's cause "
        f"(spec_source_root missing): {message!r}"
    )


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
    writer = SpecPinWriter(pin_target_dir=tmp_path)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    first = (tmp_path / ".mindwire" / "pin").read_bytes()
    # Now write again — a new bootstrap pin (different pinned_at may or may not
    # differ; either way there must be exactly one pin file after the second write).
    writer.write_before_dispatch(Role.IMPLEMENTER, "01JOTHERTHREADID000000000000")
    entries = sorted(p.name for p in (tmp_path / ".mindwire").iterdir())
    assert entries == ["pin"], (
        f"expected exactly one 'pin' file (atomic write leaves no .tmp behind); found {entries}"
    )
    second = (tmp_path / ".mindwire" / "pin").read_bytes()
    # The second reason references the second thread id
    assert b"01JOTHERTHREADID000000000000" in second
    # Sanity: the two are different
    assert first != second


def test_atomic_write_tmp_filename_is_unique_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successive writes must NOT reuse the same tmp filename.

    PR-review #335 round-3 (BLOCKING): a hardcoded ``pin.tmp`` lets two
    cross-process writers open the same tmp file, one process replaces it
    while the other is mid-write, and the reader sees a torn file.
    :func:`_atomic_write` therefore uses :func:`tempfile.mkstemp` which
    guarantees a per-call unique name. This test observes the tmp names
    by wrapping ``os.replace`` to capture the source path each call sees.
    """
    seen_tmp_paths: list[Path] = []
    real_replace = os.replace

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen_tmp_paths.append(Path(os.fspath(src)))
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", spy_replace)

    writer = SpecPinWriter(pin_target_dir=tmp_path)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)
    writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)

    assert len(seen_tmp_paths) == 3, "expected three tmp files across three writes"
    # All three must be distinct — a hardcoded ".mindwire/pin.tmp" would
    # give three copies of the same path here (the bug this test guards).
    assert len(set(seen_tmp_paths)) == 3, (
        "tmp filenames are not unique across successive writes — "
        "cross-process writers would race on the same tmp file "
        f"(saw: {seen_tmp_paths})"
    )
    # And the fixed suffix ``pin.tmp`` must never appear literally: an
    # accidental regression back to ``path.with_suffix(...+'.tmp')`` would
    # give exactly ``.mindwire/pin.tmp`` on every call.
    for p in seen_tmp_paths:
        assert p.name != "pin.tmp", (
            f"tmp filename regressed to the hardcoded ``pin.tmp`` shape: {p!r}"
        )


def test_atomic_write_cleans_up_tmp_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that raises mid-flight must NOT leak tmp files.

    Cross-process defence needs no leftover ``.pin.*.tmp`` accumulating
    in the pin directory: (a) the pin directory is small and (b) an
    inspector auditing pin state should see the current pin only. Force
    the write path to raise by patching ``os.replace``; the abort must
    unlink the tmp file it created before re-raising.
    """
    writer = SpecPinWriter(pin_target_dir=tmp_path)

    class _BoomError(RuntimeError):
        pass

    def broken_replace(_src: str | os.PathLike[str], _dst: str | os.PathLike[str]) -> None:
        raise _BoomError("simulated replace failure")

    monkeypatch.setattr("os.replace", broken_replace)

    with pytest.raises(_BoomError):
        writer.write_before_dispatch(Role.IMPLEMENTER, _THREAD_ID)

    # No .tmp file should remain in the pin directory after the abort.
    leftover = sorted((tmp_path / ".mindwire").iterdir())
    assert leftover == [], f"tmp file leaked after write failure: {[p.name for p in leftover]}"


# --------------------------------------------------------------------------- #
# Type-safety of the SpecPinMapping protocol
# --------------------------------------------------------------------------- #


def test_empty_mapping_reports_no_spec_for_any_thread() -> None:
    """EMPTY_MAPPING is the Phase 1 default — every thread returns None."""
    mapping: SpecPinMapping = EMPTY_MAPPING
    assert mapping.spec_id_for("any-thread") is None
    assert mapping.spec_id_for(_THREAD_ID) is None
    assert mapping.spec_id_for("") is None


# --------------------------------------------------------------------------- #
# Async surface — write_before_dispatch_async (PR-review #335 round-4 ADVISORY)
#
# The blocking file I/O (mkstemp + write + os.replace) is deferred to the
# default thread-pool executor via asyncio.to_thread so async dispatch loops
# do not stall while the pin is being written. Three invariants pinned here:
#
#   (a) the async wrapper produces the same on-disk shape as the sync method
#       (parity — one code path with one behaviour).
#   (b) PinDispatchAbortError still propagates through the await (fail-loud —
#       msg-3991 objection 3 is preserved across the executor hop).
#   (c) the sync work actually runs off the event-loop thread (regression:
#       reverting to a direct sync call would run on the event-loop thread
#       and re-introduce the ADVISORY the human asked us to fix).
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_write_before_dispatch_async_produces_same_pin_shape(tmp_path: Path) -> None:
    """Parity: the async wrapper writes the same bytes shape as the sync method."""
    writer = SpecPinWriter(pin_target_dir=tmp_path, mapping=EMPTY_MAPPING)
    await writer.write_before_dispatch_async(Role.IMPLEMENTER, _THREAD_ID)

    pin_path = tmp_path / ".mindwire" / "pin"
    assert pin_path.is_file(), "async pin write did not land on disk"
    parsed = yaml.safe_load(pin_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert parsed["mode"] == "bootstrap"
    assert parsed["pinned_by"] == DISPATCHER_PINNED_BY
    assert "pinned_at" in parsed
    # §3-A: the resolved-form fields must be absent on a bootstrap pin —
    # exactly the same invariant the sync path pins in
    # test_writer_writes_bootstrap_pin_when_thread_is_unmapped.
    forbidden = _RESOLVED_FORM_FIELDS.intersection(parsed.keys())
    assert not forbidden, f"async bootstrap pin contains resolved-form fields {sorted(forbidden)}"


@pytest.mark.anyio
async def test_write_before_dispatch_async_noops_for_proposer_role(tmp_path: Path) -> None:
    """The async wrapper preserves the sync no-op branch for out-of-scope roles."""
    writer = SpecPinWriter(pin_target_dir=tmp_path)
    await writer.write_before_dispatch_async(Role.PROPOSER, _THREAD_ID)
    assert not (tmp_path / ".mindwire" / "pin").exists()


@pytest.mark.anyio
async def test_write_before_dispatch_async_propagates_abort(tmp_path: Path) -> None:
    """PinDispatchAbortError must surface through the await, not get swallowed.

    asyncio.to_thread re-raises the sync exception on the awaiting frame; if
    a future refactor forgets that (e.g. wraps the executor call in a
    ``try/except``), this test breaks and the fail-loud contract of msg-3991
    objection 3 is preserved on the async path.
    """
    mapping = _StaticMapping({_THREAD_ID: "SPEC-2099-01-01-nonexistent"})
    writer = SpecPinWriter(
        pin_target_dir=tmp_path,
        spec_source_root=tmp_path,
        mapping=mapping,
    )
    with pytest.raises(PinDispatchAbortError):
        await writer.write_before_dispatch_async(Role.IMPLEMENTER, _THREAD_ID)
    # Same "no pin left behind" guarantee as the sync path.
    assert not (tmp_path / ".mindwire" / "pin").exists()


@pytest.mark.anyio
async def test_write_before_dispatch_async_runs_off_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocking sync work must run on a different thread from the event loop.

    Regression: a naïve refactor that removes the ``asyncio.to_thread``
    wrapper (or accidentally makes the async surface call ``write_before_dispatch``
    inline) would run the disk I/O on the event-loop thread — which is the
    exact ADVISORY (PR-review #335 round-4) this refactor exists to fix.
    We compare the thread that actually performs the ``os.replace`` step of
    the atomic write against the thread the awaiting coroutine is on: they
    MUST differ. ``asyncio.to_thread`` uses the running loop's default
    executor, which is a distinct ``ThreadPoolExecutor`` worker.
    """
    calling_thread = threading.get_ident()
    replace_thread: dict[str, int | None] = {"tid": None}
    real_replace = os.replace

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replace_thread["tid"] = threading.get_ident()
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", spy_replace)
    writer = SpecPinWriter(pin_target_dir=tmp_path)
    await writer.write_before_dispatch_async(Role.IMPLEMENTER, _THREAD_ID)

    assert replace_thread["tid"] is not None, "os.replace spy was never called"
    assert replace_thread["tid"] != calling_thread, (
        "the blocking os.replace ran on the event-loop thread — "
        "write_before_dispatch_async is expected to offload sync I/O to "
        "the default thread-pool executor via asyncio.to_thread "
        "(PR-review #335 round-4 ADVISORY: no sync I/O on the async loop)"
    )
    # And the pin still landed correctly, so the offload didn't lose the write.
    assert (tmp_path / ".mindwire" / "pin").is_file()
