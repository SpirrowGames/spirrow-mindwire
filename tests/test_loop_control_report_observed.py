"""``scripts/loop_control.py --report-observed-hold``.

T-role-exit-leaves-clone-off-default-branch PR-B.

Pins the hold-only contract (Bohr msg-8840 Obj-1-2): the flag reports ``state=hold`` with the
``sweep:dirty-clone-parked`` actor exactly once; there is no value argument, so ``run`` or any other
value is rejected by argparse (exit 2) before any MCP call; a transport failure exits non-zero.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.magickit.client import MagickitMcpError


def _load() -> Any:
    script = Path(__file__).resolve().parents[1] / "scripts" / "loop_control.py"
    spec = importlib.util.spec_from_file_location("loop_control_report_module", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["loop_control_report_module"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def factory(self, _url: str | None) -> _Recorder:
        return self

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self.fail:
            raise MagickitMcpError("transport down")
        return {"ok": True}


def test_flag_reports_hold_once_with_sweep_actor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    rec = _Recorder()
    monkeypatch.setattr(mod, "StreamableHttpChatroomMcp", rec.factory)
    assert mod.main(["--report-observed-hold", "--project", "P"]) == 0
    assert rec.calls == [
        (
            "loop_control_report_observed",
            {"project": "P", "state": "hold", "actor": "sweep:dirty-clone-parked"},
        )
    ]
    assert '"reported": "hold"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--report-observed-hold", "run", "--project", "P"],
        ["--report-observed-hold", "anything", "--project", "P"],
        ["--report-observed", "hold", "--project", "P"],
        ["--report-observed", "run", "--project", "P"],
    ],
)
def test_any_value_is_rejected_before_the_reporter(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    mod = _load()
    rec = _Recorder()
    monkeypatch.setattr(mod, "StreamableHttpChatroomMcp", rec.factory)
    with pytest.raises(SystemExit) as exc:
        mod.main(argv)
    assert exc.value.code == 2
    assert rec.calls == []


def test_transport_failure_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    rec = _Recorder(fail=True)
    monkeypatch.setattr(mod, "StreamableHttpChatroomMcp", rec.factory)
    assert mod.main(["--report-observed-hold", "--project", "P"]) == 1
    assert len(rec.calls) == 1
    assert "did not land" in capsys.readouterr().err


def test_read_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the flag the script still only reads: no report_observed call."""
    mod = _load()
    rec = _Recorder()

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        rec.calls.append((name, arguments))
        return {"desired_state": "hold", "configured": True}

    monkeypatch.setattr(rec, "call_tool", call_tool)
    monkeypatch.setattr(mod, "StreamableHttpChatroomMcp", rec.factory)
    assert mod.main(["--project", "P"]) == 0
    assert [name for name, _ in rec.calls] == ["loop_control_get"]
