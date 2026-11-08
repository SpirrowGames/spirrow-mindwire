"""Register a new clone slot as a trusted Claude Code workspace (A-4, msg-8053 / msg-8234).

Measured on the loop host (T-ephemeral-impl-clone-slot-per-thread):

* trust does **not** inherit from a parent directory: ``C:/workspace`` is trusted, yet
  ``claude --bg --dangerously-skip-permissions`` in a new directory under it exits 1 within a
  second with ``Workspace not trusted`` on stderr. No interactive prompt appears and the process
  does not wait for input; nothing is written to ``.claude.json``, so a second launch in the
  same directory fails the same way. Re-measured 2026-11-08T17:09Z (Claude Code 2.1.319, stdin
  closed, 60 s tree-kill bound, Bohr msg-8502), first measured 2026-11-08 in the retreated WIP;
* an entry ``projects["<absolute path, forward slashes>"].hasTrustDialogAccepted = true`` in
  ``<config dir>/.claude.json`` is enough: the same launch, with only that entry added to an
  otherwise empty config, prints ``backgrounded`` and exits 0. (Measured once, 2026-11-08, in
  the retreated WIP session; not re-measured since — step 1b confirms it.)

So every slot is registered once, when it is provisioned. The config file is Claude Code's own
(``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``). It is never created here: a missing
or unreadable file raises, which fails the provision loudly (counted, then parked, by
:class:`~spirrow_mindwire.clone_slot_prep.SlotPrep`) instead of inventing a user config; a file
that is not JSON is never overwritten to "fix" it. The file is written only when the entry is
missing (once per slot path). The write (Bohr msg-8505 / msg-8507, Einstein go) is guarded:

* every byte outside the edited entry is kept — the writer refuses a file whose untouched content
  would not re-serialize to exactly its own bytes (Claude Code's own ``indent=2`` shape does:
  checked against the loop host's real file 2026-11-08, read-only);
* temp file in the same directory + ``os.replace``; right before the replace the file is read
  again and compared BYTE for byte with what was read (mtime is too coarse); any difference
  throws the temp file away and starts over;
* afterwards the entry is read back; up to 3 rounds, then ``claude.json changed during update``
  (or ``entry missing after write``) fails the provision.

Our own writers never overlap: ``prepare_slot`` runs inside slot-prep, one at a time under
``conductor-tick.lock`` and the per-candidate synchronous sweep, so the only other writer is a
running Claude Code. Known gap: a write that lands between the compare and the replace
(milliseconds, once per slot) is still lost, as between two Claude Code processes. Entries are
never removed in PR-A, so ``projects`` grows by one key per thread slot.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

TRUST_KEY = "hasTrustDialogAccepted"


class TrustRegistrationError(RuntimeError):
    """The slot could not be registered as trusted; the message says why, with the path."""


def claude_config_file(env: Mapping[str, str] | None = None) -> Path:
    """``$CLAUDE_CONFIG_DIR/.claude.json`` when the variable is set, else ``~/.claude.json``."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home()) / ".claude.json"


def project_key(slot_dir: Path) -> str:
    """The key Claude Code uses for a project: absolute path, forward slashes, upper-case drive.

    Matches the measured key shape (``C:/workspace``): a key in another shape registers nothing
    the launch will find, so the slot would be refused although "registered".
    """
    key = str(Path(os.path.abspath(slot_dir))).replace("\\", "/")
    if len(key) >= 2 and key[1] == ":" and key[0].isalpha():
        key = key[0].upper() + key[1:]
    return key


def _parse(raw: bytes, config: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustRegistrationError(f"{config} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TrustRegistrationError(f"{config} is not a JSON object")
    return data


def _read_bytes(config: Path) -> bytes:
    try:
        return config.read_bytes()
    except OSError as exc:
        raise TrustRegistrationError(f"cannot read {config}: {exc}") from exc


def _read(config: Path) -> dict[str, Any]:
    return _parse(_read_bytes(config), config)


def _serialize(data: dict[str, Any], suffix: str) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + suffix


def _entry_trusted(data: dict[str, Any], key: str) -> bool:
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    entry = projects.get(key)
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True


def is_trusted(slot_dir: Path, config: Path) -> bool:
    return _entry_trusted(_read(config), project_key(slot_dir))


MAX_ATTEMPTS = 3
CHANGED_DURING_UPDATE = "claude.json changed during update"


def _edit(
    config: Path,
    mutate: Callable[[dict[str, Any]], bool],
    check: Callable[[dict[str, Any]], bool],
    *,
    before_replace: Callable[[], None] | None = None,
) -> bool:
    """Read-modify-replace ``config`` without clobbering a concurrent writer (Bohr msg-8507).

    ``mutate`` edits the parsed object and returns ``False`` when nothing needs writing (then
    nothing is written). The original BYTES are kept; right before ``os.replace`` the file is read
    again and the temp file is thrown away unless it is byte-identical (not mtime: too coarse).
    After the replace, ``check`` must hold on a fresh read. Up to :data:`MAX_ATTEMPTS` rounds; then
    :class:`TrustRegistrationError`. Every byte outside the edited entry is kept: the write is
    refused when the untouched file would not re-serialize to exactly its own bytes. Known gap:
    a write landing between the compare and the replace (milliseconds, once per slot) is lost.
    """
    last = ""
    for _ in range(MAX_ATTEMPTS):
        raw = _read_bytes(config)
        data = _parse(raw, config)
        text = raw.decode("utf-8")
        suffix = text[len(text.rstrip()) :]
        if _serialize(data, suffix) != text:
            raise TrustRegistrationError(
                f"{config}: not in the shape this writer reproduces byte for byte; "
                "refusing to rewrite it"
            )
        if not mutate(data):
            return False
        fd, tmp = tempfile.mkstemp(prefix=".claude.json.", suffix=".tmp", dir=config.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(_serialize(data, suffix))
            if before_replace is not None:
                before_replace()
            if _read_bytes(config) != raw:
                last = CHANGED_DURING_UPDATE
                os.unlink(tmp)
                continue
            os.replace(tmp, config)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise TrustRegistrationError(f"cannot write {config}: {exc}") from exc
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        if check(_read(config)):
            return True
        last = "entry missing after write"
    raise TrustRegistrationError(f"{config}: {last} ({MAX_ATTEMPTS} attempts)")


def ensure_trusted(
    slot_dir: Path, config: Path, *, before_replace: Callable[[], None] | None = None
) -> bool:
    """Make ``slot_dir`` a trusted project in ``config``. ``True`` = the file was written."""
    key = project_key(slot_dir)

    def mutate(data: dict[str, Any]) -> bool:
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise TrustRegistrationError(f"{config}: 'projects' is not a JSON object")
        entry = projects.setdefault(key, {})
        if not isinstance(entry, dict):
            raise TrustRegistrationError(f"{config}: projects[{key!r}] is not a JSON object")
        if entry.get(TRUST_KEY) is True:
            return False
        entry[TRUST_KEY] = True
        return True

    return _edit(config, mutate, lambda d: _entry_trusted(d, key), before_replace=before_replace)


def remove_project_entries(
    config: Path,
    matches: Callable[[str], bool],
    *,
    before_replace: Callable[[], None] | None = None,
) -> list[str]:
    """Drop every ``projects`` key for which ``matches`` holds; returns the keys dropped.

    Same guarded write as :func:`ensure_trusted`. Used by the trust measurement's cleanup only
    (Bohr msg-8507 Obj-22-2): entries go one by one, never by restoring a whole-file backup.
    """
    dropped: list[str] = []

    def mutate(data: dict[str, Any]) -> bool:
        dropped.clear()
        projects = data.get("projects")
        if not isinstance(projects, dict):
            return False
        dropped.extend(k for k in projects if matches(k))
        for k in dropped:
            del projects[k]
        return bool(dropped)

    def check(data: dict[str, Any]) -> bool:
        projects = data.get("projects")
        return not isinstance(projects, dict) or not any(matches(k) for k in projects)

    _edit(config, mutate, check, before_replace=before_replace)
    return list(dropped)


def trust_preparer(config: Path | None = None) -> Callable[[Path], None]:
    """``SlotPrep.prepare_slot`` that registers the new slot as trusted (raises on failure)."""

    def _prepare(slot_dir: Path) -> None:
        ensure_trusted(slot_dir, config if config is not None else claude_config_file())

    return _prepare
