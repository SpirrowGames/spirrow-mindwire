"""Read-only trust check for a pooled clone slot (T-ephemeral-impl-clone-slot-per-thread).

The human ruling of 2026-11-17 (msg-9385, option A; design msg-9386 D-1 改訂 7) fixes the
slots at ``<slots_root>/<repo>/slot-NN/`` and has a person mark each of them trusted ONCE, at
deploy time (``docs/deploy.md``). MindWire never writes Claude Code's config file: a write races
a running Claude Code's own update of the same file and can silently drop it, which is the
"silent stop" this thread exists to remove. So this module only reads.

Measured on the loop host:

* trust does **not** inherit from a parent directory: ``C:/workspace`` is trusted, yet
  ``claude --bg`` in a git root below it exits 1 at once with ``Workspace not trusted`` (first
  measured 2026-11-08, re-measured 2026-11-08T17:09Z, Claude Code 2.1.319; re-observed
  2026-11-17 on Claude Code 2.1.325 in ``C:/workspace/sandbox/mindwire-impl`` and in a fresh
  git root under it). :func:`is_trusted` therefore looks at the slot's own entry only and never
  walks to an ancestor: the slot is its own git root;
* the entry Claude Code reads is ``projects["<absolute path, forward slashes>"]
  .hasTrustDialogAccepted = true`` in ``<config dir>/.claude.json``; every key on the loop host's
  real file (read-only, 2026-11-17) has an upper-case drive letter and forward slashes
  (``C:/workspace/spirrow/spirrow-mindwire``). :func:`project_key` produces exactly that shape.
  A lower-case-drive key was NOT measured (that needs a write this module must not make), so it
  is not accepted: a key Claude Code might not honour would make a slot look trusted and then
  fail its launch.

A missing, unreadable or non-JSON config raises :class:`TrustReadError` (the caller parks the
turn that wanted the slot, loudly); it is never created or repaired here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TRUST_KEY = "hasTrustDialogAccepted"


class TrustReadError(RuntimeError):
    """The trust state could not be read; the message says why, with the path."""


def claude_config_file(env: Mapping[str, str] | None = None) -> Path:
    """``$CLAUDE_CONFIG_DIR/.claude.json`` when the variable is set, else ``~/.claude.json``."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home()) / ".claude.json"


def project_key(slot_dir: Path) -> str:
    """The key Claude Code uses for a project: absolute path, forward slashes, upper-case drive."""
    key = str(Path(os.path.abspath(slot_dir))).replace("\\", "/")
    if len(key) >= 2 and key[1] == ":" and key[0].isalpha():
        key = key[0].upper() + key[1:]
    return key


def _read(config: Path) -> dict[str, Any]:
    try:
        raw = config.read_bytes()
    except OSError as exc:
        raise TrustReadError(f"cannot read {config}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustReadError(f"{config} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TrustReadError(f"{config} is not a JSON object")
    return data


def is_trusted(slot_dir: Path, config: Path | None = None) -> bool:
    """Whether ``slot_dir``'s own entry is trusted. Reads ``config``; never writes anything."""
    data = _read(config if config is not None else claude_config_file())
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    entry = projects.get(project_key(slot_dir))
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True
