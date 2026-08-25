"""Tests for the identity findings read-out CLI (``scripts/identity_findings.py``).

Two families of pins live here, both narrow and deliberate:

1. **Classification-path reporting.** The findings JSON must name the classification file
   its numbers actually came from. The script is the "測る" half of msg-1491 §4 and its whole
   value is being auditable — a reader has to be able to re-run it from what the artifact
   says it read. An artifact that computes from ``--classification <override>`` while
   reporting the default tree path is not reproducible from its own scope block.

2. **Cutoff parsing.** ``--since-created-at`` is a static CLI argument and must be parsed
   ONCE in ``main()``, fail-fast, before any network I/O. A per-message parse in the hot
   loop with a ``ValueError`` fallback that returns ``True`` (in-scope) would let a typo
   like ``2026/08/17`` silently promote the run to "no cutoff" and scan the entire project
   history — a serious fail-open. The pins cover both the fail-fast on a bad ISO string
   and the plumbing that reaches the per-message check with the parsed :class:`datetime`.

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
                    # The script filters on ``created_at`` (per the chatroom message
                    # schema); a fake using ``timestamp`` would leave ``created_at``
                    # missing and hit ``_in_scope``'s fail-open path, so the cutoff
                    # would never actually be exercised.
                    "created_at": "2026-08-20T00:00:00+00:00",
                }
            ]
        }


def _patch_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMcp()
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda *_a, **_kw: fake)


_SINCE_ISO = "2026-08-17T00:00:00+00:00"


def _run_measure(classification_path: Path) -> dict[str, Any]:
    classification = load_legitimate_roles(classification_path)
    result: dict[str, Any] = asyncio.run(
        _MODULE._measure(
            projects=("spirrow-mindwire",),
            since_iso=_SINCE_ISO,
            since_cutoff=_MODULE._parse_cutoff(_SINCE_ISO),
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


# ---------------------------------------------------------------------------
# Cutoff-parsing pins (round 4 blocking objection)
# ---------------------------------------------------------------------------


class _CutoffCheckMcp:
    """Fake that returns TWO messages: one before the cutoff, one after.

    The cutoff logic must drop the pre-cutoff one. If ``_in_scope`` is bypassed
    (e.g. because the fake used ``timestamp`` instead of ``created_at`` and hit
    the missing-field fail-open), BOTH messages end up counted — that is what
    this pin refuses.
    """

    async def call_tool(self, name: str, params: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            if int(params.get("offset") or 0) > 0:
                return {"items": [], "total": 1}
            return {"items": [{"thread_id": "T-a"}], "total": 1}
        assert name == "chatroom_get_thread"
        return {
            "messages": [
                {
                    "msg_id": "msg-0001",
                    "author": "naysayer-pr-review",
                    "role": "naysayer",
                    "created_at": "2026-08-10T00:00:00+00:00",  # BEFORE cutoff
                },
                {
                    "msg_id": "msg-9999",
                    "author": "naysayer-pr-review",
                    "role": "naysayer",
                    "created_at": "2026-08-20T00:00:00+00:00",  # AFTER cutoff
                },
            ]
        }


def test_in_scope_actually_drops_pre_cutoff_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cutoff is really applied — not silently bypassed by a missing field.

    Regression pin for the round-4 finding: the fake used ``timestamp`` and thus
    left ``created_at`` missing, so ``_in_scope`` returned True unconditionally
    and the test passed for the wrong reason. This pin uses ``created_at`` on
    two messages straddling the cutoff and asserts that exactly one survives.
    """
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda *_a, **_kw: _CutoffCheckMcp())

    classification = load_legitimate_roles(default_classification_path())
    result: dict[str, Any] = asyncio.run(
        _MODULE._measure(
            projects=("spirrow-mindwire",),
            since_iso=_SINCE_ISO,
            since_cutoff=_MODULE._parse_cutoff(_SINCE_ISO),
            since_msg_id=None,
            url=None,
            classification=classification,
            classification_path=default_classification_path(),
        )
    )

    assert result["totals"]["messages_scanned"] == 2
    assert result["totals"]["messages_in_scope"] == 1
    # The one that survived is the post-cutoff one.
    naysayer_entry = next(e for e in result["authors"] if e["raw_name"] == "naysayer-pr-review")
    assert naysayer_entry["post_count"] == 1


def test_typo_in_since_created_at_fails_fast_before_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A caller-bug typo in ``--since-created-at`` aborts before any MCP call.

    Regression pin for the round-4 finding: the old code parsed ``since_iso`` in
    ``_in_scope`` and returned True on ``ValueError``, so ``--since-created-at=2026/08/17``
    silently scanned the entire project history. Now it exits non-zero (exit code
    2 for "caller argument bug") and produces no JSON.
    """

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(
            "MCP was constructed despite an unparseable --since-created-at; "
            "the CLI must fail fast BEFORE any network I/O."
        )

    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "identity_findings.py",
            "--project",
            "spirrow-mindwire",
            "--since-created-at",
            "2026/08/17",  # slashes, not dashes — not ISO 8601
        ],
    )

    exit_code = _MODULE.main()

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # no JSON emitted
    assert "--since-created-at is not ISO 8601" in captured.err
    assert "'2026/08/17'" in captured.err


def test_parse_cutoff_normalises_z_suffix_and_naive_input() -> None:
    """``_parse_cutoff`` is the single place cutoff strings become aware datetimes.

    Pin: both a ``Z`` suffix (chatroom-native shape) and a naive ISO string must
    become tz-aware UTC. Any regression that returns a naive datetime here would
    make :func:`_in_scope` crash on the comparison — this catches it at parse time.
    """
    for iso in ("2026-08-17T00:00:00Z", "2026-08-17T00:00:00", "2026-08-17T00:00:00+00:00"):
        cutoff = _MODULE._parse_cutoff(iso)
        assert cutoff.tzinfo is not None, iso
        assert cutoff.utcoffset() is not None and cutoff.utcoffset().total_seconds() == 0.0


def test_parse_cutoff_rejects_non_iso_string() -> None:
    """The parser raises :class:`ValueError` on caller-visible typos.

    ``main()`` translates this into exit code 2; the parser itself must NOT
    swallow the error (fail-fast is the whole point of parsing once in main).
    """
    with pytest.raises(ValueError):
        _MODULE._parse_cutoff("2026/08/17")
