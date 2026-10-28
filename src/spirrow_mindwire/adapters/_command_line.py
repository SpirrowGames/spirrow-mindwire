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
with the same prompt share one file, so concurrent spawns cannot remove a file
another session is still starting from, and there is no cleanup to get wrong.
A prompt changes only when the code or a repo's manifest does, so the
directory holds one small file per (prompt, repo) a deploy has used.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
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
    return Path(tempfile.gettempdir()) / "spirrow-mindwire" / "system-prompts"


def system_prompt_file(text: str, *, directory: Path | None = None) -> SystemPromptFile:
    """Write ``text`` to a content-addressed file and return the SDK option for it.

    Written as UTF-8 with no newline translation, so the CLI reads back exactly
    the string the adapter rendered. An existing file of the same name already
    holds this content and is left alone; a new one is written to a temporary
    name and renamed into place, so a concurrent reader never sees half a file.
    """
    data = text.encode("utf-8")
    target_dir = directory if directory is not None else _default_prompt_dir()
    path = target_dir / f"{hashlib.sha256(data).hexdigest()}.md"
    if not path.is_file():
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
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
