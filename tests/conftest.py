"""Shared pytest fixtures for spirrow-mindwire."""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.adapters import _command_line
from spirrow_mindwire.obligations import ObligationsManifest, load_manifest


@pytest.fixture(scope="session")
def obligations() -> ObligationsManifest:
    """The in-repo loop-readable obligations manifest, loaded once per test session.

    Adapters take the manifest by constructor injection (no module-global path
    read), so nearly every test that spins up an ``ImplementerSdkAdapter`` or
    ``NaysayerSdkAdapter`` needs one. Cached at session scope because the
    manifest is immutable and loading it re-parses YAML.
    """
    return load_manifest()


@pytest.fixture(scope="session")
def _system_prompt_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("system-prompts")


@pytest.fixture(autouse=True)
def _isolate_system_prompt_dir(monkeypatch: pytest.MonkeyPatch, _system_prompt_dir: Path) -> None:
    """Keep role adapters' prompt files out of the developer's real cache.

    Every adapter built in a test writes its system prompt to
    ``adapters._command_line._default_prompt_dir()``, and the prompt embeds the
    test's own ``tmp_path``, so each test makes a new file. Left pointing at
    ``%LOCALAPPDATA%`` / ``~/.cache``, one local run of the suite added about
    60 files (2 MB) there that nothing ever removes. Tests that check the real
    default keep a reference taken at import time.
    """
    monkeypatch.setattr(_command_line, "_default_prompt_dir", lambda: _system_prompt_dir)


#: The marker line preflight P3 requires in ``pre-push`` (``preflight._P3_PRE_PUSH_MARKER``).
GUARD_PRE_PUSH_MARKER = "# mindwire-guard: non-ff-expect v1"


def make_guard_hooks(hooks: Path) -> Path:
    """A stand-in for the spirrow-docs ``githooks`` directory (A8, Bohr msg-10088).

    ``pre-commit`` and ``pre-push`` both start with a shebang, are executable, and exit 0, so a
    slot armed with this directory passes preflight P3 and still commits and pushes in tests.
    """
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_bytes(b"#!/bin/sh\nexit 0\n")
    (hooks / "pre-push").write_bytes(
        b"#!/bin/sh\n" + GUARD_PRE_PUSH_MARKER.encode("ascii") + b"\nexit 0\n"
    )
    for name in ("pre-commit", "pre-push"):
        (hooks / name).chmod(0o755)
    return hooks


@pytest.fixture
def slot_hooks(tmp_path: Path) -> Path:
    """The hooks directory a borrowed slot is armed with (``SlotPrep(hooks_path=...)``)."""
    return make_guard_hooks(tmp_path / "guard-hooks")
