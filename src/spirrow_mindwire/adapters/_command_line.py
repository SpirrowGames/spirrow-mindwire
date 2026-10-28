"""Keep a role session's CLI command line under the Windows limit.

The SDK turns ``ClaudeAgentOptions`` into one ``claude.exe`` command line, and a
string ``system_prompt`` goes on it verbatim as ``--system-prompt <text>``.
Windows refuses to start a process whose command line is longer than 32,767
characters, and says so as ``FileNotFoundError: [WinError 206]``, which the SDK
rewrites to ``Claude Code not found at: ...claude.exe`` — about a binary that
exists.

That happened on 2026-10-24 (thread
``T-incident-implementer-spawn-cmdline-too-long-2026-10-27``). PR #496 added one
ADR to ``spec/adr_index.yaml``; the implementer's system prompt carries that
index, so its command line grew to 32,838 characters, and every implementer
spawn on every project failed from then until the fix — 21 failures over three
days and ten quarantined threads, read as "the CLI is missing".

Two things here, and every SDK-backed role adapter uses both:

- :func:`system_prompt_file` moves the prompt off the command line. The SDK
  passes ``{"type": "file", "path": ...}`` as ``--system-prompt-file <path>``,
  so the command line no longer grows with the ADR index or the obligations.
  Measured on both CLIs the roles run (2.1.133 bundled, 2.1.311 host install):
  the prompt arrives intact, Japanese text included.
- :func:`check_command_line` measures the command line before spawning and
  raises :class:`CommandLineTooLongError` with the real number. If something
  else ever pushes it over again, the error says what happened instead of
  pointing at the binary.

The file is named by the SHA-256 of its content and never deleted. Sessions
with the same prompt share one file, and there is no cleanup to get wrong.
A prompt changes only when the code or a repo's manifest does, so the
directory holds one small file per (prompt, repo) a deploy has used.

Where it lives, and what is trusted (PR-gate on #499 @ 8b818c3):

- The directory is in the **user's own** cache (``%LOCALAPPDATA%`` on Windows,
  ``$XDG_CACHE_HOME`` or ``~/.cache`` elsewhere), not the shared temp dir. In a
  shared ``/tmp`` another local user could pre-create the directory (spawns
  fail) or a file under a name they can compute (the prompt is predictable, so
  is its hash). Nobody else can create entries in a user's home. On POSIX the
  directory is also created ``0700`` and refused if it is not ours or others
  can write to it.
- An existing file is used only if its bytes are exactly the prompt. Anything
  else — truncated, edited, planted — is rewritten.
- Two spawns may race to create the same file. On Windows the loser's
  ``os.replace`` fails while the winner's ``claude.exe`` has the file open; the
  file there is then already the right one, so the loser checks the bytes and
  goes on instead of failing its spawn.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import SystemPromptFile

__all__ = [
    "WINDOWS_COMMAND_LINE_LIMIT",
    "CommandLineTooLongError",
    "check_command_line",
    "command_line_length",
    "system_prompt_file",
]

# CreateProcessW's lpCommandLine limit, in characters, terminator excluded.
WINDOWS_COMMAND_LINE_LIMIT = 32767


class CommandLineTooLongError(RuntimeError):
    """The CLI command line the SDK would build exceeds the Windows limit."""

    def __init__(self, length: int, limit: int) -> None:
        super().__init__(f"command line too long ({length} > {limit})")
        self.length = length
        self.limit = limit


def _default_prompt_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "spirrow-mindwire" / "system-prompts"


def _ensure_private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32":
        st = directory.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise PermissionError(
                f"system-prompt directory {directory} is writable by other users or not ours "
                f"(uid={st.st_uid}, mode={oct(st.st_mode & 0o777)}); refusing to use it"
            )


def _holds(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def system_prompt_file(text: str, *, directory: Path | None = None) -> SystemPromptFile:
    """Write ``text`` to a content-addressed file and return the SDK option for it.

    Written as UTF-8 with no newline translation, so the CLI reads back exactly
    the string the adapter rendered. A file of the same name is reused only if
    it holds exactly these bytes; otherwise a new one is written to a temporary
    name and renamed into place, so a concurrent reader never sees half a file.
    """
    data = text.encode("utf-8")
    target_dir = directory if directory is not None else _default_prompt_dir()
    _ensure_private_dir(target_dir)
    path = target_dir / f"{hashlib.sha256(data).hexdigest()}.md"
    if not _holds(path, data):
        fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                os.replace(tmp, path)
            except PermissionError:
                # Lost a race on Windows: a concurrent spawn put the file there and its CLI
                # has it open. If what is there is ours byte for byte, nothing is lost.
                if not _holds(path, data):
                    raise
                Path(tmp).unlink(missing_ok=True)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return {"type": "file", "path": str(path)}


def command_line_length(options: Any) -> int:
    """Length of the command line the SDK would build for ``options``.

    Built by the SDK's own ``_build_command`` and quoted the way Windows
    receives it, so nothing about the flag layout is restated here. The binary
    is ``options.cli_path`` when set, else the bundled CLI, else ``claude`` —
    the SDK's later PATH search can only pick a path of similar length.
    """
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    transport = SubprocessCLITransport(prompt="", options=options)
    if transport._cli_path is None:
        transport._cli_path = transport._find_bundled_cli() or "claude"
    return len(subprocess.list2cmdline(transport._build_command()))


def check_command_line(options: Any, *, limit: int = WINDOWS_COMMAND_LINE_LIMIT) -> int:
    """Return the command-line length, or raise :class:`CommandLineTooLongError`."""
    length = command_line_length(options)
    if length > limit:
        raise CommandLineTooLongError(length, limit)
    return length
