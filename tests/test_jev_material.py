"""T-decider-conductor-hook 2e-2b-2 — Jev's ``jev`` on the decision-material PUT.

Spec: Bohr msg-8616 (endorsed Einstein, no objections), against the interface magickit#109
delivered (operator msg-8577 §2): an optional ``jev`` object whose three keys
``should_ask_human`` (number in [0, 1], not bool), ``rule`` (str) and ``model_version`` (str) are
all required when it is sent.

This file pins the Python half: the ``provider`` field on :class:`TierCAnnotation` (optional, so
older lines still parse), the one mapping :func:`jev_material`, and ``scripts/jev_material.py``,
which joins on the composer-read head. The wrapper half (the PUT body, the log line, fail-open) is
in ``tests/Test-DecisionComposerWiring.ps1``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_tierc_jev_bounce import _result

from spirrow_mindwire.decider.annotation import (
    NO_RULE_TEXT,
    UNKNOWN_PROVIDER,
    TierCAnnotation,
    annotation_from_result,
    annotation_line,
    append_annotation,
    jev_material,
    read_annotations,
)
from spirrow_mindwire.decider.verdict import TierCVerdictKind

NOW = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "jev_material.py"


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("_jev_material_cli_test_module", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLI = _load_cli()


def _ann(
    *, rule: str | None = "R3", provider: str | None = "jev", msg_id: str = "msg-9"
) -> TierCAnnotation:
    return TierCAnnotation("p", "T-a", msg_id, 0.12, rule, "tierc-v4", "d", "t", provider)


# --------------------------------------------------------------------------- the mapping


def test_jev_material_maps_the_three_keys_magickit_requires() -> None:
    assert jev_material(_ann()) == {
        "should_ask_human": 0.12,
        "rule": "R3",
        "model_version": "jev/tierc-v4",
    }


def test_no_rule_uses_the_digest_text_so_rule_is_always_a_str() -> None:
    jev = jev_material(_ann(rule=None))
    assert jev is not None
    assert jev["rule"] == NO_RULE_TEXT
    # The page and the digest say the same thing.
    assert annotation_line(_ann(rule=None)).endswith(f"— {jev['rule']}")


def test_no_provider_says_unknown_so_model_version_is_never_empty() -> None:
    jev = jev_material(_ann(provider=None))
    assert jev is not None
    assert jev["model_version"] == f"{UNKNOWN_PROVIDER}/tierc-v4"


def test_no_annotation_is_no_jev() -> None:
    assert jev_material(None) is None


@pytest.mark.parametrize("rule", ["R3", None])
@pytest.mark.parametrize("provider", ["jev", None])
def test_every_mapped_object_passes_magickit_s_rules(
    rule: str | None, provider: str | None
) -> None:
    """The checks magickit#109 applies (operator msg-8577 §2), restated as the receiver's view."""
    jev = jev_material(_ann(rule=rule, provider=provider))
    assert jev is not None
    assert set(jev) == {"should_ask_human", "rule", "model_version"}
    p = jev["should_ask_human"]
    assert isinstance(p, int | float) and not isinstance(p, bool) and 0.0 <= p <= 1.0
    assert isinstance(jev["rule"], str) and jev["rule"]
    assert isinstance(jev["model_version"], str) and jev["model_version"]


# --------------------------------------------------------------------------- provider field


def test_annotation_from_result_records_the_provider() -> None:
    ann = annotation_from_result(
        _result(TierCVerdictKind.UNSURE, 0.4), project="p", thread_id="T-a", msg_id="m", now=NOW
    )
    assert ann is not None
    assert ann.provider == "jev"


def test_provider_round_trips_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    append_annotation(path, _ann())
    found, skipped = read_annotations(path)
    assert skipped == 0
    assert found[("p", "T-a", "msg-9")].provider == "jev"


def test_a_line_written_before_provider_existed_still_parses(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    old = {
        "project": "p",
        "thread_id": "T-a",
        "msg_id": "msg-9",
        "ask_score": 0.3,
        "matched_rule": "R1",
        "questions_version": "tierc-v4",
        "decision_id": "d",
        "logged_at": "t",
    }
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    found, skipped = read_annotations(path)
    assert skipped == 0
    ann = found[("p", "T-a", "msg-9")]
    assert ann.provider is None
    assert ann == TierCAnnotation("p", "T-a", "msg-9", 0.3, "R1", "tierc-v4", "d", "t")


def test_a_non_string_provider_is_a_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    row = {
        "project": "p",
        "thread_id": "T-a",
        "msg_id": "msg-9",
        "ask_score": 0.3,
        "matched_rule": None,
        "questions_version": "tierc-v4",
        "decision_id": None,
        "logged_at": "t",
        "provider": 7,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert read_annotations(path) == ({}, 1)


# --------------------------------------------------------------------------- the CLI


def _cli(tmp_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = _CLI.main(list(argv))
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def _argv(path: Path, head: str = "msg-9") -> list[str]:
    return [
        "--annotations",
        str(path),
        "--project",
        "p",
        "--thread-id",
        "T-a",
        "--head-msg-id",
        head,
    ]


def test_cli_prints_the_jev_of_exactly_this_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "a.jsonl"
    append_annotation(path, _ann(msg_id="msg-8", rule="R1"))
    append_annotation(path, _ann(msg_id="msg-9", rule="R3"))
    code, out, _ = _cli(tmp_path, capsys, *_argv(path))
    assert code == 0
    assert out == {"jev": {"should_ask_human": 0.12, "rule": "R3", "model_version": "jev/tierc-v4"}}


def test_cli_another_head_is_null(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.jsonl"
    append_annotation(path, _ann(msg_id="msg-8"))
    code, out, _ = _cli(tmp_path, capsys, *_argv(path, head="msg-9"))
    assert (code, out) == (0, {"jev": None})


def test_cli_missing_file_is_null(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _cli(tmp_path, capsys, *_argv(tmp_path / "absent.jsonl"))
    assert (code, out) == (0, {"jev": None})


def test_cli_last_line_per_head_wins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.jsonl"
    append_annotation(path, _ann(rule="R1"))
    append_annotation(path, _ann(rule="R4"))
    _, out, _ = _cli(tmp_path, capsys, *_argv(path))
    assert out["jev"]["rule"] == "R4"


def test_cli_stdout_is_ascii_even_for_the_japanese_rule_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "a.jsonl"
    append_annotation(path, _ann(rule=None))
    code = _CLI.main(_argv(path))
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.isascii()
    assert json.loads(out)["jev"]["rule"] == NO_RULE_TEXT


def test_cli_unreadable_file_exits_1_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A directory where the file should be: exists, cannot be read as a file.
    path = tmp_path / "a.jsonl"
    path.mkdir()
    code, out, err = _cli(tmp_path, capsys, *_argv(path))
    assert code == 1
    assert out is None
    assert "annotations unreadable" in err


def test_cli_counts_malformed_lines_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "a.jsonl"
    append_annotation(path, _ann())
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    code, out, err = _cli(tmp_path, capsys, *_argv(path))
    assert code == 0
    assert out["jev"] is not None
    assert "1 malformed" in err
