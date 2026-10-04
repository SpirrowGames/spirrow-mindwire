"""T-tier-c-admission-gate U4c — the outputs U4b left out, added to ``tierc_gate_measure.py``.

Spec: Bohr msg-6488 steps A-D (E/F are in ``test_decision_log_audit.py``), endorsed by Einstein.
Like the rest of the script, every output is counts plus ``msg_id`` lists: no pass/fail verdict.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER

SINCE = datetime(2026, 9, 16, tzinfo=UTC)
UNTIL = datetime(2026, 9, 30, tzinfo=UTC)


def _measure() -> Any:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "tierc_gate_measure_u4c", root / "scripts" / "tierc_gate_measure.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tierc_gate_measure_u4c"] = mod
    spec.loader.exec_module(mod)
    return mod


MEASURE = _measure()


def _m(i: int, author: str, tail: str, day: int = 20) -> Any:
    return MEASURE.builder.RawMessage(
        msg_id=f"msg-{i}",
        author=author,
        content=f"body {i}\n\n{tail}",
        timestamp=datetime(2026, 9, day, 12, i, tzinfo=UTC),
        role=None,
        next_participant=None,
    )


def _classify(
    threads: list[tuple[str, str, list[Any]]],
    *,
    modes: Mapping[str, str] | None = None,
    must_admit: tuple[str, ...] = (),
) -> dict[str, Any]:
    return MEASURE.classify(  # type: ignore[no-any-return]
        threads,
        dict(ROSTER),
        since=SINCE,
        until=UNTIL,
        mode_by_project=modes,
        must_admit=must_admit,
    )


def test_a_admitted_share_is_split_by_label_and_adds_up() -> None:
    msgs = [
        _m(1, "Bohr", "TIER-C: goal\nNEXT: human"),
        _m(2, "Heisenberg", "TIER-C: goal\nNEXT: human"),
        _m(3, "Bohr", "TIER-C: cost\nNEXT: human"),
        _m(4, "Bohr", "TIER-C: unsure:goal?\nNEXT: human"),
        _m(5, "Bohr", "TIER-C: scope\nNEXT: human"),  # ii, not iii
        _m(6, "Bohr", "NEXT: human"),  # i
    ]
    out = _classify([("p", "T-a", msgs)], modes={"p": "off"})
    assert out["iii_admitted_by_label"] == {
        "cost": 1,
        "goal": 2,
        "irreversible": 0,
        "unsure:goal?": 1,
    }
    assert sum(out["iii_admitted_by_label"].values()) == out["counts"]["iii_admitted"] == 4
    assert out["iii_admitted_by_label_msg_ids"]["goal"] == ["p/T-a#msg-1", "p/T-a#msg-2"]


def test_b_a_bounced_must_admit_ref_is_dropped_and_an_unseen_one_is_not_found() -> None:
    msgs = [
        _m(1, "Bohr", "TIER-C: goal\nNEXT: human"),  # admitted
        _m(2, "Bohr", "NEXT: human"),  # bounced (no label)
        _m(3, "Bohr", "TIER-C: goal\nNEXT: human", day=1),  # outside the window
    ]
    refs = ("p/T-a#msg-1", "p/T-a#msg-2", "p/T-a#msg-3", "q/T-z#msg-9")
    out = _classify([("p", "T-a", msgs)], modes={"p": "off"}, must_admit=refs)
    assert out["must_admit"] == {
        "p/T-a#msg-1": "iii_admitted",
        "p/T-a#msg-2": "i_bounce",
        "p/T-a#msg-3": "not_found",
        "q/T-z#msg-9": "not_found",
    }
    assert out["must_admit_dropped"] == ["p/T-a#msg-2", "p/T-a#msg-3", "q/T-z#msg-9"]


def test_b_default_regression_set_is_magickit_msg_829_and_msg_740() -> None:
    assert MEASURE.DEFAULT_MUST_ADMIT == (
        "spirrow-magickit/T-merged-to-main-without-gate-artifact#msg-829",
        "spirrow-magickit/T-dashboard-system-page-retirement-unfiled#msg-740",
    )
    out = MEASURE.classify([], dict(ROSTER), since=SINCE, until=UNTIL)
    assert set(out["must_admit_dropped"]) == set(MEASURE.DEFAULT_MUST_ADMIT)


def test_c_bounces_split_by_each_projects_mode() -> None:
    threads = [
        (
            "enf",
            "T-1",
            [_m(1, "Bohr", "NEXT: human"), _m(2, "Bohr", "TIER-C: other: x\nNEXT: human")],
        ),
        ("shd", "T-2", [_m(1, "Heisenberg", "NEXT: human")]),
        ("unk", "T-3", [_m(1, "Bohr", "NEXT: human")]),
    ]
    out = _classify(threads, modes={"enf": "enforce", "shd": "off"})
    assert out["i_bounce_by_mode"] == {"enforced": 2, "shadow": 1, "mode_unmeasured": 1}
    assert sum(out["i_bounce_by_mode"].values()) == out["counts"]["i_bounce"]
    assert out["i_bounce_by_mode_msg_ids"]["shadow"] == ["shd/T-2#msg-1"]
    assert out["mode_by_project"]["enf"] == "enforce"
    assert out["mode_by_project"]["unk"].startswith("unmeasured:")


def test_c_a_missing_config_reads_as_unmeasured_not_the_default(tmp_path: Path) -> None:
    modes = MEASURE.modes_from(("a", "b"), MEASURE.load_config(tmp_path / "absent.toml"))
    assert set(modes) == {"a", "b"}
    assert all(v.startswith("unmeasured: config not found") for v in modes.values())


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_c_the_configured_mode_is_read_for_every_project(tmp_path: Path, mode: str) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(f'[tierc_gate]\nmode = "{mode}"\n', encoding="utf-8")
    assert MEASURE.modes_from(("a", "b"), MEASURE.load_config(cfg)) == {"a": mode, "b": mode}


def test_c_an_unreadable_config_reads_as_unmeasured(tmp_path: Path) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text('[tierc_gate]\nmode = "bogus"\n', encoding="utf-8")
    modes = MEASURE.modes_from(("a",), MEASURE.load_config(cfg))
    assert modes["a"].startswith("unmeasured: config unreadable")


def test_d_the_script_uses_the_conductors_resolver() -> None:
    from spirrow_mindwire.conductor.core import roster_role

    assert MEASURE.roster_role is roster_role
    assert not hasattr(MEASURE, "_role_of")


@pytest.mark.parametrize("kind", ["malformed", "missing"])
def test_c_main_measures_nothing_without_a_readable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """No roster, no counts: an empty stand-in roster would report a false clean 0 / 0 / 0.

    A bad ``--config`` is reported, not a crash; but nothing is fetched and no bucket,
    ``must_admit`` or mode output is written — only the reason, with a non-zero exit.
    """
    cfg = tmp_path / "mindwire.toml"
    if kind == "malformed":
        cfg.write_text('[tierc_gate]\nmode = "bogus"\n', encoding="utf-8")
    out = tmp_path / "out.json"
    fetched: list[object] = []

    async def _harvest(projects: object) -> list[object]:
        fetched.append(projects)
        return []

    monkeypatch.setattr(MEASURE.builder, "harvest", _harvest)
    rc = MEASURE.main(
        [
            "--since",
            "2026-09-01",
            "--until",
            "2026-10-01",
            "--project",
            "a",
            "--out",
            str(out),
            "--config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert fetched == []
    result = json.loads(out.read_text(encoding="utf-8"))
    reason = {"malformed": "config unreadable", "missing": "config not found"}[kind]
    assert result["unmeasured"].startswith(f"unmeasured: {reason}")
    for key in ("counts", "msg_ids", "must_admit", "must_admit_dropped", "mode_by_project"):
        assert key not in result


def test_c_main_measures_with_a_readable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured roster and mode reach the output when the config reads."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text('[tierc_gate]\nmode = "enforce"\n', encoding="utf-8")
    out = tmp_path / "out.json"

    async def _no_threads(projects: object) -> list[object]:
        return []

    monkeypatch.setattr(MEASURE.builder, "harvest", _no_threads)
    rc = MEASURE.main(
        [
            "--since",
            "2026-09-01",
            "--until",
            "2026-10-01",
            "--project",
            "a",
            "--out",
            str(out),
            "--config",
            str(cfg),
        ]
    )
    assert rc == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["mode_by_project"] == {"a": "enforce"}
    assert "unmeasured" not in result


def test_h1_read_modes_is_gone_and_nothing_calls_it() -> None:
    """``read_modes`` was a test-only wrapper; tests run ``main``'s own sequence (U4d H)."""
    assert not hasattr(MEASURE, "read_modes")
    root = Path(__file__).resolve().parents[1]
    callers = [
        p
        for d in ("src", "scripts")
        for p in (root / d).rglob("*.py")
        if "read_modes" in p.read_text(encoding="utf-8")
    ]
    assert callers == []
