"""Tests for the identity findings read-out CLI (``scripts/identity_findings.py``).

Scope here is narrow and deliberate: the findings JSON must name the classification file
its numbers actually came from. The script is the "測る" half of msg-1491 §4 and its whole
value is being auditable — a reader has to be able to re-run it from what the artifact says
it read. An artifact that computes from ``--classification <override>`` while reporting the
default tree path is not reproducible from its own scope block, so the two pins below cover
both the default run and the override run.

The MCP fetch path is monkey-patched to isolate the tests from network I/O — same pattern as
``test_parked_humans_cli.py`` / ``test_head_skip_cli.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.identity import default_classification_path, load_legitimate_roles

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "identity_findings.py"

_OVERRIDE_YAML = """
version: 1
identities:
  - name: naysayer-pr-review
    kind: participant
    legitimate: ["naysayer"]
    primary_source: "src/spirrow_mindwire/orchestrator.py::x"
    reason: "the naysayer"
""".strip()


def _load_cli_module() -> Any:
    """Load the standalone CLI script as a module (it is not on the import path)."""
    spec = importlib.util.spec_from_file_location(
        "_identity_findings_cli_test_module", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


class _FakeMcp:
    """Stand-in for :class:`StreamableHttpChatroomMcp` — one thread, one in-scope message."""

    async def call_tool(self, name: str, params: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            if int(params.get("offset") or 0) > 0:
                return {"items": [], "total": 1}
            return {"items": [{"thread_id": "T-a"}], "total": 1}
        assert name == "chatroom_get_thread"
        return {
            "messages": [
                {
                    "msg_id": "msg-9999",
                    "author": "naysayer-pr-review",
                    "role": "naysayer",
                    "timestamp": "2026-08-20T00:00:00+00:00",
                }
            ]
        }


def _patch_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMcp()
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda *_a, **_kw: fake)


def _run_measure(classification_path: Path) -> dict[str, Any]:
    classification = load_legitimate_roles(classification_path)
    result: dict[str, Any] = asyncio.run(
        _MODULE._measure(
            projects=("spirrow-mindwire",),
            since_iso="2026-08-17T00:00:00+00:00",
            since_msg_id=None,
            url=None,
            classification=classification,
            classification_path=classification_path,
        )
    )
    return result


def test_measure_reports_the_path_it_was_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_measure`` serialises the caller's path, not a re-derived default.

    Regression pin: ``_measure`` used to hardcode ``str(default_classification_path())``
    into ``scope``, so an override run computed from one file and reported another.
    """
    _patch_mcp(monkeypatch)
    override = tmp_path / "override.yaml"
    override.write_text(_OVERRIDE_YAML, encoding="utf-8")

    result = _run_measure(override)

    assert result["scope"]["classification_path"] == str(override)
    assert result["scope"]["classification_path"] != str(default_classification_path())
    # The override really was the file the numbers came from.
    assert [e["raw_name"] for e in result["authors"]] == ["naysayer-pr-review"]


def test_main_threads_the_override_path_into_the_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: ``--classification`` reaches the emitted JSON's scope block.

    This is the operator-visible form of the bug — running the script with an override
    and getting an artifact that names the default path.
    """
    _patch_mcp(monkeypatch)
    override = tmp_path / "override.yaml"
    override.write_text(_OVERRIDE_YAML, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "identity_findings.py",
            "--project",
            "spirrow-mindwire",
            "--classification",
            str(override),
        ],
    )

    assert _MODULE.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"]["classification_path"] == str(override)


def test_default_run_still_names_the_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-override case is unchanged — the default path is still what gets reported."""
    _patch_mcp(monkeypatch)

    result = _run_measure(default_classification_path())

    assert result["scope"]["classification_path"] == str(default_classification_path())
