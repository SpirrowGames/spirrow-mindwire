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
:class:`~spirrow_mindwire.clone_slot_prep.SlotPrep`) instead of inventing a user config. Every
other key is kept as read; the write is a temp file + ``os.replace`` in the same directory, and the
entry is read back afterwards. A Claude Code process that rewrites the file from an older copy can
still drop the entry; the launch then fails as ``workspace-untrusted``, which ``claude_bg`` already
stands down loudly on.
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
    """The key Claude Code uses for a project: absolute path with forward slashes."""
    return str(Path(os.path.abspath(slot_dir))).replace("\\", "/")


def _read(config: Path) -> dict[str, Any]:
    try:
        text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustRegistrationError(f"cannot read {config}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrustRegistrationError(f"{config} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TrustRegistrationError(f"{config} is not a JSON object")
    return data


def is_trusted(slot_dir: Path, config: Path) -> bool:
    projects = _read(config).get("projects")
    if not isinstance(projects, dict):
        return False
    entry = projects.get(project_key(slot_dir))
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True


def ensure_trusted(slot_dir: Path, config: Path) -> bool:
    """Make ``slot_dir`` a trusted project in ``config``. ``True`` = the file was written."""
    data = _read(config)
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise TrustRegistrationError(f"{config}: 'projects' is not a JSON object")
    key = project_key(slot_dir)
    entry = projects.setdefault(key, {})
    if not isinstance(entry, dict):
        raise TrustRegistrationError(f"{config}: projects[{key!r}] is not a JSON object")
    if entry.get(TRUST_KEY) is True:
        return False
    entry[TRUST_KEY] = True
    fd, tmp = tempfile.mkstemp(prefix=".claude.json.", suffix=".tmp", dir=config.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, config)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise TrustRegistrationError(f"cannot write {config}: {exc}") from exc
    if not is_trusted(slot_dir, config):
        raise TrustRegistrationError(f"{config}: trust entry for {key} did not survive the write")
    return True


def trust_preparer(config: Path | None = None) -> Callable[[Path], None]:
    """``SlotPrep.prepare_slot`` that registers the new slot as trusted (raises on failure)."""

    def _prepare(slot_dir: Path) -> None:
        ensure_trusted(slot_dir, config if config is not None else claude_config_file())

    return _prepare
