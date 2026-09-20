"""Regression tests for the hand-run PR-gate driver (``scripts/naysayer_review.py``).

The narrowly-scoped sentinel for T-hand-fired-gate-cannot-name-the-implementer msg-3885:
the manual lane MUST wire the roster resolver's return value into
``PrReviewOrchestrator.fire_pr_review(..., implementer=...)``. Before this landed the driver
called the orchestrator with no ``implementer`` kwarg (falling back to the old
``implementer=None`` default), so every REQUEST_CHANGES verdict fired by hand routed to
``NEXT: human`` and parked the design thread.

Two tests, matching msg-3849's scope: (1) valid roster → implementer flows through, (2) bad
roster → fail-loud exit 3 before the paid Gemini review runs. Deliberately NOT an end-to-end
byte-parity test against the conductor lane — that was rejected as OverScope in msg-3849 §Obj-2
(the required kwarg is enforced by the type system; a string-diff test would test ``str.format``,
not the wiring).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Never

import pytest

from spirrow_mindwire.value_objects import Role

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "naysayer_review.py"


def _load_cli_module() -> Any:
    """Load ``scripts/naysayer_review.py`` as a module (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("_naysayer_review_cli_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


class _StubConductorConfig:
    def __init__(self, roster: dict[str, Role]) -> None:
        self.roster = roster


class _StubSettings:
    def __init__(self, roster: dict[str, Role]) -> None:
        self.conductor = _StubConductorConfig(roster)


class _FakeDriver:
    """Minimal stand-in for :class:`NaysayerPrReviewDriver` — no Lexora, no GitHub."""

    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class _CaptureOrchestrator:
    """Records the kwargs of ``fire_pr_review`` so the test can pin ``implementer=``."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls: list[dict[str, object]] = []

    async def fire_pr_review(self, **kwargs: object) -> tuple[Any, Any, dict[str, Any]]:
        self.calls.append(dict(kwargs))
        # Return a shape ``main()`` can consume: thread_ref, outcome, relay.
        outcome = _FakeOutcome()
        thread_ref = _FakeThreadRef()
        relay = {"msg_id": "msg-42"}
        return thread_ref, outcome, relay


class _FakeOutcome:
    def __init__(self) -> None:
        self.verdict = _FakeVerdict()
        self.ci_state = _FakeCiState()
        self.body = "critique body"
        self.head_sha = "deadbeef"


class _FakeVerdict:
    value = "approve"


class _FakeCiState:
    value = "success"


class _FakeThreadRef:
    thread_id = "T-pr-review-r-7"


def _patch_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    roster: dict[str, Role],
    capture: _CaptureOrchestrator | None = None,
    driver: _FakeDriver | None = None,
) -> None:
    """Redirect the CLI module's globals to the test doubles — no network, no billed review."""
    monkeypatch.setattr(_MODULE, "load_settings", lambda: _StubSettings(roster))
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda *a, **k: object())
    monkeypatch.setattr(_MODULE, "NaysayerPrReviewDriver", lambda *a, **k: driver or _FakeDriver())
    if capture is not None:
        monkeypatch.setattr(_MODULE, "PrReviewOrchestrator", lambda *a, **k: capture)


def test_manual_lane_wires_resolver_return_value_into_implementer_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel for msg-3885 D-3: the resolver's output MUST land on ``implementer=``.

    Regression on the exact defect: a re-introduced ``fire_pr_review(...)`` call without
    ``implementer=`` (or with a hardcoded literal instead of the resolver's output) would flip
    this test. This is the ~10-line canary msg-3849 §Substitute traded the OverScope parity
    test for.
    """
    capture = _CaptureOrchestrator()
    _patch_env(
        monkeypatch,
        roster={
            "Bohr": Role.PROPOSER,
            "Heisenberg": Role.IMPLEMENTER,
            "Einstein": Role.NAYSAYER,
        },
        capture=capture,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["naysayer_review.py", "--pr", "o/r#7", "--design-thread", "T-some-design-thread"],
    )
    asyncio.run(_MODULE.main())
    assert len(capture.calls) == 1
    call = capture.calls[0]
    # The load-bearing assertion: the resolver returned "Heisenberg" and the driver passed
    # THAT to ``implementer=`` — this is exactly what the msg-3842 defect ate.
    assert call["implementer"] == "Heisenberg"
    # Also pin the other required kwargs so a future signature change surfaces here.
    assert call["project"] == "spirrow-mindwire"
    assert call["pr_ref"] == "o/r#7"
    assert call["design_thread"] == "T-some-design-thread"


def test_manual_lane_fails_loud_with_exit_3_when_no_implementer_in_roster(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """msg-3885 D-2: bad roster → SystemExit(3) BEFORE the gate is billed.

    The gate must not fire (no orchestrator constructed, no driver constructed), and stderr
    must carry the exception's own message so an operator sees WHY resolution failed.
    """
    orchestrator_calls: list[object] = []

    def _fail_if_constructed(*_a: object, **_k: object) -> Never:
        orchestrator_calls.append(_a)
        raise AssertionError("PrReviewOrchestrator must not be constructed when the resolver fails")

    monkeypatch.setattr(_MODULE, "load_settings", lambda: _StubSettings({"Bohr": Role.PROPOSER}))
    monkeypatch.setattr(_MODULE, "PrReviewOrchestrator", _fail_if_constructed)
    monkeypatch.setattr(_MODULE, "NaysayerPrReviewDriver", _fail_if_constructed)
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", _fail_if_constructed)
    monkeypatch.setattr(
        sys,
        "argv",
        ["naysayer_review.py", "--pr", "o/r#7", "--design-thread", "T-some-design-thread"],
    )
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(_MODULE.main())
    assert excinfo.value.code == 3
    err = capsys.readouterr().err
    assert "no IMPLEMENTER persona" in err
    # And nothing downstream was constructed — the fail-loud aborted before any billing surface.
    assert orchestrator_calls == []
