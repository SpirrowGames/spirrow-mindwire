"""D-16d-1 part 2: ``--report`` -- what the head cache and the ledger say, read from the store only.

Spec: T-stalled-pr-has-no-detector msg-8477 ⑤ (``--report`` reads the cache; it fetches no
body of its own), msg-8549 §1-§4 as amended by msg-8552 (Obj-7-1 / Obj-7-2).

**What it reads.** The store file, once, read-only: ``thread_heads`` (the cache the tick
keeps), ``thread_heads_pending`` (the heads the last evaluated tick could not read), ``records``
and ``quarantined_records``. It never calls the chatroom or GitHub, never takes the ledger
lock and never writes or moves anything -- a corrupt or missing store is reported as such,
not repaired.

**What it prints.**

* ``heads_by_group`` -- how many cached heads are in each of A-D. B (``STOP: done``) is
  counted here and nowhere else: a B thread never opens (msg-8549 §2 / msg-8552).
* ``pending_heads`` -- the unread heads as of the last tick that wrote the store (``count``,
  ``keys``). They read as C with 72h until a tick reads them.
* ``next_human`` -- the D heads that are ``NEXT: human``, count and list. They keep opening at
  72h like the rest of D (msg-8552 Obj-7-1); the report only shows them.
* ``open_records_by_group`` -- every open ledger record, under its thread's cached group. A
  thread record whose head is not cached goes under C with ``head_read: false`` (unread is C);
  a record that is not a thread (a PR) goes under ``not_thread``. Every entry of
  ``quarantined_records`` also goes under ``not_thread``, whatever its key, marked
  ``quarantined: true`` with its ``reason``: it is not a record the tick evaluates, so no
  head group applies to it.

The "``T-pr-review-*`` whose PR is merged or closed" form of B is not here: neither the store
nor the tick holds PR state, so it would always read "unknown" (msg-8552 Obj-7-2, moved to
D-16d-2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from spirrow_mindwire.stall_ledger.heads import CachedHead, HeadGroup
from spirrow_mindwire.stall_ledger.model import UnitKind
from spirrow_mindwire.stall_ledger.store import (
    PENDING_HEADS_KEY,
    THREAD_HEADS_KEY,
    StoreStatus,
)

#: ``open_records_by_group`` bucket for a record that is not a thread.
NOT_THREAD = "not_thread"

_GROUPS = tuple(g.value for g in HeadGroup)


def read_store_readonly(path: Path) -> tuple[dict[str, Any] | None, str]:
    """The store envelope and a one-word state. Never moves, repairs or writes anything."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, StoreStatus.ABSENT.value
    except (OSError, ValueError):
        return None, StoreStatus.CORRUPT.value
    if not isinstance(data, dict):
        return None, StoreStatus.CORRUPT.value
    return data, f"schema_version={data.get('schema_version')!r}"


def _mapping(envelope: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = envelope.get(key)
    return value if isinstance(value, Mapping) else {}


def build_report(envelope: Mapping[str, Any] | None, *, store_state: str) -> dict[str, Any]:
    """The report object (module docstring). Pure: reads ``envelope`` only."""
    envelope = envelope or {}
    heads: dict[str, CachedHead] = {}
    unparsed: list[str] = []
    for key, raw in _mapping(envelope, THREAD_HEADS_KEY).items():
        cached = CachedHead.from_json(raw)
        if cached is None:
            unparsed.append(key)  # the tick reads it again; not counted in any group
        else:
            heads[key] = cached

    by_group = dict.fromkeys(_GROUPS, 0)
    for cached in heads.values():
        by_group[cached.head.group.value] += 1

    next_human = sorted(
        key
        for key, cached in heads.items()
        if cached.head.group is HeadGroup.WAITING and cached.head.detail == "next_human"
    )

    pending_raw = _mapping(envelope, PENDING_HEADS_KEY)
    pending_keys_raw = pending_raw.get("keys")
    pending_keys = (
        sorted(k for k in pending_keys_raw if isinstance(k, str))
        if isinstance(pending_keys_raw, list)
        else []
    )

    open_by_group: dict[str, list[dict[str, Any]]] = {g: [] for g in (*_GROUPS, NOT_THREAD)}
    for key, raw in sorted(_mapping(envelope, "records").items()):
        record = raw if isinstance(raw, Mapping) else {}
        entry: dict[str, Any] = {
            "key": key,
            "stall_epoch_start": record.get("stall_epoch_start"),
            "klass": record.get("klass"),
        }
        if not key.startswith(f"{UnitKind.THREAD.value}:"):
            open_by_group[NOT_THREAD].append(entry)
            continue
        cached = heads.get(key)
        if cached is None:
            open_by_group[HeadGroup.UNEXPLAINED.value].append({**entry, "head_read": False})
            continue
        open_by_group[cached.head.group.value].append(
            {**entry, "head_read": True, "detail": cached.head.detail}
        )
    for key, raw in sorted(_mapping(envelope, "quarantined_records").items()):
        quarantined = raw if isinstance(raw, Mapping) else {}
        open_by_group[NOT_THREAD].append(
            {
                "key": key,
                "quarantined": True,
                "reason": quarantined.get("reason"),
                "first_detected_at": quarantined.get("first_detected_at"),
                "last_detected_at": quarantined.get("last_detected_at"),
            }
        )

    return {
        "kind": "stall_report",
        "store": store_state,
        "heads_by_group": by_group,
        "heads_unparsed": sorted(unparsed),
        "pending_heads": {
            "count": len(pending_keys),
            "keys": pending_keys,
        },
        "next_human": {"count": len(next_human), "threads": next_human},
        "open_records_by_group": open_by_group,
    }


def report_from_path(path: Path) -> dict[str, Any]:
    envelope, state = read_store_readonly(path)
    return build_report(envelope, store_state=state)


__all__ = ["NOT_THREAD", "build_report", "read_store_readonly", "report_from_path"]
