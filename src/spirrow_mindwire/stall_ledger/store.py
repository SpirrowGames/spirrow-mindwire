"""D-16ab open-record store: ``<DataDir>/state/stall-ledger.json``.

Spec: T-stalled-pr-has-no-detector msg-4685 §2, amended by msg-4687 §3, msg-4689 §2,
msg-4691 §2, msg-4693 §2-§3, msg-4695 §3, msg-4697 §1-§2 and msg-4699 §2.

Shape (msg-4693 §2)::

    {
      "schema_version": "1.0",
      "records": { "<unit.key>": { ...StallRecord... } },
      "quarantined_records": { "<key>": {reason, first_detected_at, last_detected_at,
                                          count, first_raw, last_raw} }
    }

Load order (msg-4691 §2, msg-4693 §3, msg-4695 §3). A file is moved aside ONLY at step
1 (not JSON / not an object) or at step 5a (proven v1, container broken); in every other
case this module refuses to write:

    0  file exists?                       no  -> ABSENT
    1  JSON object?                       no  -> CORRUPT (moved aside, continue as absent)
    2  schema_version "major.minor"?      no  -> VERSION_UNRECOGNIZED (halt, no write)
    3  major > 1?                         yes -> VERSION_AHEAD (halt, no write)
    4  major < 1?                         yes -> VERSION_UNRECOGNIZED (halt, no write)
    5a records is an object?              no  -> CORRUPT (moved aside)
       quarantined_records (if present) an object?  no -> CORRUPT (moved aside)
    5b each record has the v1 fields      no  -> that record is QUARANTINED
    5c each key == its record's unit.key  no  -> that record is QUARANTINED

Preservation (msg-4687 §3-1): every record is kept as the dict it was read. D-16ab
changes only ``klass`` / ``class_history`` on a record that stays open; it adds new
records and removes closed ones. D-16c adds one more change, :meth:`LoadedStore.settle_marker`:
``flags`` and ``evidence.marker_pending`` / ``evidence.marker_reason`` when an owed marker
fetch settles (msg-5746 §2). Unknown record keys and unknown top-level keys survive
untouched, and so do ``remedy_attempts`` and ``ladder_stage``.

Writes are temp file + ``os.replace`` (atomic rename). The single-writer rule (msg-4703
§4) is enforced by the caller holding :mod:`~spirrow_mindwire.stall_ledger.lock`; the
operator CLI never writes this file -- it drops a request file instead (msg-4699 §2).
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from spirrow_mindwire.stall_ledger.model import (
    ClassEntry,
    EmittedEventIds,
    RemedyAttempt,
    RemedyState,
    StallRecord,
    Unit,
    UnitKind,
)

STORE_FILENAME = "stall-ledger.json"
REQUESTS_DIRNAME = "stall-ledger.requests"

#: The major version this binary understands, and the version it writes on a new store.
KNOWN_MAJOR = 1
WRITE_VERSION = "1.0"

_VERSION_RE = re.compile(r"\A(?P<major>\d+)\.(?P<minor>\d+)\Z")


class StoreStatus(StrEnum):
    """The ``store`` field of the heartbeat line (msg-4685 §6, msg-4689 §2, msg-4691 §2)."""

    VALID = "valid"
    ABSENT = "absent"
    CORRUPT = "corrupt"
    VERSION_AHEAD = "version_ahead"
    VERSION_UNRECOGNIZED = "version_unrecognized"


class RecordInvalidError(ValueError):
    """A record failed 5b / 5c. ``reason`` is the quarantine reason string."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime")
    return value.astimezone(UTC).isoformat()


def _parse_iso(raw: object, name: str) -> datetime:
    if raw is None:
        raise RecordInvalidError(f"missing_field:{name}")
    if not isinstance(raw, str):
        raise RecordInvalidError(f"wrong_type:{name}")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordInvalidError(f"wrong_type:{name}") from exc
    if value.tzinfo is None:
        raise RecordInvalidError(f"wrong_type:{name}")
    return value


def _require(raw: dict[str, Any], name: str, kind: type | tuple[type, ...]) -> Any:
    if name not in raw:
        raise RecordInvalidError(f"missing_field:{name}")
    value = raw[name]
    if not isinstance(value, kind):
        raise RecordInvalidError(f"wrong_type:{name}")
    return value


def _str_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RecordInvalidError(f"wrong_type:{name}")
    return list(value)


def _parse_attempt(raw: object) -> RemedyAttempt:
    """A stored remedy attempt, read only far enough for ``origin()`` (msg-4687 §3-2).

    D-16ab never writes attempts; the refire unit owns their format. The fields
    ``origin()`` reads (``at``, ``state``, ``emitted_event_ids``) are required; the rest
    fall back to neutral values, since D-16ab never interprets them.
    """
    if not isinstance(raw, dict):
        raise RecordInvalidError("wrong_type:remedy_attempts")
    at = _parse_iso(raw.get("at"), "remedy_attempts")
    state_raw = raw.get("state")
    if not isinstance(state_raw, str) or state_raw not in {s.value for s in RemedyState}:
        raise RecordInvalidError("wrong_type:remedy_attempts")
    state = RemedyState(state_raw)
    ids = raw.get("emitted_event_ids")
    if not isinstance(ids, dict):
        raise RecordInvalidError("wrong_type:remedy_attempts")
    github_ids = _str_list(ids.get("github_review_ids", []), "remedy_attempts")
    chatroom_ids = _str_list(ids.get("chatroom_msg_ids", []), "remedy_attempts")
    head_sha = raw.get("head_sha")
    return RemedyAttempt(
        at=at,
        kind="gate-refire",
        head_sha=head_sha if isinstance(head_sha, str) else "",
        state=state,
        emitted_event_ids=EmittedEventIds(tuple(github_ids), tuple(chatroom_ids)),
    )


def record_from_json(key: str, raw: object) -> StallRecord:
    """5b + 5c: parse one stored record or raise :class:`RecordInvalidError`."""
    if not isinstance(raw, dict):
        raise RecordInvalidError("wrong_type:record")
    unit_raw = _require(raw, "unit", dict)
    kind = unit_raw.get("kind")
    identifier = unit_raw.get("identifier")
    if kind is None:
        raise RecordInvalidError("missing_field:unit.kind")
    if identifier is None:
        raise RecordInvalidError("missing_field:unit.identifier")
    if not isinstance(identifier, str) or not isinstance(kind, str):
        raise RecordInvalidError("wrong_type:unit")
    try:
        unit = Unit(UnitKind(kind), identifier)
    except ValueError as exc:
        raise RecordInvalidError("wrong_type:unit.kind") from exc
    stall_epoch_start = _parse_iso(raw.get("stall_epoch_start"), "stall_epoch_start")
    condition_first_seen_at = _parse_iso(
        raw.get("condition_first_seen_at"), "condition_first_seen_at"
    )
    klass = _require(raw, "klass", str)
    history_raw = _require(raw, "class_history", list)
    history: list[ClassEntry] = []
    for entry in history_raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("klass"), str):
            raise RecordInvalidError("wrong_type:class_history")
        history.append(
            ClassEntry(at=_parse_iso(entry.get("at"), "class_history"), klass=entry["klass"])
        )
    attempts = [_parse_attempt(a) for a in _require(raw, "remedy_attempts", list)]
    ladder_stage = _require(raw, "ladder_stage", str)
    flags = _str_list(_require(raw, "flags", list), "flags")
    evidence = _require(raw, "evidence", dict)
    if "motion_at_open" not in evidence:
        raise RecordInvalidError("missing_field:evidence.motion_at_open")
    _parse_iso(evidence["motion_at_open"], "evidence.motion_at_open")
    if unit.key != key:
        raise RecordInvalidError("key_mismatch")
    return StallRecord(
        unit=unit,
        stall_epoch_start=stall_epoch_start,
        condition_first_seen_at=condition_first_seen_at,
        klass=klass,
        class_history=history,
        remedy_attempts=attempts,
        ladder_stage=ladder_stage,
        flags=flags,
        evidence=dict(evidence),
    )


def new_record_json(
    *,
    unit: Unit,
    now: datetime,
    klass: str,
    flags: list[str],
    motion_at_open: datetime,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON of a record D-16ab opens (msg-4685 §4-2).

    ``evidence`` adds keys next to ``motion_at_open`` (D-16c: ``marker_reason``,
    ``marker_pending``; msg-5746 §2 A2').
    """
    return {
        "unit": {"kind": unit.kind.value, "identifier": unit.identifier},
        "stall_epoch_start": utc_iso(now),
        "condition_first_seen_at": utc_iso(now),
        "klass": klass,
        "class_history": [{"at": utc_iso(now), "klass": klass}],
        "remedy_attempts": [],
        "ladder_stage": "initial",
        "flags": list(flags),
        "evidence": {**(evidence or {}), "motion_at_open": utc_iso(motion_at_open)},
    }


@dataclass(frozen=True)
class QuarantineEvent:
    key: str
    reason: str
    first: bool  # True only on the first failure for this key (record_quarantined)


@dataclass
class LoadedStore:
    """The result of :func:`load_store`.

    ``envelope`` is ``None`` exactly when the store must not be written
    (``VERSION_AHEAD`` / ``VERSION_UNRECOGNIZED``).
    """

    status: StoreStatus
    envelope: dict[str, Any] | None
    records: dict[str, StallRecord] = field(default_factory=dict)
    quarantine_events: list[QuarantineEvent] = field(default_factory=list)
    quarantine_repeats: int = 0
    corrupt_moved_to: Path | None = None
    detail: str | None = None

    @property
    def writable(self) -> bool:
        return self.envelope is not None

    @property
    def records_raw(self) -> dict[str, Any]:
        assert self.envelope is not None
        records = self.envelope["records"]
        assert isinstance(records, dict)
        return records

    @property
    def quarantined(self) -> dict[str, Any]:
        assert self.envelope is not None
        quarantined = self.envelope.setdefault("quarantined_records", {})
        assert isinstance(quarantined, dict)
        return quarantined

    # ── mutations the driver makes ────────────────────────────────────────────────────

    def open_record(self, key: str, raw: dict[str, Any]) -> None:
        self.records_raw[key] = raw
        self.records[key] = record_from_json(key, raw)

    def reclassify(self, key: str, klass: str, now: datetime) -> bool:
        """Set the current class; append to ``class_history`` if it changed."""
        raw = self.records_raw[key]
        if raw.get("klass") == klass:
            return False
        raw["klass"] = klass
        raw["class_history"].append({"at": utc_iso(now), "klass": klass})
        self.records[key] = record_from_json(key, raw)
        return True

    def settle_marker(self, key: str, flags: list[str], marker_reason: str | None) -> None:
        """Settle an owed marker fetch (D-16c A5, msg-5746 §2).

        Sets ``flags``, clears ``evidence.marker_pending``, and sets or removes
        ``evidence.marker_reason``. Changes nothing else on the record.
        """
        raw = self.records_raw[key]
        raw["flags"] = list(flags)
        evidence = raw["evidence"]
        evidence.pop("marker_pending", None)
        if marker_reason is None:
            evidence.pop("marker_reason", None)
        else:
            evidence["marker_reason"] = marker_reason
        self.records[key] = record_from_json(key, raw)

    def close_record(self, key: str) -> None:
        del self.records_raw[key]
        del self.records[key]

    def clear_quarantined(self, key: str) -> bool:
        return self.quarantined.pop(key, None) is not None


def fresh_envelope() -> dict[str, Any]:
    return {"schema_version": WRITE_VERSION, "records": {}, "quarantined_records": {}}


def _move_aside(path: Path, now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    os.replace(path, target)
    return target


def _quarantine(
    quarantined: dict[str, Any], key: str, reason: str, raw: object, now: datetime
) -> bool:
    """Coalesce a record failure under its key (msg-4697 §1). True on the first failure."""
    entry = quarantined.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("count"), int):
        entry["reason"] = reason
        entry["last_detected_at"] = utc_iso(now)
        entry["count"] = entry["count"] + 1
        entry["last_raw"] = copy.deepcopy(raw)
        return False
    quarantined[key] = {
        "reason": reason,
        "first_detected_at": utc_iso(now),
        "last_detected_at": utc_iso(now),
        "count": 1,
        "first_raw": copy.deepcopy(raw),
        "last_raw": copy.deepcopy(raw),
    }
    return True


def validate_v1_records(envelope: dict[str, Any], now: datetime) -> LoadedStore:
    """Steps 5b / 5c. Only ever called on a store whose major version is 1 (msg-4691)."""
    records_raw = envelope["records"]
    quarantined = envelope.setdefault("quarantined_records", {})
    loaded = LoadedStore(status=StoreStatus.VALID, envelope=envelope)
    for key in list(records_raw):
        raw = records_raw[key]
        try:
            loaded.records[key] = record_from_json(key, raw)
        except RecordInvalidError as exc:
            del records_raw[key]
            first = _quarantine(quarantined, key, exc.reason, raw, now)
            loaded.quarantine_events.append(QuarantineEvent(key, exc.reason, first))
            if not first:
                loaded.quarantine_repeats += 1
    return loaded


def load_store(path: Path, now: datetime) -> LoadedStore:
    """Run the load order in the module docstring."""
    # Step 0
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoadedStore(status=StoreStatus.ABSENT, envelope=fresh_envelope())
    # Step 1
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        moved = _move_aside(path, now)
        return LoadedStore(
            status=StoreStatus.CORRUPT,
            envelope=fresh_envelope(),
            corrupt_moved_to=moved,
            detail="not a JSON object",
        )
    # Step 2
    version = data.get("schema_version")
    m = _VERSION_RE.match(version) if isinstance(version, str) else None
    if m is None:
        return LoadedStore(
            status=StoreStatus.VERSION_UNRECOGNIZED,
            envelope=None,
            detail=f"schema_version={version!r}",
        )
    major = int(m.group("major"))
    # Step 3
    if major > KNOWN_MAJOR:
        return LoadedStore(
            status=StoreStatus.VERSION_AHEAD, envelope=None, detail=f"schema_version={version}"
        )
    # Step 4
    if major < KNOWN_MAJOR:
        return LoadedStore(
            status=StoreStatus.VERSION_UNRECOGNIZED,
            envelope=None,
            detail=f"schema_version={version}",
        )
    # Step 5a
    container_ok = isinstance(data.get("records"), dict) and isinstance(
        data.get("quarantined_records", {}), dict
    )
    if not container_ok:
        moved = _move_aside(path, now)
        return LoadedStore(
            status=StoreStatus.CORRUPT,
            envelope=fresh_envelope(),
            corrupt_moved_to=moved,
            detail="records / quarantined_records is not an object",
        )
    # Steps 5b / 5c
    return validate_v1_records(data, now)


def save_store(path: Path, envelope: dict[str, Any]) -> None:
    """Temp file + atomic rename. One call per heartbeat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    data = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ─── Clear requests (msg-4699 §2) ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClearRequest:
    path: Path
    unit_key: str
    requested_by: str
    requested_at: str


@dataclass(frozen=True)
class InvalidRequest:
    path: Path
    detail: str


def write_clear_request(
    requests_dir: Path, unit_key: str, requested_by: str, now: datetime
) -> Path:
    """Drop one request file. Never touches the store (msg-4699 §2-1).

    Every request gets its own new file (temp name, then rename to a unique name), so no
    request can overwrite another and there is no read-modify-write anywhere.
    """
    requests_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"clear-{stamp}-{secrets.token_hex(6)}.json"
    target = requests_dir / name
    tmp = requests_dir / f"{name}.tmp"
    payload = {"unit_key": unit_key, "requested_by": requested_by, "requested_at": utc_iso(now)}
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def read_clear_requests(requests_dir: Path) -> tuple[list[ClearRequest], list[InvalidRequest]]:
    """Every ``clear-*.json`` in name order (names start with a timestamp)."""
    if not requests_dir.is_dir():
        return [], []
    valid: list[ClearRequest] = []
    invalid: list[InvalidRequest] = []
    for path in sorted(requests_dir.glob("clear-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            invalid.append(InvalidRequest(path, f"unreadable: {exc}"))
            continue
        if not isinstance(data, dict) or not isinstance(data.get("unit_key"), str):
            invalid.append(InvalidRequest(path, "no unit_key"))
            continue
        valid.append(
            ClearRequest(
                path=path,
                unit_key=data["unit_key"],
                # ``or ""``: an explicit JSON ``null`` reads as empty, never as the
                # string "None" (#362 gate advisory, Bohr msg-5157 §4).
                requested_by=str(data.get("requested_by") or ""),
                requested_at=str(data.get("requested_at") or ""),
            )
        )
    return valid, invalid


def load_store_readonly(path: Path) -> tuple[dict[str, Any], str]:
    """For ``--list-quarantined``: the quarantine map and a one-word store state.

    Never moves, repairs or writes anything -- listing is not a writer (msg-4699 §2-4).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, StoreStatus.ABSENT.value
    except (OSError, ValueError):
        return {}, StoreStatus.CORRUPT.value
    if not isinstance(data, dict):
        return {}, StoreStatus.CORRUPT.value
    quarantined = data.get("quarantined_records", {})
    if not isinstance(quarantined, dict):
        return {}, StoreStatus.CORRUPT.value
    return quarantined, f"schema_version={data.get('schema_version')!r}"
