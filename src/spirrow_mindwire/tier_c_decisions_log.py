"""JSONL decisions log — the single append-only surface for admission events.

The decisions log is a single append-only JSONL file, one event per line,
carrying every ``kind`` from :class:`~spirrow_mindwire.tier_c_admission_gate.LogKind`.
The design rationale in msg-3708 §2 (Bohr) and msg-3648 v1 §4 (Bohr) is
"single source of truth per file": no separate ledgers for bounces, label
migrations, and unsure admits — one physical file with a ``kind`` field per
row does the work of any number of conceptual ledgers.

Two writer classes exist by design (msg-3702 §3 書き出しマトリクス):

* **Infra sources** — the admission gate itself, the pre_gate_dispatcher,
  and the pr_gate_relay's `DEFERRED` path — call :func:`append_log_entry`
  directly. These sources emit structured
  :class:`~spirrow_mindwire.tier_c_admission_gate.LogEntry` values, so a
  direct API call is strictly better than emitting a text form and re-
  parsing it. Any indirection here would be the "hybrid & dual-management"
  bug Einstein flagged in msg-3701.

* **Workspace source** — the implementer role (LLM). It emits
  ``DECIDED:`` / ``DEFERRED:`` grammar prefixes as free-text lines at the
  tail of its handoff messages; the conductor's extraction step parses
  those lines and calls :func:`append_log_entry` with the same
  :class:`~spirrow_mindwire.tier_c_admission_gate.LogEntry` shape. The
  allowlist for extraction is ``{implementer}`` only (msg-3702 §1) so no
  infra author's message can leak through the LLM-oriented parser as
  false-positive events.

RETRY store
-----------

:func:`build_retry_lookup` returns the ``retry_lookup`` callable the
admission gate wants, scanning the log for the most recent ``BOUNCED``
entry with a given ``uuid`` and confirming no subsequent ``RETRY_ADMIT``
resolved it. The log is expected to be small enough (bounces are rare)
that a full scan per lookup is cheaper than a separate index, and
correctness on a small file beats a stale index every time — the
observability tag was measuring hundreds of events across a whole
month (msg-3630 §1). If the file ever grows past that scale a Bloom
filter or a rolling window index becomes the right optimisation; not
now.

Attribution
-----------

Design: T-tier-c-admission-gate. JSONL single-file discipline from
msg-3648 v1 §4 (Bohr); 6-kind enum from msg-3708 §2 (Bohr); allowlist
restriction to ``{implementer}`` from msg-3702 §1 (Bohr accepting
Einstein msg-3701 BLOCKING). The extraction callable belongs to the
conductor, not this module; the callable's contract is that it emits
:class:`~spirrow_mindwire.tier_c_admission_gate.LogEntry` values and
calls :func:`append_log_entry` — no other coupling.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .tier_c_admission_gate import LogEntry, LogKind, RetryLookup


def _entry_to_dict(
    entry: LogEntry,
    *,
    thread: str | None,
    msg_id: str | None,
) -> dict[str, Any]:
    """Materialise a :class:`LogEntry` as the on-disk JSONL row shape.

    The on-disk row carries the ``kind`` string plus the payload dict
    the gate produced, flattened one level so JSONL scanners can index
    on ``kind`` / ``thread`` / ``msg_id`` directly. ``thread`` and
    ``msg_id`` are optional: the admission gate does not have them (it
    is passed a body, not a message envelope), so the transport layer
    that wraps the gate call fills them in. When both are ``None``
    (unit tests, replay probes) they are omitted from the row.
    """
    row: dict[str, Any] = {"kind": entry.kind.value}
    if thread is not None:
        row["thread"] = thread
    if msg_id is not None:
        row["msg_id"] = msg_id
    # Payload keys live next to the top-level keys so ``jq '.kind'``
    # and ``jq '.reason'`` both work without a nested prefix. The
    # payload never carries ``kind`` / ``thread`` / ``msg_id`` itself
    # (those live above the payload), so the merge is unambiguous.
    row.update(entry.payload)
    return row


def append_log_entry(
    log_path: Path,
    entry: LogEntry,
    *,
    thread: str | None = None,
    msg_id: str | None = None,
) -> None:
    """Append a single :class:`LogEntry` to the JSONL log at ``log_path``.

    The write is a single ``open(..., "a")`` + one ``write`` + one
    newline. On POSIX a single ``write`` under the pipe-buffer size
    (4 KiB) is atomic — our rows are far smaller — so an interleaving
    writer never produces a torn line. On Windows the same guarantee
    is not formally documented but holds empirically for small writes
    to a text file opened in append mode.

    Parent directories are created if missing so a fresh install with
    an empty state directory does not fault the first time the gate
    fires. A permission or quota failure on the write is left to
    propagate — the caller must not silently drop admission events.
    """
    row = _entry_to_dict(entry, thread=thread, msg_id=msg_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def append_log_entries(
    log_path: Path,
    entries: Iterable[LogEntry],
    *,
    thread: str | None = None,
    msg_id: str | None = None,
) -> None:
    """Append several :class:`LogEntry` values in the given order.

    Convenience for the common case where a single admission call
    returns a small list of entries (a legacy admit is one
    ``LABEL_MIGRATION`` row; a RETRY admit on a legacy label is one
    ``LABEL_MIGRATION`` + one ``RETRY_ADMIT``, in that order). The
    RETRY lookup scans the file FORWARDS from oldest to newest row
    and lets the last observation for a given UUID win (see
    :func:`build_retry_lookup`) — so keeping the ``RETRY_ADMIT``
    strictly after the ``LABEL_MIGRATION`` it describes is a
    presentation convention (the log tells a coherent left-to-right
    story of what the gate did), not a correctness requirement.
    """
    for entry in entries:
        append_log_entry(log_path, entry, thread=thread, msg_id=msg_id)


def _iter_rows(log_path: Path) -> Iterable[dict[str, Any]]:
    """Yield each JSONL row in ``log_path``, skipping malformed rows.

    A malformed row is not a fatal condition here — the JSONL file
    could have been touched by an external tool, or a previous writer
    could have raced with a rotation. The gate's correctness never
    depends on a corrupt row being absent, only on the well-formed
    rows being present. So we skip malformed rows silently rather
    than raising, and let a future validator surface them.

    An empty file (or a missing file) yields no rows.
    """
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def build_retry_lookup(log_path: Path) -> RetryLookup:
    """Return a :data:`~spirrow_mindwire.tier_c_admission_gate.RetryLookup`
    for the log at ``log_path``.

    The returned callable ``(uuid, author) -> bool`` scans the log
    each time it is called. That is deliberately not memoised: the
    admission gate is invoked at most once per message, and the log
    scan is bounded by the number of bounces to date (small). A
    memoised lookup would cache stale answers whenever the RETRY_ADMIT
    a fresh admission wrote is not yet visible to a subsequent call
    in the same process — a category of bug the whole "no shared
    state" design of msg-3648 v1 §4 exists to avoid.

    The rule (msg-3710 §1 "RETRY_ADMIT が append されたら uuid は
    resolved"): a UUID is an unresolved bounce for an author when the
    LAST event in the log against the ``(retry_uuid, author)`` PAIR
    is a ``BOUNCED`` — i.e. no later ``RETRY_ADMIT`` for the SAME
    ``(uuid, author)`` pair has resolved it yet. Because a UUID may
    be reused across independent bounce/admit cycles (e.g. a sequence
    counter, or a hash collision in a short-token space), we cannot
    exit the scan at the first ``RETRY_ADMIT`` we see: a subsequent
    ``BOUNCED`` with the same UUID re-opens the state, and the
    truthful answer is the last observation. So the scan reads the
    whole file forwards, updating a single ``bounced`` flag as it
    goes, and returns whatever the final state was. This is the fix
    for PR-gate objection #322-1 (forward-scan) and #322-4
    (cross-author scoping).

    Author scoping (PR-gate objection #322-4): both the ``BOUNCED``
    open and the ``RETRY_ADMIT`` close must match the target author,
    not only the UUID. ``decide_admission`` stamps every entry with
    the message author it received, so a shared UUID between two
    authors (say Alice's and Bob's colliding sequence-counter values)
    keeps two independent flags. Absent this scoping, Bob's admission
    would clear Alice's unresolved bounce and lock her out of the
    retry path — the exact "trapping the user in an infinite loop"
    class of bug the RETRY prologue exists to prevent.

    Callers that want a different UUID field name should re-derive
    this callable — the field name is a contract between the gate's
    output shape and the log's on-disk format, and both live in the
    same package on purpose so a rename can be caught at type-check
    time.
    """

    def _lookup(uuid: str, author: str) -> bool:
        # Read the whole file forwards; let the last observation for
        # this (uuid, author) pair win. Rows that reference a
        # different UUID or a different author are irrelevant to this
        # author's retry state and are skipped whole.
        bounced = False
        for row in _iter_rows(log_path):
            if row.get("retry_uuid") != uuid:
                continue
            if row.get("author") != author:
                continue
            kind = row.get("kind")
            if kind == LogKind.BOUNCED.value:
                bounced = True
            elif kind == LogKind.RETRY_ADMIT.value:
                bounced = False
        return bounced

    return _lookup
