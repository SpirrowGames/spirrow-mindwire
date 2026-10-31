"""T-decider-conductor-hook 2e-1a-iv-split — ``iv_pr_gate_relay`` split by kind.

Spec: Bohr msg-7267 (design msg-7261 / 7263 / 7265, approved by Einstein msg-7266 / msg-7322 go).
``iv_by_kind`` puts every ``pr-gate-relay`` ``NEXT: human`` in exactly one of
``gate_ci_hold`` / ``gate_verdict`` / ``relay_notice``, using the production readers; the bucket
``iv_pr_gate_relay`` itself is unchanged. Fixture bodies are built with the production writers
where one exists, so a writer change that the reader still accepts keeps these tests honest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER

from spirrow_mindwire.conductor.gate_records import (
    RELAY_AUTHOR,
    render_ci_hold_marker,
    render_relay_heading,
)

SINCE = datetime(2026, 9, 16, tzinfo=UTC)
UNTIL = datetime(2026, 9, 30, tzinfo=UTC)
SHA = "224d06852d0438591610e2267068efb6c0075da1"
PR = "SpirrowGames/spirrow-mindwire#480"


def _measure() -> Any:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "tierc_gate_measure_iv_split", root / "scripts" / "tierc_gate_measure.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tierc_gate_measure_iv_split"] = mod
    spec.loader.exec_module(mod)
    return mod


MEASURE = _measure()


def _footer(event: str = "APPROVE", sha: str = SHA) -> str:
    return f"<!-- mindwire:verdict head_sha={sha} event={event} -->"


HEADING = render_relay_heading(PR, SHA)

#: One body per classification path (msg-7267 Tests list).
BODIES: dict[str, str] = {
    "approve_heading_and_footer": f"{HEADING}\n\nLGTM.\n\n{_footer()}\n\nNEXT: human",
    "legacy_heading_no_footer": f"{render_relay_heading(PR, None)}\n\nLGTM.\n\nNEXT: human",
    "heading_two_footers": (
        f"{HEADING}\n\nquoted {_footer('REQUEST_CHANGES')}\n\n{_footer()}\n\nNEXT: human"
    ),
    "footer_no_heading": f"Verdict for {PR}.\n\n{_footer('COMMENT')}\n\nNEXT: human",
    "ci_hold": (
        f"{HEADING}\n\nCI pending; holding.\n\n{render_ci_hold_marker(head=SHA)}\n\nNEXT: human"
    ),
    "stop_notice": "Conductor stop — stand-down\n\nThe loop stopped here.\n\nNEXT: human",
    "heading_quoted_later": f"Conductor stop — guard\n\nquoting:\n{HEADING}\n\nNEXT: human",
}

EXPECTED: dict[str, str] = {
    "approve_heading_and_footer": "gate_verdict",
    "legacy_heading_no_footer": "gate_verdict",
    "heading_two_footers": "gate_verdict",
    "footer_no_heading": "gate_verdict",
    "ci_hold": "gate_ci_hold",
    "stop_notice": "relay_notice",
    "heading_quoted_later": "relay_notice",
}


def _msg(i: int, author: str, content: str) -> Any:
    return MEASURE.builder.RawMessage(
        msg_id=f"msg-{i}",
        author=author,
        content=content,
        timestamp=datetime(2026, 9, 20, 12, i, tzinfo=UTC),
        role=None,
        next_participant=None,
    )


def _classify(msgs: list[Any]) -> dict[str, Any]:
    return MEASURE.classify(  # type: ignore[no-any-return]
        [("p", "T-iv", msgs)],
        dict(ROSTER),
        since=SINCE,
        until=UNTIL,
        mode_by_project={"p": "off"},
        must_admit=(),
    )


def test_fixture_bodies_exercise_the_intended_reader() -> None:
    """Guard the fixtures themselves: each body reaches the reader its path is about."""
    from spirrow_mindwire.conductor.gate_records import ci_hold_head, relay_heading_pr_ref
    from spirrow_mindwire.github.reviews import parse_verdict_footer

    assert relay_heading_pr_ref(BODIES["legacy_heading_no_footer"]) == PR
    assert parse_verdict_footer(BODIES["legacy_heading_no_footer"]) is None
    assert parse_verdict_footer(BODIES["heading_two_footers"]) is None
    assert relay_heading_pr_ref(BODIES["footer_no_heading"]) is None
    assert parse_verdict_footer(BODIES["footer_no_heading"]) is not None
    assert ci_hold_head(BODIES["ci_hold"]) is not None
    assert relay_heading_pr_ref(BODIES["ci_hold"]) == PR
    assert relay_heading_pr_ref(BODIES["heading_quoted_later"]) is None


@pytest.mark.parametrize("case", sorted(BODIES))
def test_each_path_lands_in_its_bucket(case: str) -> None:
    out = _classify([_msg(1, RELAY_AUTHOR, BODIES[case])])
    ref = "p/T-iv#msg-1"
    assert out["msg_ids"]["iv_pr_gate_relay"] == [ref]
    assert out["iv_by_kind"][EXPECTED[case]] == [ref]
    others = [k for k in MEASURE.IV_KINDS if k != EXPECTED[case]]
    assert all(out["iv_by_kind"][k] == [] for k in others)


def test_buckets_add_up_to_iv_and_iv_is_unchanged() -> None:
    cases = sorted(BODIES)
    msgs = [_msg(i, RELAY_AUTHOR, BODIES[c]) for i, c in enumerate(cases, start=1)]
    msgs.append(_msg(len(msgs) + 1, "Bohr", "TIER-C: goal\nNEXT: human"))  # not (iv)
    out = _classify(msgs)
    iv = out["msg_ids"]["iv_pr_gate_relay"]
    assert out["counts"]["iv_pr_gate_relay"] == len(cases) == len(iv)
    assert set(out["iv_by_kind"]) == set(MEASURE.IV_KINDS)
    merged = [r for k in MEASURE.IV_KINDS for r in out["iv_by_kind"][k]]
    assert sorted(merged) == sorted(iv) and len(merged) == len(set(merged))
    assert out["iv_by_kind_counts"] == {"gate_ci_hold": 1, "gate_verdict": 4, "relay_notice": 2}
    assert sum(out["iv_by_kind_counts"].values()) == out["counts"]["iv_pr_gate_relay"]


def test_output_carries_no_body_text() -> None:
    msgs = [_msg(i, RELAY_AUTHOR, BODIES[c]) for i, c in enumerate(sorted(BODIES), start=1)]
    dumped = json.dumps(_classify(msgs), ensure_ascii=False)
    for needle in ("LGTM", "mindwire:verdict", "mindwire:ci-hold", "Conductor stop", "PR-gate"):
        assert needle not in dumped
